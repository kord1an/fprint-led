"""Recovery/configuration tests with temporary files, without system changes."""

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

import led_state
from led_state import Snapshot, StateFile, acquire_lock, recover
from fprint_led import Led, notify_ready, parse_args


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="fprint-led-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.device = self.root / "leds" / "hda::micmute"
        self.device.mkdir(parents=True)
        (self.device / "trigger").write_text("none [audio-micmute]\n")
        (self.device / "brightness").write_text("1\n")
        (self.device / "max_brightness").write_text("1\n")
        self.state = StateFile(self.root / "state.json")
        self.snapshot = Snapshot("hda::micmute", "audio-micmute", 1)
        for target in ("led_state.LED_ROOT", "fprint_led.LED_ROOT"):
            patcher = patch(target, self.device.parent)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("led_state.RUN_ROOT", self.root / "run")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_atomic_state_is_private_and_cannot_replace_old_recovery(self):
        self.state.save(self.snapshot)
        self.assertEqual(self.state.load(), self.snapshot)
        self.assertEqual(stat.S_IMODE(self.state.path.stat().st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            self.state.save(Snapshot("hda::micmute", "none", 0))
        self.assertEqual(self.state.load(), self.snapshot)

    def test_state_exists_before_the_first_led_write(self):
        led = Led("hda::micmute", 500, False, Mock(), self.state)
        seen = []

        def write(attribute, value):
            self.assertEqual(self.state.load(), self.snapshot)
            seen.append((attribute, value))

        led.write = write
        with patch("fprint_led.GLib.timeout_add", return_value=None):
            led.start()
        self.assertEqual(seen[0], ("trigger", "none"))
        led.restore()
        self.assertFalse(self.state.path.exists())

    def test_publication_failure_never_writes_or_clears_existing_state(self):
        self.state.save(self.snapshot)
        led = Led("hda::micmute", 500, False, Mock(), self.state)
        led.write = Mock()
        with self.assertRaises(FileExistsError):
            led.start()
        led.restore()
        led.write.assert_not_called()
        self.assertEqual(self.state.load(), self.snapshot)

    def test_recovery_restores_hardware_then_removes_state(self):
        self.state.save(self.snapshot)
        (self.device / "trigger").write_text("[none] audio-micmute\n")
        recover(self.state)
        self.assertEqual((self.device / "trigger").read_text(), "audio-micmute\n")
        self.assertFalse(self.state.path.exists())

    def test_failed_recovery_retains_state(self):
        self.state.save(self.snapshot)
        with patch("led_state.restore_outputs", side_effect=OSError("device unavailable")):
            with self.assertRaises(OSError):
                recover(self.state)
        self.assertEqual(self.state.load(), self.snapshot)

    def test_recovery_refuses_an_unsupported_trigger_or_wrong_led(self):
        self.state.save(self.snapshot)
        (self.device / "trigger").write_text("[none]\n")
        with self.assertRaises(ValueError):
            recover(self.state)
        with self.assertRaises(RuntimeError):
            recover(self.state, expected_led="another-led")
        self.assertTrue(self.state.path.exists())

    def test_recovery_never_accepts_a_path_instead_of_a_led_name(self):
        data = {"version": 1, "led": "../outside", "trigger": "none", "brightness": 1}
        self.state.path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.state.load()

    def test_state_symlink_is_not_followed(self):
        target = self.root / "other.json"
        target.write_text("{}")
        self.state.path.symlink_to(target)
        with self.assertRaises(OSError):
            self.state.load()

    def test_lock_prevents_cleanup_during_active_monitor(self):
        self.state.save(self.snapshot)
        fd = acquire_lock("hda::micmute")
        try:
            with patch("sys.argv", ["led_state.py", "--restore", str(self.state.path)]):
                with self.assertLogs("fprint-led", level="ERROR"):
                    self.assertEqual(led_state.main(), 1)
        finally:
            os.close(fd)
        self.assertTrue(self.state.path.exists())

    def test_dry_run_does_not_publish_state(self):
        led = Led("hda::micmute", 500, True, Mock(), self.state)
        led.write = Mock()
        led.start()
        led.restore()
        led.write.assert_not_called()
        self.assertFalse(self.state.path.exists())


class ConfigurationTests(unittest.TestCase):
    def config(self, content):
        fd, name = tempfile.mkstemp(prefix="fprint-led-config-")
        os.close(fd)
        path = Path(name)
        path.write_text(content)
        self.addCleanup(path.unlink)
        return path

    def test_config_and_explicit_cli_override(self):
        path = self.config("[fprint-led]\nled = other-led\ninterval = 0.7\n")
        args = parse_args(["--config", str(path), "--interval", "0.2"])
        self.assertEqual((args.led, args.interval), ("other-led", 0.2))

    def test_unknown_configuration_is_rejected(self):
        path = self.config("[fprint-led]\nintervall = 0.5\n")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                parse_args(["--config", str(path)])
        self.assertEqual(error.exception.code, 2)

    def test_invalid_interval_and_relative_state_are_rejected(self):
        for arguments in (["--interval", "nan"], ["--state-file", "relative.json"]):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parse_args(arguments)

    def test_readiness_uses_the_systemd_datagram_protocol(self):
        with patch.dict(os.environ, {"NOTIFY_SOCKET": "@fprint-led-test"}, clear=True):
            with patch("fprint_led.socket.socket") as socket:
                notify_ready()
        channel = socket.return_value.__enter__.return_value
        channel.connect.assert_called_once_with("\0fprint-led-test")
        channel.sendall.assert_called_once_with(b"READY=1")


if __name__ == "__main__":
    unittest.main()
