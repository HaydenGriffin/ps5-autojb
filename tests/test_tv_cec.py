"""tv/ps5-cec.py: only probe or X, only to the device that names itself PlayStation 5."""
import importlib.util
import io
import struct
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('ps5_cec', Path(__file__).resolve().parents[1] / 'tv/ps5-cec.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class Bus:
    """A fake /dev/cec0: devices by logical address, and every message the TV sent."""
    def __init__(self, devices, ack=True):
        self.devices, self.ack, self.sent = devices, ack, []

    def ioctl(self, fd, request, msg, mutate):
        assert request == M.CEC_TRANSMIT and mutate
        length = struct.unpack('<I', M._bytes(msg)[16:20])[0]
        data = list(msg[32:32 + length])
        self.sent.append(data)
        dest = data[0] & 0x0F
        present = dest in self.devices
        msg[50] = 0x01 if present and (self.ack or data[1] == 0x46) else 0x24
        if present and data[1] == 0x46 and msg[48] == 0x47:
            answer = [(dest << 4) | 0, 0x47] + list(self.devices[dest].encode())
            for i, b in enumerate(answer):
                msg[32 + i] = b
            for i, b in enumerate(struct.pack('<I', len(answer))):
                msg[16 + i] = b


def run(command, bus):
    out = io.StringIO()
    code = M.main(command, opener=lambda *a: 99, ioctl=bus.ioctl, out=out, closer=lambda fd: None)
    return code, out.getvalue()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(M.time, 'sleep', lambda s: None)


def presses(bus):
    return [d for d in bus.sent if len(d) > 1 and d[1] in (0x44, 0x45)]


def test_x_finds_the_ps5_by_name_and_presses_select():
    bus = Bus({4: 'PlayStation 5', 8: 'SHIELD'})
    assert run('x', bus) == (0, 'pressed x on ps5 4\n')
    assert presses(bus) == [[0x04, 0x44, 0x00], [0x04, 0x45]]


def test_the_ps5_is_found_wherever_its_address_moved():
    bus = Bus({4: 'SHIELD', 8: 'PlayStation 5'})
    assert run('x', bus)[0] == 0
    assert presses(bus) == [[0x08, 0x44, 0x00], [0x08, 0x45]]


def test_probe_presses_nothing():
    bus = Bus({4: 'PlayStation 5'})
    assert run('probe', bus) == (0, 'ps5 4\n')
    assert presses(bus) == []


@pytest.mark.parametrize('command', ['', 'select', 'x; reboot', 'up', 'X', 'probe x'])
def test_anything_else_is_refused_without_touching_the_bus(command):
    bus = Bus({4: 'PlayStation 5'})
    assert run(command, bus)[0] == 2
    assert bus.sent == []


def test_no_ps5_means_no_press():
    bus = Bus({8: 'SHIELD'})
    assert run('x', bus)[0] == 3
    assert presses(bus) == []


def test_unacknowledged_press_is_reported():
    bus = Bus({4: 'PlayStation 5'}, ack=False)
    assert run('x', bus)[0] == 4


def test_source_is_ascii_for_the_tvs_python_2():
    # 6 Nov: a "section" sign in the docstring was a SyntaxError on the TV (no encoding declaration in Python 2).
    (Path(__file__).resolve().parents[1] / 'tv/ps5-cec.py').read_bytes().decode('ascii')
