"""Full shutdown requires two matching, selected Turn Off frames and never retries."""
import importlib.util
from pathlib import Path
import pytest

SPEC = importlib.util.spec_from_file_location('ps5_autojb', Path(__file__).resolve().parents[1] / 'launcher/ps5_autojb.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class Console:
    def __init__(self, choices=None, power='awake'):
        self.choices = iter(choices or [('Turn Off PS5', 95)] * 2)
        self.initial_power = power
        self.opened = self.closed = self.sent = False
        self.reads = 0
        self.after = 'offline'

    def power(self): return self.after if self.sent else self.initial_power
    def open(self): self.opened = True
    def close(self): self.closed = True
    def shutdown_menu(self): pass
    def shutdown_choice(self):
        self.reads += 1
        return next(self.choices)
    def press(self, button, wait=0):
        assert button == 'cross'
        self.sent = True
    def wait(self, seconds): pass
    def api(self, path, data): assert path == '/api/maker/home'


def test_verified_turn_off_is_sent_once_and_power_loss_is_checked():
    c = Console()
    assert M.shutdown(c)['state'] == 'off'
    assert c.sent and c.closed and c.reads == 2


@pytest.mark.parametrize('choice', [('Enter Rest Mode', 99), ('Restart PS5', 99), ('Turn Off PS5', 49), ('', 0)])
def test_changed_or_uncertain_selection_never_confirms_shutdown(choice):
    c = Console([('Turn Off PS5', 95), choice])
    with pytest.raises(M.Refused): M.shutdown(c)
    assert not c.sent and c.closed


def test_dry_run_returns_home_without_confirming():
    c = Console()
    assert M.shutdown(c, dry_run=True)['state'] == 'target verified; not shut down'
    assert not c.sent and c.closed


@pytest.mark.parametrize('power', ['offline', 'rest', 'unknown'])
def test_off_rest_or_unknown_never_opens_remote_play(power):
    c = Console(power=power)
    with pytest.raises(M.Refused): M.shutdown(c)
    assert not c.opened and not c.sent


def test_still_awake_is_not_reported_as_off():
    c = Console(); c.after = 'awake'
    with pytest.raises(M.Refused, match='not confirmed'): M.shutdown(c)
    assert c.sent and c.closed


@pytest.mark.parametrize('age', [31, -10])
def test_stale_or_future_requests_are_ignored(tmp_path, monkeypatch, age):
    monkeypatch.setattr(M.time, 'time', lambda: 1000)
    monkeypatch.setattr(M, 'operate_shutdown', lambda *a, **kw: pytest.fail('must not operate'), raising=False)
    service = M.Launcher({}, tmp_path, lambda _: None)
    service.power_off({'id': 'a' * 32, 'at': 1000-age})


def test_duplicate_command_is_consumed_before_operating(tmp_path, monkeypatch):
    monkeypatch.setattr(M.time, 'time', lambda: 1000)
    calls, results = [], []
    def operate(*args):
        calls.append(True)
        return {'state': 'off', 'message': 'PS5 is off'}
    monkeypatch.setattr(M, 'operate_shutdown', operate, raising=False)
    service = M.Launcher({}, tmp_path, lambda _: None, publish_power=results.append)
    request = {'id': 'a' * 32, 'at': 999}
    service.power_off(request)
    # The claim also survives a service restart.
    service = M.Launcher({}, tmp_path, lambda _: None, publish_power=results.append)
    service.power_off(request)
    assert calls == [True]
    assert results[0]['id'] == request['id']


@pytest.mark.parametrize('raw,expected', [('Turn Off PS5', 'Turn Off PS5'), ('Tum Off PSS', 'Turn Off PS5'),
    ('Enter Rest Made', 'Enter Rest Mode'), ('Restart PS5', 'Restart PS5')])
def test_selected_outline_and_only_complete_known_labels_are_recognised(monkeypatch, raw, expected):
    import io
    from PIL import Image, ImageDraw
    im = Image.new('RGB', (640, 360), (24, 24, 24))
    ImageDraw.Draw(im).rectangle((395, 188, 611, 258), outline=(140, 140, 140), width=1)
    png = io.BytesIO(); im.save(png, 'PNG')
    monkeypatch.setattr(M, 'ocr', lambda png, box, **kw: ('Power', 95) if box[1] == 136 else (raw, 95))
    assert M.selected_power_option(png.getvalue()) == (expected, 95)


@pytest.mark.parametrize('shape', ['none', 'divider', 'three_edges', 'no_sides'])
def test_missing_or_ambiguous_outline_is_refused(monkeypatch, shape):
    import io
    from PIL import Image, ImageDraw
    im = Image.new('RGB', (640, 360), (24, 24, 24)); draw = ImageDraw.Draw(im)
    if shape == 'divider': draw.line((395, 188, 611, 188), fill=(140, 140, 140))
    if shape in ('three_edges', 'no_sides'):
        for y in ([156, 188, 258] if shape == 'three_edges' else [188, 258]):
            draw.line((395, y, 611, y), fill=(140, 140, 140))
    png = io.BytesIO(); im.save(png, 'PNG')
    monkeypatch.setattr(M, 'ocr', lambda *a, **kw: ('Power', 95))
    with pytest.raises(M.Refused): M.selected_power_option(png.getvalue())


def test_busy_session_is_not_closed(tmp_path, monkeypatch):
    c = Console()
    def busy(): raise M.Refused('Remote Play is busy')
    c.open = busy
    with pytest.raises(M.Refused): M.shutdown(c)
    assert not c.closed and not c.sent


def test_retained_or_malformed_shutdown_mqtt_never_starts_work(tmp_path, monkeypatch):
    import json, sys, types
    started = []
    class Client:
        def __init__(self, **kwargs): pass
        def username_pw_set(self, *a): pass
        def will_set(self, *a, **kw): pass
        def connect(self, *a, **kw): pass
        def subscribe(self, topics): assert (M.PREFIX + '/shutdown', 0) in topics
        def publish(self, *a, **kw): pass
        def loop_forever(self):
            self.on_connect(self, None, {}, 0)
            for payload, retained in [(b'{}', True), (b'bad', False), (b'\xff', False),
                                      (json.dumps({'id': 'a'*32, 'at': 999}).encode(), False)]:
                self.on_message(self, None, types.SimpleNamespace(topic=M.PREFIX+'/shutdown', payload=payload, retain=retained))
    mqtt = types.ModuleType('paho.mqtt.client'); mqtt.Client = Client
    parent = types.ModuleType('paho.mqtt'); parent.client = mqtt
    paho = types.ModuleType('paho'); paho.mqtt = parent
    for name, mod in [('paho', paho), ('paho.mqtt', parent), ('paho.mqtt.client', mqtt)]: monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(M.threading, 'Thread', lambda **kw: types.SimpleNamespace(start=lambda: started.append(kw)))
    M.serve({'mqtt': {'host': 'broker.invalid', 'username': 'test', 'password': 'test'}}, tmp_path)
    work = [t for t in started if t['target'].__name__ == 'power_off']
    assert len(work) == 1 and work[0]['args'][0]['id'] == 'a'*32


@pytest.mark.parametrize('changed', [False, True])
def test_native_menu_cross_requires_two_power_readings(tmp_path, monkeypatch, changed):
    c = M.Console({'host': 'example.invalid'}, tmp_path)
    calls = []
    monkeypatch.setattr(c, 'api', lambda path, data=None: calls.append((path, data)))
    monkeypatch.setattr(c, 'wait', lambda _: None)
    monkeypatch.setattr(c, 'snapshot', lambda: b'frame')
    labels = iter([('Games Media', 95), ('Home', 95), ('Power', 95), ('Music' if changed else 'Power', 95)])
    monkeypatch.setattr(M, 'ocr', lambda *a, **kw: next(labels))
    choices = iter([('Enter Rest Mode', 95), ('Turn Off PS5', 95)])
    monkeypatch.setattr(c, 'shutdown_choice', lambda: next(choices))
    if changed:
        with pytest.raises(M.Refused, match='changed'): c.shutdown_menu()
    else: c.shutdown_menu()
    presses = [d['button'] for path, d in calls if path == '/api/maker/press']
    assert presses.count('cross') == (0 if changed else 1)  # only opens Power, never confirms shutdown
    assert presses.count('right') == 1


def test_unclear_frames_are_retried_without_any_input(tmp_path, monkeypatch):
    c = M.Console({'host': 'example.invalid'}, tmp_path)
    frames = []
    monkeypatch.setattr(c, 'wait', lambda _: None)
    monkeypatch.setattr(c, 'api', lambda *a, **kw: pytest.fail('no input while reading'))
    monkeypatch.setattr(c, 'snapshot', lambda: frames.append(True) or b'frame')
    values = iter([M.Refused('outline incomplete'), ('Tur Off PS5', 64), ('Turn Off PS5', 95)])
    def read(_):
        value = next(values)
        if isinstance(value, Exception): raise value
        return value
    monkeypatch.setattr(M, 'selected_power_option', read)
    assert c.shutdown_choice() == ('Turn Off PS5', 95)
    assert len(frames) == 3
