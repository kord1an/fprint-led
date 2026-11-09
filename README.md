# fprint-led

A small Linux service that blinks an existing laptop LED while fingerprint
verification is active. The LED returns to its original trigger when the
verification finishes or is cancelled.

The initial implementation uses the microphone mute LED. It changes only the
LED's trigger and brightness, preserving the actual microphone mute state.

## How it works

The service observes fprintd on the system D-Bus. It starts blinking only after
a successful response to `VerifyStart`, then stops on a terminal verification
status, a confirmed stop/release, client disconnection, or fprintd disappearance.
It does not start verification itself or modify PAM, drivers, or authentication
policy. Detection is event-driven; the only periodic operation is LED blinking.

The systemd unit adds automatic startup, journald logging, restart on failure,
and independent LED recovery after the monitor is killed. Before taking control
of the LED, the monitor saves its original state in a private runtime file.

## Current status

Experimental, locally tested on Fedora 45 with GNOME 51, a Synaptics reader,
and `/sys/class/leds/hda::micmute` using the `audio-micmute` trigger.

Confirmed locally:

- Successful fingerprint verification and cancellation in the terminal.
- GNOME lock-screen verification and cancellation by signing in with a password.
- Fingerprint authentication requested by an application during Flatpak installation.
- Restoration of the microphone mute indication without changing mute state.
- Cleanup on Ctrl+C and recovery/restart after SIGKILL under systemd.
- Automatic startup after reboot, followed by successful lock-screen verification.

First fingerprint login through GDM after reboot remains unverified: in the
observed boot, the indicator was ready before GDM, but fprintd was first activated
later during lock-screen authentication. The indicator does not enable fingerprint
authentication where the login screen has not requested it.

## Try it

Requires Python 3.10 or newer and PyGObject (`python3-gobject` on Fedora), plus working
fprintd authentication. Run commands from the project directory.

Start with observation only:

```sh
sudo python3 fprint_led.py --dry-run
```

Wait for `READY`, then run `fprintd-verify` in another terminal. To test the LED,
stop the observer and run it again without `--dry-run`.

See [the manual PoC guide](POC.md) for the complete workflow and manual LED
recovery, and [the systemd guide](SERVICE.md) for configuration and crash cleanup.

For a first system installation, after stopping the manual observer:

```sh
sudo bash ./install-service.sh
```

The installer starts the service and enables boot startup. It refuses to overwrite
an existing installation. See [SERVICE.md](SERVICE.md#update) for updates and
[SERVICE.md](SERVICE.md#uninstall) for full removal.

## Permissions and limits

- System D-Bus monitoring requires UID 0 with Fedora's dbus-broker. The installed
  unit drops Linux capabilities and restricts system writes to its runtime
  directory and two LED sysfs attributes.
- A different LED requires changing both configuration and the unit's writable
  paths. Other LEDs and readers have not been tested.
- After a monitor restart, blinking resumes on a new verification. The monitor
  does not reconstruct a verification already in progress.
- SIGKILL recovery requires the systemd installation. Recovery cannot run during
  power loss or kernel failure, or restore inaccessible hardware.

## Development

```sh
python3 -m unittest discover -v
systemd-analyze verify --generators=no --man=no systemd/fprint-led.service
```

The unit tests use synthetic events and temporary files, without accessing the
system D-Bus or real LED. GitHub Actions runs these checks automatically; passing
CI does not establish compatibility with another reader or laptop.

## License

[MIT](LICENSE).
