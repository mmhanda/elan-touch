"""
A drop-in implementation of fprintd's D-Bus API (net.reactivated.Fprint) backed by the elan-touch
engine, so pam_fprintd, fprintd-enroll/verify/list/delete and desktop settings work unchanged.
"""
import logging
import pwd
import threading

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

from . import config, store
from .engine import ACCEPT_Z, Engine, Touch
from .sensor import SensorError

BUS_NAME = "net.reactivated.Fprint"
MANAGER_PATH = "/net/reactivated/Fprint/Manager"
DEVICE_PATH = "/net/reactivated/Fprint/Device/0"
MANAGER_IFACE = "net.reactivated.Fprint.Manager"
DEVICE_IFACE = "net.reactivated.Fprint.Device"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

FINGERS = ("left-thumb", "left-index-finger", "left-middle-finger", "left-ring-finger",
           "left-little-finger", "right-thumb", "right-index-finger", "right-middle-finger",
           "right-ring-finger", "right-little-finger")
ENROLL_STAGES = 25
TOUCHES_PER_VERIFY = config.load()["touches_per_verify"]

log = logging.getLogger("elan-touch")


class FprintError(dbus.DBusException):
    def __init__(self, name, message):
        super().__init__(message)
        self._dbus_error_name = "net.reactivated.Fprint.Error." + name


class Manager(dbus.service.Object):
    def __init__(self, bus, device):
        super().__init__(bus, MANAGER_PATH)
        self.device = device

    @dbus.service.method(MANAGER_IFACE, out_signature="ao")
    def GetDevices(self):
        return [dbus.ObjectPath(DEVICE_PATH)]

    @dbus.service.method(MANAGER_IFACE, out_signature="o")
    def GetDefaultDevice(self):
        return dbus.ObjectPath(DEVICE_PATH)


