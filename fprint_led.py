#!/usr/bin/env python3
"""Fingerprint LED indicator for manual or systemd operation. No PAM calls."""

import argparse
import configparser
from dataclasses import dataclass
import logging
import math
import os
from pathlib import Path
import signal
import socket

from led_state import LED_ROOT, Snapshot, StateFile, acquire_lock, recover, restore_outputs, validate_led_name

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib


SERVICE = "net.reactivated.Fprint"
INTERFACE = SERVICE + ".Device"
BUS = "org.freedesktop.DBus"
BUS_PATH = "/org/freedesktop/DBus"
DEVICE_PATH = "/net/reactivated/Fprint/Device/0"
SYSTEM_BUS_ADDRESS = "unix:path=/run/dbus/system_bus_socket"
LOG = logging.getLogger("fprint-led")


@dataclass(frozen=True)
class Event:
    kind: str
    sender: str = ""
    destination: str = ""
    path: str = ""
    interface: str = ""
    member: str = ""
    serial: int = 0
    reply_serial: int = 0
    body: tuple = ()
    error: str = ""


@dataclass
class Pending:
    method: str
    attempt: tuple | None
    finished: bool = False


class Verification:
    """Only this state machine decides when the LED may be used."""

    def __init__(self, led, path=DEVICE_PATH):
        self.led = led
        self.path = path
        self.owner = ""
        self.pending = {}
        self.active = None  # (client unique name, VerifyStart serial)

    def stop(self, reason):
        if self.active is not None:
            LOG.info("STOP client=%s reason=%s", self.active[0], reason)
        self.active = None
        self.led.restore()

    def set_owner(self, owner):
        if owner != self.owner:
            self.stop("fprintd owner changed")
            self.pending.clear()
            self.owner = owner
            LOG.info("fprintd owner=%s", owner or "absent")

    def handle(self, event):
        if event.kind == "owner":
            self.set_owner(event.body[0])
            return
        if (event.kind == "signal" and event.sender == BUS
                and event.path == BUS_PATH and event.interface == BUS
                and event.member == "NameOwnerChanged"):
            name, old, new = event.body
            if name == SERVICE:
                self.set_owner(new)
            elif name == old and not new:
                if self.active and self.active[0] == name:
                    self.stop("active client disconnected")
                self.pending = {k: v for k, v in self.pending.items() if k[0] != name}
            return
        if not self.owner:
            return
        if (event.kind == "call" and event.path == self.path
                and event.interface == INTERFACE
                and event.destination in (SERVICE, self.owner)
                and event.member in ("VerifyStart", "VerifyStop", "Release")):
            if len(self.pending) >= 64:
                raise RuntimeError("Too many outstanding fprintd calls; stopping safely")
            key = (event.sender, event.serial)
            attempt = key if event.member == "VerifyStart" else self.active
            # Also cover a client pipelining Stop/Release before the Start reply.
            if event.member != "VerifyStart" and attempt is None:
                starts = [k for k, v in self.pending.items()
                          if k[0] == event.sender and v.method == "VerifyStart"]
                attempt = starts[-1] if starts else None
            self.pending[key] = Pending(event.member, attempt)
        elif event.kind in ("return", "error") and event.sender == self.owner:
            key = (event.destination, event.reply_serial)
            pending = self.pending.pop(key, None)
            if pending is None:
                return
            if event.kind == "error":
                LOG.info("%s rejected client=%s error=%s",
                         pending.method, event.destination, event.error)
                if (pending.method == "VerifyStop"
                        and event.error == SERVICE + ".Error.NoActionInProgress"
                        and self.active == pending.attempt
                        and self.active and self.active[0] == event.destination):
                    self.stop("VerifyStop reports no active verification")
                return
            if pending.method == "VerifyStart" and not pending.finished:
                self.stop("new confirmed verification")
                self.active = key
                LOG.info("START client=%s serial=%d", *key)
                self.led.start()
            elif pending.method != "VerifyStart":
                # Never let another client's Stop/Release affect this attempt.
                if pending.attempt and pending.attempt[0] == event.destination:
                    start = self.pending.get(pending.attempt)
                    if start is not None:
                        start.finished = True
                    if self.active == pending.attempt:
                        self.stop(pending.method + " completed")
        elif (event.kind == "signal" and event.sender == self.owner
                and event.path == self.path and event.interface == INTERFACE
                and event.member == "VerifyStatus"):
            result, done = event.body
            LOG.info("STATUS result=%s done=%s", result, done)
            if done:
                for pending in self.pending.values():
                    if pending.method == "VerifyStart":
                        pending.finished = True
                self.stop("terminal VerifyStatus: " + result)


