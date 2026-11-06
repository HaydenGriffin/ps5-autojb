#!/usr/bin/env python3
"""Print a DualSense's Bluetooth address and the console it is paired with, for the ESP32 waker's secrets.

Linux only, run as root, with the controller connected by USB and no bluetoothd running (BlueZ's sixaxis plugin
re-pairs a USB DualSense to the computer). Never use macOS for this: it re-pairs a USB DualSense to the Mac (even with
Bluetooth off), and the report then names the Mac instead of the PS5. Pair the controller to the PS5 first (USB cable
to the console, press PS), then plug it into the Linux machine.

    sudo python3 tools/read_dualsense.py
"""
import fcntl
import glob
import os
import sys

SONY = '054C'


def hidiocgfeature(length):
    return (3 << 30) | (length << 16) | (ord('H') << 8) | 0x07


def address(raw):
    return ':'.join('%02X' % b for b in reversed(raw))


def pairing(report):
    """Feature report 0x09: bytes 1-6 the controller, bytes 10-15 its paired host, both little-endian."""
    if report[0] != 0x09 or len(report) < 16:
        raise ValueError('not a DualSense pairing report')
    return address(report[1:7]), address(report[10:16])


def main():
    for path in sorted(glob.glob('/sys/class/hidraw/hidraw*')):
        with open(path + '/device/uevent') as f:
            if SONY not in f.read().upper():
                continue
        fd = os.open('/dev/' + os.path.basename(path), os.O_RDWR)
        try:
            buf = bytearray(64)
            buf[0] = 0x09
            fcntl.ioctl(fd, hidiocgfeature(64), buf, True)
        finally:
            os.close(fd)
        controller, console = pairing(bytes(buf))
        print(f'ps5_waker_dualsense_bt_addr: "{controller}"')
        print(f'ps5_waker_console_bt_addr: "{console}"')
        return 0
    print('No Sony controller found on USB (hidraw).', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
