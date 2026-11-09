#!/usr/bin/env python3
"""LED recovery using only Python's standard library (also used by systemd)."""

import argparse
from dataclasses import asdict, dataclass
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import tempfile


LED_ROOT = Path("/sys/class/leds")
RUN_ROOT = Path("/run/fprint-led")
LOG = logging.getLogger("fprint-led")


def validate_led_name(name):
    if (not isinstance(name, str) or name in (".", "..")
            or not re.fullmatch(r"[A-Za-z0-9_.:+-]+", name)):
        raise ValueError("LED must be a basename containing letters, digits, _, ., :, + or -")
    return name


@dataclass(frozen=True)
class Snapshot:
    led: str
    trigger: str
    brightness: int


def acquire_lock(name):
    validate_led_name(name)
    RUN_ROOT.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(RUN_ROOT / (name + ".lock"),
                 os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        os.close(fd)
        raise RuntimeError("Another fprint-led instance holds the LED lock") from None
    # Keep the file after exit: unlinking a lock file would allow inode races.
    return fd


class StateFile:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("Recovery state path must be absolute")

    def load(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        with os.fdopen(fd) as stream:
            text = stream.read(4097)
        if len(text) > 4096:
            raise ValueError("Recovery state is too large")
        data = json.loads(text)
        if (not isinstance(data, dict)
                or set(data) != {"version", "led", "trigger", "brightness"}
                or type(data["version"]) is not int or data["version"] != 1):
            raise ValueError("Invalid recovery state format")
        validate_led_name(data["led"])
        if (not isinstance(data["trigger"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.:+-]+", data["trigger"])
                or type(data["brightness"]) is not int or data["brightness"] < 0):
            raise ValueError("Invalid LED recovery values")
        return Snapshot(data["led"], data["trigger"], data["brightness"])

    def save(self, snapshot):
        fd, temporary = tempfile.mkstemp(prefix=".led-state-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"version": 1, **asdict(snapshot)}, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            # Atomic publication without replacing an unrecovered state file.
            os.link(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def clear(self):
        self.path.unlink(missing_ok=True)


def restore_outputs(trigger, brightness, write):
    brightness_error = None
    try:
        write("brightness", brightness if trigger == "none" else 0)
    except OSError as error:
        brightness_error = error
        LOG.warning("Brightness restoration failed: %s", error)
    # Do not let failure of the brightness write prevent trigger restoration.
    write("trigger", trigger)
    if trigger == "none" and brightness_error:
        raise brightness_error


def recover(state, expected_led=None):
    snapshot = state.load()
    if snapshot is None:
        return
    if expected_led is not None and snapshot.led != expected_led:
        raise RuntimeError("Saved state belongs to another LED; run recovery first")
    path = LED_ROOT / snapshot.led
    triggers = (path / "trigger").read_text().split()
    available = {s.strip("[]") for s in triggers}
    maximum = int((path / "max_brightness").read_text())
    if snapshot.trigger not in available or snapshot.brightness > maximum:
        raise ValueError("Saved LED state is not supported by the current device")

    def write(attribute, value):
        (path / attribute).write_text(str(value) + "\n")

    restore_outputs(snapshot.trigger, snapshot.brightness, write)
    state.clear()  # Retain recovery information on any failure above.
    LOG.info("Recovered LED=%s trigger=%s", snapshot.led, snapshot.trigger)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore", type=Path, required=True, metavar="STATE_FILE")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    fd = None
    try:
        state = StateFile(args.restore)
        snapshot = state.load()
        if snapshot is None:
            return 0
        fd = acquire_lock(snapshot.led)
        # Reload under the same per-LED lock used by the monitor.
        recover(state, expected_led=snapshot.led)
        return 0
    except Exception as error:
        LOG.error("LED recovery failed; state retained: %s", error)
        return 1
    finally:
        if fd is not None:
            os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