class Led:
    def __init__(self, name, interval_ms, dry_run, fail, state=None):
        self.path = LED_ROOT / name
        self.interval_ms = interval_ms
        self.dry_run = dry_run
        self.fail = fail
        self.state = state
        self.timer = None
        self.original = None
        self.on = False
        self.maximum = int((self.path / "max_brightness").read_text())
        if self.maximum < 1:
            raise ValueError("LED max_brightness must be positive")
        self.snapshot()  # Validate without changing anything.

    def snapshot(self):
        selected = [s[1:-1] for s in (self.path / "trigger").read_text().split()
                    if s.startswith("[") and s.endswith("]")]
        if len(selected) != 1:
            raise RuntimeError("Cannot determine the LED's original trigger")
        return selected[0], int((self.path / "brightness").read_text())

    def write(self, attribute, value):
        (self.path / attribute).write_text(str(value) + "\n")

    def start(self):
        self.original = self.snapshot()
        LOG.info("LED acquire=%s original_trigger=%s dry_run=%s",
                 self.path.name, self.original[0], self.dry_run)
        if self.dry_run:
            return
        if self.state is not None:
            try:
                self.state.save(Snapshot(self.path.name, *self.original))
            except Exception:
                # Publication failed before any LED write. Do not overwrite or
                # clear an older recovery file in the final cleanup path.
                self.original = None
                raise
        # Save recovery information before the first write, including failures.
        self.write("trigger", "none")
        self.on = True
        self.write("brightness", self.maximum)
        self.timer = GLib.timeout_add(self.interval_ms, self.tick)

    def tick(self):
        try:
            self.on = not self.on
            self.write("brightness", self.maximum if self.on else 0)
            return GLib.SOURCE_CONTINUE
        except Exception as error:
            self.timer = None
            self.fail("LED blink failed: " + str(error))
            return GLib.SOURCE_REMOVE

    def restore(self):
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None
        if self.original is None:
            return
        trigger, brightness = self.original
        if not self.dry_run:
            restore_outputs(trigger, brightness, self.write)
            if self.state is not None:
                self.state.clear()
        self.original = None
        LOG.info("LED restored trigger=%s dry_run=%s", trigger, self.dry_run)


class Monitor:
    def __init__(self, verification, loop, fail):
        self.verification = verification
        self.loop = loop
        self.fail = fail
        self.connection = None
        self.filter_id = None
        self.closed_id = None
        self.getting_owner = False
        self.stopping = False

    def dispatch(self, event):
        if not self.stopping:
            try:
                self.verification.handle(event)
            except Exception as error:
                self.fail("Event handling failed: " + str(error))
        return GLib.SOURCE_REMOVE

    def filter(self, connection, message, incoming, user_data):
        if not incoming:
            return message
        try:
            kind = {Gio.DBusMessageType.METHOD_CALL: "call",
                    Gio.DBusMessageType.METHOD_RETURN: "return",
                    Gio.DBusMessageType.ERROR: "error",
                    Gio.DBusMessageType.SIGNAL: "signal"}.get(message.get_message_type())
            sender = message.get_sender() or ""
            destination = message.get_destination() or ""
            if (kind in ("return", "error") and sender == BUS
                    and destination == self.unique_name):
                # Setup replies must reach call_sync. Queue the owner snapshot
                # in the SAME stream as NameOwnerChanged, without a startup gap.
                if self.getting_owner:
                    if kind == "return":
                        owner, = message.get_body().unpack()
                        GLib.idle_add(self.dispatch, Event("owner", body=(owner,)))
                    elif message.get_error_name() == BUS + ".Error.NameHasNoOwner":
                        GLib.idle_add(self.dispatch, Event("owner", body=("",)))
                return message
            member = message.get_member() or ""
            body = ()
            if kind == "signal" and member in ("NameOwnerChanged", "VerifyStatus"):
                body = message.get_body().unpack()
            event = Event(kind, sender, destination, message.get_path() or "",
                          message.get_interface() or "", member,
                          message.get_serial(), message.get_reply_serial(), body,
                          message.get_error_name() or "")
            # GIO filters run on a worker thread: no state changes or LED I/O here.
            GLib.idle_add(self.dispatch, event)
        except Exception as error:
            GLib.idle_add(self.fail, "D-Bus decoding failed: " + str(error))
        # In particular, consume monitored method calls. Dispatching them through
        # GIO would send automatic error replies, forbidden on a monitor socket.
        return None

    def call(self, interface, method, parameters):
        return self.connection.call_sync(
            BUS, BUS_PATH, interface, method, parameters, None,
            Gio.DBusCallFlags.NONE, 5000, None)

    def start(self):
        # A private connection; never convert Gio's shared bus connection.
        self.connection = Gio.DBusConnection.new_for_address_sync(
            SYSTEM_BUS_ADDRESS,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        self.connection.set_exit_on_close(False)
        self.unique_name = self.connection.get_unique_name()
        self.closed_id = self.connection.connect(
            "closed", lambda *args: self.fail("System D-Bus connection lost"))
        self.filter_id = self.connection.add_filter(self.filter, None)
        lifecycle = (f"type='signal',sender='{BUS}',path='{BUS_PATH}',"
                     f"interface='{BUS}',member='NameOwnerChanged'")
        self.call(BUS, "AddMatch", GLib.Variant("(s)", (lifecycle + f",arg0='{SERVICE}'",)))
        self.getting_owner = True
        try:
            self.call(BUS, "GetNameOwner", GLib.Variant("(s)", (SERVICE,)))
        except GLib.Error as error:
            if Gio.DBusError.get_remote_error(error) != BUS + ".Error.NameHasNoOwner":
                raise
        finally:
            self.getting_owner = False
        path = self.verification.path
        rules = [f"type='method_call',path='{path}',interface='{INTERFACE}',member='{m}'"
                 for m in ("VerifyStart", "VerifyStop", "Release")]
        rules += [f"type='method_return',sender='{SERVICE}'",
                  f"type='error',sender='{SERVICE}'",
                  f"type='signal',sender='{SERVICE}',path='{path}',"
                  f"interface='{INTERFACE}',member='VerifyStatus'", lifecycle]
        self.call(BUS + ".Monitoring", "BecomeMonitor", GLib.Variant("(asu)", (rules, 0)))
        LOG.info("READY: observing new verification attempts; no fprintd activation")

    def close(self):
        self.stopping = True
        if self.connection is not None:
            if self.closed_id is not None:
                self.connection.disconnect(self.closed_id)
            # Keep the consuming filter until the monitor socket has closed.
            if not self.connection.is_closed():
                self.connection.close_sync(None)
            if self.filter_id is not None:
                self.connection.remove_filter(self.filter_id)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="log decisions without writing to the LED")
    parser.add_argument("--config", type=Path, help="optional INI configuration file")
    parser.add_argument("--state-file", type=Path, help="persist original LED state for independent recovery")
    parser.add_argument("--led", help="LED basename under /sys/class/leds (default: hda::micmute)")
    parser.add_argument("--device-path")
    parser.add_argument("--interval", type=float, help="seconds between LED toggles (default: 0.5)")
    args = parser.parse_args(argv)
    defaults = {"led": "hda::micmute", "device_path": DEVICE_PATH, "interval": "0.5"}
    try:
        if args.config is not None:
            config = configparser.ConfigParser(interpolation=None)
            with args.config.open() as stream:
                config.read_file(stream)
            if config.sections() != ["fprint-led"] or config.defaults():
                raise ValueError("Config must contain only the [fprint-led] section")
            if set(config["fprint-led"]) - defaults.keys():
                raise ValueError("Unknown option in [fprint-led]")
            defaults.update(config["fprint-led"])
        args.led = args.led if args.led is not None else defaults["led"]
        args.device_path = args.device_path if args.device_path is not None else defaults["device_path"]
        args.interval = args.interval if args.interval is not None else float(defaults["interval"])
        validate_led_name(args.led)
        if args.state_file is not None and not args.state_file.is_absolute():
            raise ValueError("--state-file must be absolute")
    except (OSError, ValueError, configparser.Error) as error:
        parser.error(str(error))
    if not GLib.Variant.is_object_path(args.device_path):
        parser.error("--device-path must be a valid D-Bus object path")
    if not math.isfinite(args.interval) or not 0.05 <= args.interval <= 5:
        parser.error("--interval must be between 0.05 and 5 seconds")
    return args


