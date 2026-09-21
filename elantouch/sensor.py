"""Direct USB access to the ELAN sensor. Wire protocol as documented by libfprint's `elan` driver."""
import time

import numpy as np
import usb.core
import usb.util

VID, PIDS = 0x04F3, (0x0C4F,)
EP_CMD_OUT, EP_CMD_IN, EP_IMG_IN = 0x01, 0x83, 0x82

CMD_SENSOR_DIM = b"\x00\x0c"
CMD_FW_VER = b"\x40\x19"
CMD_ACTIVATE = b"\x40\x2a"
CMD_GET_IMAGE = b"\x00\x09"
CMD_CALIB_STATUS = b"\x40\x23"
CMD_LED_ON = b"\x40\x31"
CMD_PRE_SCAN = b"\x40\x3f"
CMD_STOP = b"\x00\x0b"

FINGER, NOT_CALIBRATED = 0x55, 0xFF


class SensorError(Exception):
    pass


class Sensor:
    def __init__(self):
        self.dev = None
        self.width = self.height = 0
        self.firmware = 0

    def open(self):
        for pid in PIDS:
            self.dev = usb.core.find(idVendor=VID, idProduct=pid)
            if self.dev is not None:
                break
        if self.dev is None:
            raise SensorError("no supported ELAN fingerprint sensor found")
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass
        try:
            self.dev.set_configuration()
            usb.util.claim_interface(self.dev, 0)
        except usb.core.USBError as exc:
            raise SensorError(f"cannot claim the sensor (is another fingerprint service using it?): {exc}")
        self._drain()
        fw = self._cmd(CMD_FW_VER, 2)
        dim = self._cmd(CMD_SENSOR_DIM, 4)
        self.firmware = (fw[0] << 8) | fw[1]
        self.width, self.height = dim[0] + 1, dim[2] + 1
        if (self.width, self.height) != (80, 80):
            raise SensorError(f"unsupported sensor geometry {self.width}x{self.height} (this project handles 80x80)")
        self._cmd(CMD_ACTIVATE, 2)
        self._cmd(CMD_LED_ON, 0)

    def close(self):
        if self.dev is None:
            return
        try:
            self._cmd(CMD_STOP, 0)
        except usb.core.USBError:
            pass
        try:
            usb.util.release_interface(self.dev, 0)
        except usb.core.USBError:
            pass
        usb.util.dispose_resources(self.dev)
        self.dev = None

    def _drain(self):
        for ep, n in ((EP_CMD_IN, 64), (EP_IMG_IN, 12800)):
            try:
                while True:
                    self.dev.read(ep, n, timeout=5)
            except usb.core.USBError:
                pass

    def _cmd(self, command, reply_len, timeout=2000, endpoint=EP_CMD_IN):
        self.dev.write(EP_CMD_OUT, command, timeout=2000)
        if reply_len == 0:
            return b""
        return bytes(self.dev.read(endpoint, reply_len, timeout=timeout))

    def frame(self):
        raw = self._cmd(CMD_GET_IMAGE, self.width * self.height * 2, timeout=3000, endpoint=EP_IMG_IN)
        return np.frombuffer(raw, dtype="<u2").reshape(self.height, self.width).astype(np.float32)

    def finger(self, timeout_ms):
        """True: finger present. False: none within the timeout. Recalibrates if the firmware asks."""
        self.dev.write(EP_CMD_OUT, CMD_PRE_SCAN, timeout=2000)
        try:
            code = bytes(self.dev.read(EP_CMD_IN, 1, timeout=timeout_ms))[0]
        except usb.core.USBTimeoutError:
            return False
        if code == NOT_CALIBRATED:
            self.calibrate()
            return False
        return code == FINGER

    def calibrate(self, budget=6.0):
        """Wait out a firmware calibration cycle: status goes 0x01 (busy) then 0x03 (done). This
        firmware needs 0.3-2 s; libfprint's elan driver gives up after 0.5 s."""
        t0, busy = time.time(), False
        while time.time() - t0 < budget:
            st = self._cmd(CMD_CALIB_STATUS, 1)[0]
            busy |= st == 0x01
            if busy and st == 0x03:
                return True
            time.sleep(0.05)
        return False
