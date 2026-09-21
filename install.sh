#!/bin/bash
# Install elan-touch as this machine's fingerprint service. Re-running it upgrades in place.
# It never touches /var/lib/elan-touch (your calibration and enrolled fingers).
set -euo pipefail
[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"
cd "$(dirname "$0")"

if command -v apt-get >/dev/null; then
    apt-get install -y python3-usb python3-numpy python3-opencv python3-dbus python3-gi fprintd libpam-fprintd
else
    echo "Not a Debian/Ubuntu system: install pyusb, numpy, opencv-python, dbus-python, PyGObject," >&2
    echo "fprintd (for its D-Bus policy file) and pam_fprintd yourself, then re-run." >&2
fi

install -d /usr/local/lib/elan-touch/elantouch
install -m 0644 elantouch/*.py /usr/local/lib/elan-touch/elantouch/
cat > /usr/local/bin/elan-touch <<'LAUNCHER'
#!/bin/sh
exec env PYTHONPATH=/usr/local/lib/elan-touch PYTHONWARNINGS=ignore python3 -m elantouch.cli "$@"
LAUNCHER
chmod 0755 /usr/local/bin/elan-touch
install -d -m 0700 /var/lib/elan-touch /var/lib/elan-touch/sensor /var/lib/elan-touch/users

# Same unit name as the stock fprintd, in /etc: it takes precedence without replacing any package.
install -m 0644 systemd/fprintd.service /etc/systemd/system/fprintd.service
install -m 0644 systemd/elan-touch-unlock.service /etc/systemd/user/elan-touch-unlock.service
# Same pam_fprintd, but a 15 s window instead of 10: reaching for the sensor takes a few seconds.
[ -d /usr/share/pam-configs ] && install -m 0644 systemd/elan-touch.pam-config /usr/share/pam-configs/elan-touch
systemctl daemon-reload
systemctl enable fprintd >/dev/null 2>&1 || true
systemctl restart fprintd

cat <<'NEXT'

elan-touch is installed and is now this machine's fprintd. Next:

  1. sudo elan-touch calibrate      once per device: measures the sensor's own pattern
  2. sudo elan-touch enroll         adaptive; it stops by itself when you are recognised reliably
  3. sudo elan-touch verify         live check
  4. sudo elan-touch pam on          fingerprint for sudo, polkit and login (password still works)
  5. systemctl --user enable --now elan-touch-unlock       touch-to-unlock (desktops without
                                                           lock-screen fingerprint support)
NEXT
