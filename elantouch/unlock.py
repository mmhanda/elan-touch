"""
Touch-to-unlock for desktops whose lock screen has no fingerprint support of its own
(e.g. KDE Plasma < 5.25): while the session is locked, run a fingerprint verification through the
fprintd API and ask logind to unlock the session on a match. Runs as the logged-in user.
"""
import logging
import os

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

FPRINT = "net.reactivated.Fprint"
SCREENSAVER = "org.freedesktop.ScreenSaver"
log = logging.getLogger("elan-touch-unlock")


class Unlocker:
    def __init__(self):
        self.session, self.system = dbus.SessionBus(), dbus.SystemBus()
        self.device = None
        self.misses = 0
        self.locked = False
        self.session.add_signal_receiver(self.on_lock_changed, "ActiveChanged", "org.freedesktop.ScreenSaver")
        self.system.add_signal_receiver(self.on_status, "VerifyStatus", FPRINT + ".Device")
        # This service can start before the desktop has claimed the screensaver name, so
        # watch for it appearing instead of assuming it is already there.
        self.session.add_signal_receiver(self.on_screensaver_appeared, "NameOwnerChanged",
                                         "org.freedesktop.DBus", "org.freedesktop.DBus",
                                         "/org/freedesktop/DBus", arg0=SCREENSAVER)
        self.query_state()

    def on_screensaver_appeared(self, _name, _old, new):
        if new:
            log.info("screen locker appeared - syncing state")
            self.query_state()

    def query_state(self):
        """Ask the screen locker whether the session is locked right now."""
        try:
            saver = dbus.Interface(self.session.get_object(SCREENSAVER, "/ScreenSaver"), SCREENSAVER)
            self.on_lock_changed(bool(saver.GetActive()))
            return True
        except dbus.DBusException:
            return False

    def on_lock_changed(self, locked):
        self.locked = bool(locked)
        log.info("session %s", "locked" if self.locked else "unlocked")
        if self.locked:
            self.misses = 0
            self.start()
        else:
            self.stop()

    def start(self):
        if not self.locked or self.device is not None:
            return False
        try:
            manager = dbus.Interface(self.system.get_object(FPRINT, "/net/reactivated/Fprint/Manager"),
                                     FPRINT + ".Manager")
            device = dbus.Interface(self.system.get_object(FPRINT, manager.GetDefaultDevice()), FPRINT + ".Device")
            device.Claim("")
            self.device = device
            device.VerifyStart("any")
        except dbus.DBusException as exc:        # no prints, sensor busy (a password prompt is using it)...
            log.info("cannot start verification: %s", exc.get_dbus_name())
            self.stop()
            GLib.timeout_add_seconds(5, self.start)
        return False

    def stop(self):
        device, self.device = self.device, None
        for call in ("VerifyStop", "Release"):
            try:
                if device is not None:
                    getattr(device, call)()
            except dbus.DBusException:
                pass

    def on_status(self, result, done):
        if self.device is None:
            return
        if result == "verify-match":
            log.info("fingerprint matched - unlocking")
            self.stop()
            self.unlock()
        elif done:
            self.misses += 1
            self.stop()
            GLib.timeout_add_seconds(min(30, 2 ** min(self.misses, 5)) if self.misses > 2 else 1, self.start)

    def unlock(self):
        login1 = dbus.Interface(self.system.get_object("org.freedesktop.login1", "/org/freedesktop/login1"),
                                "org.freedesktop.login1.Manager")
        for sid, uid, _user, _seat, path in login1.ListSessions():
            if int(uid) != os.getuid():
                continue
            props = dbus.Interface(self.system.get_object("org.freedesktop.login1", path),
                                   "org.freedesktop.DBus.Properties")
            if str(props.Get("org.freedesktop.login1.Session", "Type")) in ("x11", "wayland"):
                dbus.Interface(self.system.get_object("org.freedesktop.login1", path),
                               "org.freedesktop.login1.Session").Unlock()
                log.info("asked logind to unlock session %s", sid)


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    Unlocker()
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
