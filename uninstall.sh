#!/bin/bash
# Restore the stock fprintd. Pass --purge to also delete calibration and enrolled fingers.
set -euo pipefail
[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"
pam-auth-update --disable fprintd 2>/dev/null || true
systemctl stop fprintd 2>/dev/null || true
rm -f /etc/systemd/system/fprintd.service /etc/systemd/user/elan-touch-unlock.service
rm -f /etc/systemd/system/multi-user.target.wants/fprintd.service
rm -rf /usr/local/lib/elan-touch /usr/local/bin/elan-touch
systemctl daemon-reload
[ "${1:-}" = "--purge" ] && rm -rf /var/lib/elan-touch
echo "elan-touch removed; the stock fprintd is back in charge."
echo "Each user should also run:  systemctl --user disable --now elan-touch-unlock"