class Device(dbus.service.Object):
    def __init__(self, bus):
        super().__init__(bus, DEVICE_PATH)
        self.bus = bus
        self.owner = self.user = None          # unique bus name / username holding the claim
        self.worker = None
        self.cancel = threading.Event()
        self.templates = {}                    # (user, finger) -> Template, kept warm between uses
        self.props = {"name": "ELAN touch fingerprint sensor (elan-touch)",
                      "num-enroll-stages": dbus.Int32(ENROLL_STAGES), "scan-type": "press",
                      "finger-present": False, "finger-needed": False}
        bus.add_signal_receiver(self._name_owner_changed, "NameOwnerChanged", "org.freedesktop.DBus",
                                "org.freedesktop.DBus", "/org/freedesktop/DBus")

    # ---- plumbing ----------------------------------------------------------------------------

    def _name_owner_changed(self, name, old, new):
        if name == self.owner and not new:      # the client vanished without releasing
            log.info("client %s disappeared, releasing", name)
            self._stop_worker()
            self.owner = self.user = None

    def _caller(self, sender):
        uid = self.bus.get_unix_user(sender)
        return uid, pwd.getpwuid(uid).pw_name

    def _polkit(self, sender, action, granted, denied):
        """Ask polkit the same questions fprintd asks. Falls back to 'same user or root'."""
        def fallback(_exc=None):
            uid, name = self._caller(sender)
            (granted if uid == 0 or name == self.user else denied)()
        try:
            authority = dbus.Interface(self.bus.get_object("org.freedesktop.PolicyKit1",
                                                           "/org/freedesktop/PolicyKit1/Authority"),
                                       "org.freedesktop.PolicyKit1.Authority")
            subject = ("system-bus-name", {"name": dbus.String(sender, variant_level=1)})
            authority.CheckAuthorization(subject, action, {}, dbus.UInt32(1), "",
                                         reply_handler=lambda r: (granted if r[0] else denied)(),
                                         error_handler=fallback, timeout=300)
        except dbus.DBusException:
            fallback()

    def _set(self, name, value):
        if self.props[name] != value:
            self.props[name] = value
            self.PropertiesChanged(DEVICE_IFACE, {name: value}, [])

    def _templates(self, user):
        for f in store.fingers(user):
            if (user, f) not in self.templates:
                self.templates[(user, f)] = store.Template.load(user, f)
        return [t for (u, _), t in self.templates.items() if u == user and t.views]

    def _require_claim(self, sender):
        if self.owner is None:
            raise FprintError("ClaimDevice", "Device was not claimed before use")
        if self.owner != sender:
            raise FprintError("AlreadyInUse", "Device is in use by another client")

    def _stop_worker(self):
        self.cancel.set()
        if self.worker is not None and self.worker.is_alive():
            self.worker.join(timeout=3.0)
        self.worker = None
        self._set("finger-needed", False)
        self._set("finger-present", False)

    def _start(self, target, *args):
        self._stop_worker()
        self.cancel = threading.Event()
        args = tuple(self.cancel if a is None else a for a in args)
        self.worker = threading.Thread(target=self._guard, args=(target,) + args, daemon=True)
        self.worker.start()

    def _guard(self, target, *args):
        kind = "verify" if target == self._verify else "enroll"
        signal = self.VerifyStatus if kind == "verify" else self.EnrollStatus
        try:
            target(*args)
        except SensorError as exc:
            log.error("%s: %s", kind, exc)
            GLib.idle_add(signal, f"{kind}-disconnected", True)
        except Exception:
            log.exception("%s failed", kind)
            GLib.idle_add(signal, f"{kind}-unknown-error", True)
        finally:
            GLib.idle_add(self._set, "finger-needed", False)
            GLib.idle_add(self._set, "finger-present", False)

    def _touch(self, eng, cancel):
        GLib.idle_add(self._set, "finger-needed", True)
        return eng.touch(timeout=3600, cancel=cancel,
                         on_finger=lambda down: GLib.idle_add(self._set, "finger-present", down))

    # ---- the two long-running operations (worker thread) ---------------------------------------

    def _verify(self, user, cancel):
        templates = self._templates(user)
        misses = 0
        with Engine() as eng:
            while not cancel.is_set():
                status, lin = self._touch(eng, cancel)
                if status == Touch.CANCELLED:
                    return
                if status != Touch.OK:
                    GLib.idle_add(self.VerifyStatus, "verify-retry-scan", False)
                    eng.wait_lift(cancel)
                    continue
                result, tpl = max(((eng.score(t, lin), t) for t in templates), key=lambda x: x[0][0])
                log.info("verify %s: z=%.1f overlap=%.0f%% (%s, %d views)", user, result[0],
                         100 * result[2], tpl.finger, len(tpl.views))
                if result[0] >= ACCEPT_Z:
                    eng.learn(tpl, lin, result)
                    GLib.idle_add(self.VerifyStatus, "verify-match", True)
                    return
                misses += 1
                if misses >= TOUCHES_PER_VERIFY:
                    GLib.idle_add(self.VerifyStatus, "verify-no-match", True)
                    return
                GLib.idle_add(self.VerifyStatus, "verify-retry-scan", False)
                eng.wait_lift(cancel)

    def _enroll(self, user, finger, cancel):
        tpl = store.Template(user, finger)
        stage = 0
        with Engine() as eng:
            while not cancel.is_set() and stage < ENROLL_STAGES:
                status, lin = self._touch(eng, cancel)
                if status == Touch.CANCELLED:
                    return
                if status != Touch.OK:
                    GLib.idle_add(self.EnrollStatus, "enroll-retry-scan", False)
                    eng.wait_lift(cancel)
                    continue
                z, _, overlap, _, _ = eng.score(tpl, lin)
                if z < ACCEPT_Z or overlap < 0.85:         # skip near-duplicates, keep new skin
                    tpl.add(lin)
                    tpl._cache = None
                stage += 1
                if stage < ENROLL_STAGES:
                    GLib.idle_add(self.EnrollStatus, "enroll-stage-passed", False)
                    eng.wait_lift(cancel)
        if stage >= ENROLL_STAGES:
            tpl.save()
            self.templates[(user, finger)] = tpl
            log.info("enrolled %s for %s with %d views", finger, user, len(tpl.views))
            GLib.idle_add(self.EnrollStatus, "enroll-completed", True)

    # ---- D-Bus API ---------------------------------------------------------------------------

    @dbus.service.method(DEVICE_IFACE, in_signature="s", out_signature="as")
    def ListEnrolledFingers(self, username):
        user = username or self.user
        names = store.fingers(user) if user else []
        if not names:
            raise FprintError("NoEnrolledPrints", "Failed to discover prints")
        return names

    @dbus.service.method(DEVICE_IFACE, in_signature="s", sender_keyword="sender")
    def Claim(self, username, sender=None):
        uid, caller = self._caller(sender)
        user = username or caller
        if self.owner is not None and self.owner != sender:
            raise FprintError("AlreadyInUse", "Device was already claimed")
        if uid != 0 and user != caller:
            raise FprintError("PermissionDenied", "Not authorized to act for another user")
        try:
            pwd.getpwnam(user)
        except KeyError:
            raise FprintError("PermissionDenied", f"unknown user {user}")
        self.owner, self.user = sender, user
        self._templates(user)                  # warm the cache while the client talks to the user

    @dbus.service.method(DEVICE_IFACE, sender_keyword="sender")
    def Release(self, sender=None):
        self._require_claim(sender)
        self._stop_worker()
        self.owner = self.user = None

    @dbus.service.method(DEVICE_IFACE, in_signature="s", sender_keyword="sender",
                         async_callbacks=("reply", "error"))
    def VerifyStart(self, finger_name, sender=None, reply=None, error=None):
        try:
            self._require_claim(sender)
            if finger_name != "any" and finger_name not in FINGERS:
                raise FprintError("InvalidFingername", "Invalid finger name")
            if not self._templates(self.user):
                raise FprintError("NoEnrolledPrints", f"No fingers enrolled for {self.user}")
        except FprintError as exc:
            return error(exc)
        user = self.user

        def go():
            self._start(self._verify, user, None)
            reply()
            self.VerifyFingerSelected("any")
        self._polkit(sender, "net.reactivated.fprint.device.verify", go,
                     lambda: error(FprintError("PermissionDenied", "Not authorized")))

    @dbus.service.method(DEVICE_IFACE, sender_keyword="sender")
    def VerifyStop(self, sender=None):
        self._require_claim(sender)
        self._stop_worker()

    @dbus.service.method(DEVICE_IFACE, in_signature="s", sender_keyword="sender",
                         async_callbacks=("reply", "error"))
    def EnrollStart(self, finger_name, sender=None, reply=None, error=None):
        try:
            self._require_claim(sender)
            if finger_name not in FINGERS:
                raise FprintError("InvalidFingername", "Invalid finger name")
        except FprintError as exc:
            return error(exc)
        user = self.user

        def go():
            self._start(self._enroll, user, finger_name, None)
            reply()
        self._polkit(sender, "net.reactivated.fprint.device.enroll", go,
                     lambda: error(FprintError("PermissionDenied", "Not authorized")))

    @dbus.service.method(DEVICE_IFACE, sender_keyword="sender")
    def EnrollStop(self, sender=None):
        self._require_claim(sender)
        self._stop_worker()

    def _delete(self, user, finger, sender, reply, error):
        def go():
            store.delete(user, finger)
            for key in [k for k in self.templates if k[0] == user and finger in (None, k[1])]:
                del self.templates[key]
            reply()
        self._polkit(sender, "net.reactivated.fprint.device.enroll", go,
                     lambda: error(FprintError("PermissionDenied", "Not authorized")))

    @dbus.service.method(DEVICE_IFACE, in_signature="s", sender_keyword="sender",
                         async_callbacks=("reply", "error"))
    def DeleteEnrolledFingers(self, username, sender=None, reply=None, error=None):
        uid, caller = self._caller(sender)
        user = username or caller
        if uid != 0 and user != caller:
            return error(FprintError("PermissionDenied", "Not authorized to act for another user"))
        self._delete(user, None, sender, reply, error)

    @dbus.service.method(DEVICE_IFACE, sender_keyword="sender", async_callbacks=("reply", "error"))
    def DeleteEnrolledFingers2(self, sender=None, reply=None, error=None):
        try:
            self._require_claim(sender)
        except FprintError as exc:
            return error(exc)
        self._delete(self.user, None, sender, reply, error)

    @dbus.service.method(DEVICE_IFACE, in_signature="s", sender_keyword="sender",
                         async_callbacks=("reply", "error"))
    def DeleteEnrolledFinger(self, finger_name, sender=None, reply=None, error=None):
        try:
            self._require_claim(sender)
            if finger_name not in FINGERS:
                raise FprintError("InvalidFingername", "Invalid finger name")
        except FprintError as exc:
            return error(exc)
        self._delete(self.user, finger_name, sender, reply, error)

    @dbus.service.signal(DEVICE_IFACE, signature="s")
    def VerifyFingerSelected(self, finger_name):
        pass

    @dbus.service.signal(DEVICE_IFACE, signature="sb")
    def VerifyStatus(self, result, done):
        pass

    @dbus.service.signal(DEVICE_IFACE, signature="sb")
    def EnrollStatus(self, result, done):
        pass

    # ---- org.freedesktop.DBus.Properties ---------------------------------------------------------

    @dbus.service.method(PROPS_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface, prop):
        if prop not in self.props:
            raise dbus.DBusException(f"no such property {prop}",
                                     name="org.freedesktop.DBus.Error.UnknownProperty")
        return self.props[prop]

    @dbus.service.method(PROPS_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        return dict(self.props)

    @dbus.service.signal(PROPS_IFACE, signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed, invalidated):
        pass


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    name = dbus.service.BusName(BUS_NAME, bus, do_not_queue=True)   # noqa: F841 (must stay referenced)
    device = Device(bus)
    Manager(bus, device)
    log.info("serving %s", BUS_NAME)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
