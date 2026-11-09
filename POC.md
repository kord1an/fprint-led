# Manual operation

Run the observer manually before installing the systemd service.
See [SERVICE.md](SERVICE.md) for installation and recovery.
Python 3 and Fedora's `python3-gobject` package are required; both are already
available on the original test machine. Run commands from the project directory.

## First run: observe only

Run in a terminal and wait for `READY`:

```sh
sudo python3 ./fprint_led.py --dry-run
```

In another terminal, run `fprintd-verify` and cancel it with Ctrl+C while waiting
for a finger. The observer should report `START`, then `STOP` for the active
client's disconnection or a terminal verification status. No LED writes occur
in dry-run mode. Stop the observer with Ctrl+C.

The observer requires root because Fedora's system dbus-broker restricts
`BecomeMonitor`. It creates a per-LED runtime lock under `/run/fprint-led`, retained until
reboot, to prevent two PoC instances from controlling the same LED. It does not
change permissions or D-Bus/polkit configuration.

## LED run (after checking the dry-run log)

```sh
sudo python3 ./fprint_led.py
```

The default LED is `hda::micmute`, with a toggle every 0.5 seconds: approximately
0.5 seconds on, then 0.5 seconds off. `--led NAME`, `--interval SECONDS`, and
`--device-path PATH` select a different LED, interval, or fprintd device.

For each confirmed verification, the program saves the selected LED trigger,
sets `none`, and toggles brightness. It restores the saved trigger on completion.
For an originally manual (`none`) trigger, it also restores the saved brightness.
For `audio-micmute`, the kernel resumes control using its current audio state;
the program does not write to any audio control or change microphone mute.

## Detection and cleanup

- Uses a private system D-Bus monitoring connection and narrow message matches.
- Starts only after a successful reply to an observed `VerifyStart` call.
- Does not use `VerifyFingerSelected` or `finger-needed` as a start condition.
- Stops on terminal `VerifyStatus`, a confirmed stop/release by the active
  client, that client's disconnection, or a change of fprintd owner.
- Tracks client and request serial; unrelated and rejected requests do not
  control the LED.
- Survives fprintd stopping/restarting and waits for a new verification.
- Exits with an error on system D-Bus loss, attempting LED restoration first.
- Handles Ctrl+C, SIGTERM, and ordinary exceptions with final cleanup.
- Never calls `Claim`, `VerifyStart`, `VerifyStop`, or `Release` itself.
- Does not activate fprintd or reconstruct a verification already in progress
  when the observer starts. Start the observer before starting authentication.

The manual workflow cannot restore the LED after SIGKILL, a fatal interpreter
crash, or the machine losing power. The systemd installation adds state persistence and independent
cleanup; see SERVICE.md. If LED restoration fails because the device is unavailable, the
error is logged; the program cannot guarantee recovery of inaccessible hardware.

For the default LED, manual recovery after an interrupted LED run is:

```sh
printf '%s\n' audio-micmute | sudo tee /sys/class/leds/hda::micmute/trigger >/dev/null
```

## Local checks

```sh
python3 -m unittest -v test_verification
```

The focused tests use fake LED operations and synthetic D-Bus events. They do
not access the system bus, authenticate, or write sysfs. Manual checks confirmed
physical blinking, successful fingerprint authentication, GNOME password
cancellation, restoration with a muted microphone, and Ctrl+C cleanup.
See README.md for the scope of the systemd validation.
