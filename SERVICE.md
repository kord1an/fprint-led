# Systemd installation and operation

The system service uses an INI configuration, an atomic LED recovery file,
and an independent standard-library-only recovery helper. The supplied installer
targets Fedora with systemd, SELinux, and working fprintd authentication.

## Install

First stop any manual PoC with Ctrl+C. Review these files before installing:

- `fprint_led.py`: the event-driven monitor, configuration, and readiness.
- `led_state.py`: runtime locking, recovery-state persistence, and cleanup.
- `fprint-led.conf`: LED name, blink interval, and fprintd device path.
- `systemd/fprint-led.service`: startup, restart, cleanup, and restrictions.
- `install-service.sh`: first-install commands; refuses existing installations.

Run from the project directory:

```sh
sudo bash ./install-service.sh
```

This copies the two Python files into `/usr/local/libexec/fprint-led`, the
configuration into `/etc/fprint-led.conf`, and the unit into
`/etc/systemd/system/fprint-led.service`. Files are root-owned and not writable by
normal users. The installer restores Fedora SELinux file labels, reloads systemd,
starts the service, and enables it for boot. It does not replace existing files
or change D-Bus, polkit, PAM, or sysfs permissions.

```sh
systemctl status fprint-led.service --no-pager
journalctl -u fprint-led.service -b --no-pager
```

## Recovery and restarts

Before writing `none` to the LED trigger, the monitor publishes its previous
trigger and brightness to `/run/fprint-led/led-state.json`. The file is mode 0600;
the runtime directory is mode 0700. Publication is atomic and refuses to replace
an unrecovered state. No fingerprint identity, template, or result is stored.

Normal completion restores the LED before removing the state file. systemd's
`ExecStopPost` invokes `led_state.py` even if the monitor was killed with SIGKILL.
The helper shares the monitor's per-LED lock and does not import Gio or contact
D-Bus. `ExecStartPre` retries leftover recovery before starting a new monitor.
Failed restoration retains the state and logs the error. The runtime directory
is preserved across service stops and restarts, but `/run` is cleared at reboot.

systemd restarts the monitor after unexpected exits or D-Bus loss, with a
2-second delay and a limit of 5 starts per minute. Invalid configuration exits
with status 2 and is not retried. A manual `systemctl stop` does not restart it.
fprintd owner changes are handled inside the monitor without activating fprintd.
After a monitor restart, an authentication already in progress is left alone;
the LED resumes only for a newly observed and confirmed verification.

`Type=notify` reports readiness after monitoring is established. At boot, the
unit is ordered before `display-manager.service`. The display manager does not
require this service: an indicator failure cannot deny authentication. A failed
startup may delay the display manager up to the startup timeout (15 seconds).

Cleanup still requires accessible LED sysfs attributes and a functioning system
manager. No userspace cleanup can run during power loss or kernel failure.

## Permissions and configuration

UID 0 is required by dbus-broker's monitor privilege check. The unit drops all
Linux capabilities, prevents privilege escalation, makes the filesystem read-only,
hides home directories and physical devices, and restricts sockets to AF_UNIX.
Its system write exceptions are its runtime directory and the selected LED's two
sysfs files; it also has private temporary storage. It cannot write physical
audio mixer controls or fingerprint storage.

Edit `/etc/fprint-led.conf` and restart the service to change the interval or
fprintd device path. To select another LED, stop the service and recover the old
LED before editing the configuration. Use `sudo systemctl edit fprint-led.service`
to override the unit's two writable paths, for example:

```ini
[Service]
ReadWritePaths=
ReadWritePaths=/sys/class/leds/NEW_LED/brightness /sys/class/leds/NEW_LED/trigger
```

Replace `NEW_LED` with the same basename as the configuration's `led` value,
then run `sudo systemctl daemon-reload` and start the service again. This
extra administrator step keeps write access narrow instead of granting access
to all of `/sys`. Stop and recover the old LED before changing the selection.

