"""Verification checks. No system bus, real LED, root, or authentication."""

import unittest
from unittest.mock import Mock
from pathlib import Path

from fprint_led import BUS, BUS_PATH, DEVICE_PATH, INTERFACE, SERVICE, Event, Led, Verification


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.led = Mock()
        self.state = Verification(self.led)
        self.state.set_owner(":1.10")
        self.led.reset_mock()

    def call(self, method="VerifyStart", client=":1.20", serial=1):
        self.state.handle(Event("call", client, SERVICE, DEVICE_PATH, INTERFACE,
                                method, serial))

    def reply(self, client=":1.20", serial=1, kind="return", error=""):
        self.state.handle(Event(kind, ":1.10", client, reply_serial=serial, error=error))

    def status(self, done=True, sender=":1.10"):
        self.state.handle(Event("signal", sender, path=DEVICE_PATH, interface=INTERFACE,
                                member="VerifyStatus", body=("verify-no-match", done)))

    def vanished(self, client=":1.20"):
        self.state.handle(Event("signal", BUS, path=BUS_PATH, interface=BUS,
                                member="NameOwnerChanged", body=(client, client, "")))

    def start(self):
        self.call()
        self.reply()
        self.led.reset_mock()

    def test_request_alone_and_wrong_reply_cannot_start(self):
        self.call()
        self.reply(client=":1.21")
        self.led.start.assert_not_called()
        self.reply()
        self.led.start.assert_called_once()

    def test_rejected_start_does_not_interrupt_an_existing_attempt(self):
        self.start()
        self.call(client=":1.21")
        self.reply(client=":1.21", kind="error", error=SERVICE + ".Error.AlreadyInUse")
        self.assertEqual(self.state.active, (":1.20", 1))
        self.led.restore.assert_not_called()

    def test_finger_selected_and_other_device_are_not_starts(self):
        self.state.handle(Event("signal", ":1.10", path=DEVICE_PATH, interface=INTERFACE,
                                member="VerifyFingerSelected", body=("any",)))
        self.state.handle(Event("call", ":1.20", SERVICE, DEVICE_PATH + "1", INTERFACE,
                                "VerifyStart", 1))
        self.reply()
        self.led.start.assert_not_called()

    def test_terminal_status_stops_but_retry_and_spoofed_signal_do_not(self):
        self.start()
        self.status(done=False)
        self.status(sender=":1.99")
        self.led.restore.assert_not_called()
        self.status()
        self.assertIsNone(self.state.active)
        self.led.restore.assert_called_once()

    def test_only_the_active_clients_disconnect_stops(self):
        self.start()
        self.vanished(":1.21")
        self.led.restore.assert_not_called()
        self.vanished()
        self.assertIsNone(self.state.active)
        self.led.restore.assert_called_once()

    def test_disconnected_clients_late_start_reply_is_ignored(self):
        self.call()
        self.vanished()
        self.reply()
        self.led.start.assert_not_called()

    def test_terminal_status_before_start_reply_cannot_restart_blinking(self):
        self.call()
        self.status()
        self.reply()
        self.led.start.assert_not_called()

    def test_successful_stop_and_release_restore_the_led(self):
        for method in ("VerifyStop", "Release"):
            with self.subTest(method=method):
                self.start()
                self.call(method, serial=2)
                self.led.restore.assert_not_called()
                self.reply(serial=2)
                self.assertIsNone(self.state.active)
                self.led.restore.assert_called_once()

    def test_other_clients_stop_cannot_stop_active_attempt(self):
        self.start()
        self.call("VerifyStop", client=":1.21", serial=2)
        self.reply(client=":1.21", serial=2)
        self.led.restore.assert_not_called()

    def test_old_release_reply_cannot_stop_a_new_attempt(self):
        self.start()
        self.call("Release", serial=2)
        self.vanished()
        self.call(client=":1.21", serial=1)
        self.reply(client=":1.21", serial=1)
        self.led.reset_mock()
        self.reply(serial=2)
        self.assertEqual(self.state.active, (":1.21", 1))
        self.led.restore.assert_not_called()

    def test_stop_before_start_reply_suppresses_late_start(self):
        self.call()
        self.call("VerifyStop", serial=2)
        self.reply(serial=2)
        self.reply()
        self.led.start.assert_not_called()

    def test_owner_replacement_clears_active_and_pending_calls(self):
        self.start()
        self.call(client=":1.21", serial=2)
        self.state.set_owner(":1.11")
        self.reply(client=":1.21", serial=2)
        self.led.restore.assert_called_once()
        self.assertIsNone(self.state.active)
        self.assertEqual(self.state.pending, {})


class LedTests(unittest.TestCase):
    def led(self, original=("audio-micmute", 1), dry_run=False):
        led = Led.__new__(Led)
        led.timer = None
        led.original = original
        led.dry_run = dry_run
        led.state = None
        led.path = Path("/sys/class/leds/hda::micmute")
        led.write = Mock()
        return led

    def test_trigger_is_restored_even_if_brightness_write_fails(self):
        led = self.led()
        led.write.side_effect = [OSError("brightness unavailable"), None]
        led.restore()
        self.assertEqual(led.write.call_args_list[-1].args, ("trigger", "audio-micmute"))
        self.assertIsNone(led.original)

    def test_failed_trigger_restoration_retains_recovery_state(self):
        led = self.led()
        led.write.side_effect = [None, OSError("trigger unavailable")]
        with self.assertRaises(OSError):
            led.restore()
        self.assertIsNotNone(led.original)

    def test_manual_trigger_preserves_original_brightness(self):
        led = self.led(("none", 1))
        led.restore()
        self.assertEqual([c.args for c in led.write.call_args_list],
                         [("brightness", 1), ("trigger", "none")])

    def test_dry_run_never_writes(self):
        led = self.led(dry_run=True)
        led.restore()
        led.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
