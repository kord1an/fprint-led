#!/usr/bin/bash
# First installation only. Run explicitly as root after reviewing SERVICE.md.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi

source_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
destination=/usr/local/libexec/fprint-led

# Never silently replace an existing installation or administrator configuration.
for target in "$destination" /etc/fprint-led.conf /etc/systemd/system/fprint-led.service; do
    if [[ -e "$target" || -L "$target" ]]; then
        echo "Refusing to overwrite $target. Review an update manually." >&2
        exit 1
    fi
done

# The accepted PoC used the old lock path; the current program uses the new one.
# Neither may be running when switching to systemd.
for lock in /run/fprint-led-poc-hda::micmute.lock /run/fprint-led/hda::micmute.lock; do
    if [[ -e "$lock" ]] && ! /usr/bin/flock --nonblock "$lock" /usr/bin/true; then
        echo "Stop the manual PoC with Ctrl+C before installing the service." >&2
        exit 1
    fi
done

/usr/bin/python3 -BEs -c 'import sys; sys.path.insert(0, sys.argv[1]); from fprint_led import parse_args; parse_args(["--config", sys.argv[1] + "/fprint-led.conf"])' "$source_dir"
/usr/bin/systemd-analyze verify --generators=no --man=no "$source_dir/systemd/fprint-led.service"
/usr/bin/install -d -o root -g root -m 0755 "$destination"
/usr/bin/install -o root -g root -m 0644 "$source_dir/fprint_led.py" "$source_dir/led_state.py" "$destination/"
/usr/bin/install -o root -g root -m 0644 "$source_dir/fprint-led.conf" /etc/fprint-led.conf
/usr/bin/install -o root -g root -m 0644 "$source_dir/systemd/fprint-led.service" /etc/systemd/system/fprint-led.service
/usr/sbin/restorecon -RF "$destination" /etc/fprint-led.conf /etc/systemd/system/fprint-led.service
/usr/bin/systemctl daemon-reload
/usr/bin/systemctl start fprint-led.service
/usr/bin/systemctl enable fprint-led.service
echo "Installed and started fprint-led.service; boot startup is enabled."
echo "Logs: journalctl -u fprint-led.service -b --no-pager"
