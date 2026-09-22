#!/bin/bash
# Restore the stock fprintd. Pass --purge to also delete calibration and enrolled fingers.
set -euo pipefail
[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"
elan-touch pam off >/dev/null 2>&1 || true
for profile in elan-touch fprintd; do
    DEBIAN_FRONTEND=noninteractive pam-auth-update --remove "$profile" </dev/null >/dev/null 2>&1 || true
done
rm -f /usr/share/pam-configs/elan-touch
# last resort: if anything still references pam_fprintd in a login path, say so loudly
if grep -q pam_fprintd /etc/pam.d/common-auth 2>/dev/null; then
    echo "WARNING: pam_fprintd is still in /etc/pam.d/common-auth - remove it by hand." >&2
fi
systemctl stop fprintd 2>/dev/null || true
rm -f /etc/systemd/system/fprintd.service /etc/systemd/user/elan-touch-unlock.service
rm -f /etc/systemd/system/multi-user.target.wants/fprintd.service
rm -rf /usr/local/lib/elan-touch /usr/local/bin/elan-touch
systemctl daemon-reload
[ "${1:-}" = "--purge" ] && rm -rf /var/lib/elan-touch
echo "elan-touch removed; the stock fprintd is back in charge."
echo "Each user should also run:  systemctl --user disable --now elan-touch-unlock"
