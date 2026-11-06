"""A ready preview is fresh, bounded on disk and never sends controller input."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location('ps5_autojb', Path(__file__).resolve().parents[1] / 'launcher/ps5_autojb.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def test_preview_uses_a_free_session_without_controller_input(tmp_path, monkeypatch):
    console = M.Console({'host': 'example.invalid'}, tmp_path)
    calls = []
    def api(path, data=None):
        calls.append(path)
        return {'open': False, 'holder': None}
    monkeypatch.setattr(console, 'api', api)
    monkeypatch.setattr(console, 'snapshot', lambda: b'fresh png')
    url = console.ready_preview()
    assert calls == ['/api/maker', '/api/maker/open', '/api/maker/close']
    assert url.startswith('http://127.0.0.1:8091/api/screenshots/autojb-ready.png?v=')
    assert (tmp_path/'screenshots/autojb-ready.png').read_bytes() == b'fresh png'
    console.ready_preview()
    assert [p.name for p in (tmp_path/'screenshots').iterdir()] == ['autojb-ready.png']


def test_busy_remote_play_is_not_taken_over(tmp_path, monkeypatch):
    console = M.Console({'host': 'example.invalid'}, tmp_path)
    calls = []
    def api(path, data=None):
        calls.append(path)
        return {'open': True, 'holder': 'someone else'}
    monkeypatch.setattr(console, 'api', api)
    with pytest.raises(M.Refused, match='busy'):
        console.ready_preview()
    assert calls == ['/api/maker']


def test_capture_failure_closes_its_session(tmp_path, monkeypatch):
    console = M.Console({'host': 'example.invalid'}, tmp_path)
    calls = []
    monkeypatch.setattr(console, 'api', lambda path, data=None: calls.append(path) or {'open': False, 'holder': None})
    def fail(): raise M.Refused('frame unavailable')
    monkeypatch.setattr(console, 'snapshot', fail)
    with pytest.raises(M.Refused, match='frame unavailable'):
        console.ready_preview()
    assert calls[-1] == '/api/maker/close'
    assert not (tmp_path/'screenshots/autojb-ready.png').exists()


@pytest.mark.parametrize('state,capture_fails', [('jailbreak confirmed', False), ('jailbreak confirmed', True), ('already jailbroken', False)])
def test_preview_is_optional_and_only_after_a_new_success(tmp_path, monkeypatch, state, capture_fails):
    shots = []
    def capture():
        shots.append(True)
        if capture_fails: raise M.Refused('busy')
        return 'http://example/ready.png'
    console = SimpleNamespace(ready_preview=capture, report=lambda _: None)
    monkeypatch.setattr(M, 'Console', lambda *a: console)
    monkeypatch.setattr(M, 'launch', lambda *a: {'state': state, 'launched': state == 'jailbreak confirmed'})
    result = M.operate({}, tmp_path)
    assert result['state'] == state
    assert len(shots) == int(state == 'jailbreak confirmed')
    assert bool(result.get('preview')) == (state == 'jailbreak confirmed' and not capture_fails)