def notify_ready():
    address = os.environ.get("NOTIFY_SOCKET")
    if address:
        if address.startswith("@"):
            address = "\0" + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
            channel.connect(address)
            channel.sendall(b"READY=1")


def main():
    args = parse_args()
    log_format = "%(levelname)s %(message)s"
    if "JOURNAL_STREAM" not in os.environ:
        log_format = "%(asctime)s " + log_format
    logging.basicConfig(level=logging.INFO, format=log_format)
    loop = GLib.MainLoop()
    exit_code = 0
    shutdown_requested = False

    def fail(reason):
        nonlocal exit_code, shutdown_requested
        exit_code = 1
        shutdown_requested = True
        LOG.error("%s", reason)
        loop.quit()
        return GLib.SOURCE_REMOVE

    def stop(signum):
        nonlocal shutdown_requested
        shutdown_requested = True
        LOG.info("Stopping on %s", signal.Signals(signum).name)
        loop.quit()

    led = monitor = None
    lock_fd = None
    old_handlers = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, lambda sig, frame: stop(sig))
        lock_fd = acquire_lock(args.led)
        state = StateFile(args.state_file) if args.state_file is not None else None
        if state is not None:
            if args.dry_run and state.load() is not None:
                raise RuntimeError("Dry run cannot recover saved state; run recovery first")
            if not args.dry_run:
                recover(state, expected_led=args.led)
        led = Led(args.led, round(args.interval * 1000), args.dry_run, fail, state)
        verification = Verification(led, args.device_path)
        monitor = Monitor(verification, loop, fail)
        monitor.start()
        if not shutdown_requested:
            notify_ready()
            loop.run()
    except Exception as error:
        fail(str(error))
    finally:
        if monitor is not None:
            monitor.stopping = True
        if led is not None:
            try:
                led.restore()
            except Exception as error:
                fail("LED restoration FAILED; manual recovery required: " + str(error))
        if monitor is not None:
            try:
                monitor.close()
            except Exception as error:
                fail("D-Bus cleanup failed: " + str(error))
        if lock_fd is not None:
            os.close(lock_fd)
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
