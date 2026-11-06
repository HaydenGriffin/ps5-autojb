#!/usr/bin/env python
"""ps5-cec: press X on the PS5 over HDMI-CEC from a rooted LG webOS TV.

Only the ps5-autojb launcher runs this, through a forced-command SSH key in /home/root/.ssh/authorized_keys. The
command it asked for arrives in SSH_ORIGINAL_COMMAND:
  probe   print the PS5's CEC logical address; exit 3 when no device names itself "PlayStation 5"
  x       User Control Pressed "Select" (the controller's X / OK), then Released, to the PS5
Anything else exits 2 without touching the bus. The PS5 is found by its OSD name every time, because logical
addresses can move. It works whatever input the TV is showing. Install: tv/README.md.

The TV has Python 2.7 (keep this file ASCII); tests run it under Python 3 with a fake /dev/cec0 (tests/test_tv_cec.py).
"""
import array
import fcntl
import os
import struct
import sys
import time

DEVICE = '/dev/cec0'
PLAYBACK = (4, 8, 9, 11)  # CEC logical addresses for playback devices
PS5_NAME = 'PlayStation 5'
SELECT = 0x00
TV = 0  # this TV's logical address (CEC_ADAP_G_LOG_ADDRS, 6 Nov)


def _io(direction, size, nr):
    return int((direction << 30) | (size << 16) | (0x61 << 8) | nr)


CEC_TRANSMIT = _io(3, 56, 5)


def _bytes(buf):
    return buf.tobytes() if hasattr(buf, 'tobytes') else buf.tostring()


def transmit(fd, data, reply=0, ioctl=fcntl.ioctl):
    """One CEC message from the TV. Returns (tx_status, reply payload bytes)."""
    msg = array.array('B', [0] * 56)
    header = struct.pack('<QQIIII', 0, 0, len(data), 1000 if reply else 0, 0, 0)
    for i, b in enumerate(bytearray(header)):
        msg[i] = b
    for i, b in enumerate(data):
        msg[32 + i] = b
    msg[48] = reply
    ioctl(fd, CEC_TRANSMIT, msg, True)
    length = struct.unpack('<I', _bytes(msg)[16:20])[0]
    return msg[50], bytearray(_bytes(msg)[32:32 + length])


def find_ps5(fd, ioctl=fcntl.ioctl):
    for address in PLAYBACK:
        status, answer = transmit(fd, [(TV << 4) | address, 0x46], reply=0x47, ioctl=ioctl)  # Give OSD Name
        if status & 0x01 and answer[2:].decode('ascii', 'replace') == PS5_NAME:
            return address
    return None


def press(fd, address, key, ioctl=fcntl.ioctl):
    pressed, _ = transmit(fd, [(TV << 4) | address, 0x44, key], ioctl=ioctl)
    time.sleep(0.15)
    released, _ = transmit(fd, [(TV << 4) | address, 0x45], ioctl=ioctl)
    return bool(pressed & 0x01) and bool(released & 0x01)


def main(command, opener=os.open, ioctl=fcntl.ioctl, out=sys.stdout, closer=os.close):
    if command not in ('probe', 'x'):
        out.write('refused: only probe or x\n')
        return 2
    fd = opener(DEVICE, os.O_RDWR)
    try:
        address = find_ps5(fd, ioctl)
        if address is None:
            out.write('no PlayStation 5 on CEC\n')
            return 3
        if command == 'probe':
            out.write('ps5 %d\n' % address)
            return 0
        if not press(fd, address, SELECT, ioctl):
            out.write('ps5 %d did not acknowledge\n' % address)
            return 4
        out.write('pressed x on ps5 %d\n' % address)
        return 0
    finally:
        closer(fd)


if __name__ == '__main__':
    sys.exit(main((os.environ.get('SSH_ORIGINAL_COMMAND') or ' '.join(sys.argv[1:])).strip()))