The runtime lock path changed from the stage-2 PoC. Stop any already-running old
PoC before switching to the new code or service. Never unlink a held lock file.

## Acceptance checks after installation

Check success and password cancellation on the GNOME lock screen, microphone
mute indication after restoration, and startup at the next normal boot. Do not
restart the system bus or change authentication configuration for testing.

To check SIGKILL cleanup, start `fprintd-verify` in one terminal and, while the LED
is blinking, run in another:

```sh
sudo systemctl kill --kill-whom=main --signal=SIGKILL fprint-led.service
```

The LED should return to its original trigger and the indicator service should
restart. This kills only the indicator. Cancel the still-running verification
with Ctrl+C, then check the journal and `/sys/class/leds/hda::micmute/trigger`.

To stop the installed service and disable boot startup:

```sh
sudo systemctl disable --now fprint-led.service
```

If cleanup failed, the saved state must be recovered before removing anything:

```sh
sudo /usr/bin/python3 -BEs /usr/local/libexec/fprint-led/led_state.py --restore /run/fprint-led/led-state.json
```

## Update

Updates are manual. Review the source, run the tests, and keep local configuration
and systemd overrides. From the checkout of the desired revision:

```sh
python3 -m unittest discover -v
sudo systemctl stop fprint-led.service
sudo /usr/bin/python3 -BEs /usr/local/libexec/fprint-led/led_state.py --restore /run/fprint-led/led-state.json
sudo install -o root -g root -m 0644 fprint_led.py led_state.py /usr/local/libexec/fprint-led/
sudo install -o root -g root -m 0644 systemd/fprint-led.service /etc/systemd/system/fprint-led.service
sudo restorecon -RF /usr/local/libexec/fprint-led /etc/systemd/system/fprint-led.service
sudo systemctl daemon-reload
sudo systemctl start fprint-led.service
```

Run these steps in order and stop if any command fails. In particular, successful
recovery must precede replacing the helper. The existing `/etc/fprint-led.conf`
and systemd drop-ins are retained. Review changes to the sample configuration
and unit before applying them. The first-install script intentionally refuses
an existing installation.

This also upgrades an early local installation using `poc.py`: the new unit runs
`fprint_led.py`. The old installed `poc.py` may remain unused; it can be removed
once the new service has been checked. Updating a checkout alone does not change
the installed service.

## Uninstall

First disable and stop the service, then recover any remaining LED state using
the installed helper:

```sh
sudo systemctl disable --now fprint-led.service
sudo /usr/bin/python3 -BEs /usr/local/libexec/fprint-led/led_state.py --restore /run/fprint-led/led-state.json
```

If either step fails, resolve it before deleting the helper or recovery state.
After successful cleanup, remove only the installed project files:

```sh
sudo rm -f -- /etc/systemd/system/fprint-led.service
sudo rm -f -- /usr/local/libexec/fprint-led/fprint_led.py /usr/local/libexec/fprint-led/led_state.py
sudo rmdir -- /usr/local/libexec/fprint-led
sudo systemctl daemon-reload
sudo systemctl reset-failed fprint-led.service
```

For an early local installation, also remove the old
`/usr/local/libexec/fprint-led/poc.py` before `rmdir`. `rmdir` intentionally refuses
a non-empty directory; inspect unexpected files instead of removing them
recursively. An already-unloaded unit may make `reset-failed` report that the
unit is not loaded.

To purge configuration too, remove `/etc/fprint-led.conf`. If you created a
`/etc/systemd/system/fprint-led.service.d` directory, remove only this service's
own overrides and reload systemd again. The root-owned runtime directory
`/run/fprint-led` contains locks and is cleared at reboot. Never delete a held
lock file or an unrecovered state file. The source checkout can be kept or
removed independently.

## Development checks

```sh
python3 -m unittest discover -v
systemd-analyze verify --generators=no --man=no systemd/fprint-led.service
```

Unit validation and tests on a private bus/fake LED do not establish that the
service's real mount namespace and SELinux access work. Those require the
interactive acceptance checks after installation.
