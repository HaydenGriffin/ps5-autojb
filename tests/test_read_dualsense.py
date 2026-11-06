"""The pairing report's two addresses come out in display order."""
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('read_dualsense', Path(__file__).resolve().parents[1] / 'tools/read_dualsense.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def test_addresses_are_reversed_into_display_order():
    report = bytes([0x09, 0x66, 0x55, 0x44, 0x33, 0x22, 0x11, 0, 0, 0, 0xF6, 0xE5, 0xD4, 0xC3, 0xB2, 0xA1]) + bytes(4)
    assert M.pairing(report) == ('11:22:33:44:55:66', 'A1:B2:C3:D4:E5:F6')


def test_other_reports_are_refused():
    with pytest.raises(ValueError):
        M.pairing(bytes([0x05]) + bytes(19))
