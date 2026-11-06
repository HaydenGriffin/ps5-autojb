"""Only a real automatic failure can request one bounded, verified normal restart."""
import importlib.util
from pathlib import Path
import pytest

SPEC = importlib.util.spec_from_file_location('ps5_autojb', Path(__file__).resolve().parents[1] / 'launcher/ps5_autojb.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class Console:
    def __init__(self, choices=None):
        self.choices = iter(choices or [('Restart PS5', 95)] * 2)
        self.powers = iter(['offline'] * 3 + ['awake'])
        self.cancelled = lambda: False
        self.opened = self.closed = self.sent = False
        self.ready = False
        self.allowed = True
        self.home = False
    def probe(self): return self.ready
    def recovery_allowed(self):
        if not self.allowed: raise M.Refused('A game or unknown app is running')
    def open(self): self.opened = True
    def close(self): self.closed = True
    def shutdown_menu(self, target): assert target == 'Restart PS5'
    def shutdown_choice(self): return next(self.choices)
    def press(self, button, wait=0):
        assert button == 'cross'
        self.sent = True
    def wait(self, seconds): pass
    def power(self): return next(self.powers)
    def api(self, path, data):
        assert path == '/api/maker/home'
        self.home = True


def test_restart_requires_exact_selection_then_down_and_up():
    c = Console()
    assert M.restart(c)['state'] == 'rebooted'
    assert c.sent and c.closed


@pytest.mark.parametrize('choice', [('Turn Off PS5', 99), ('Enter Rest Mode', 99), ('Restart PS5', 20)])
def test_restart_never_selects_shutdown_rest_or_uncertain_text(choice):
    c = Console([('Restart PS5', 95), choice])
    with pytest.raises(M.Refused): M.restart(c)
    assert not c.sent and c.closed


def test_late_success_prevents_any_restart():
    c = Console(); c.ready = True
    assert M.restart(c)['state'] == 'already jailbroken'
    assert not c.opened and not c.sent


def test_game_running_never_opens_remote_play():
    c = Console(); c.allowed = False
    with pytest.raises(M.Refused): M.restart(c)
    assert not c.opened and not c.sent


def test_dry_run_returns_home_without_restart():
    c = Console()
    assert M.restart(c, dry_run=True)['state'] == 'restart target verified; not restarted'
    assert c.home and c.closed and not c.sent


def test_cancelled_recovery_never_opens_remote_play():
    c = Console(); c.cancelled = lambda: True
    with pytest.raises(M.Refused, match='cancelled'): M.restart(c)
    assert not c.opened and not c.sent


@pytest.mark.parametrize('power', ['awake', 'rest', 'unknown', 'app'])
def test_restart_without_confirmed_power_cycle_is_not_retried(power):
    c = Console(); c.powers = iter([power] * 60)
    with pytest.raises(M.Refused): M.restart(c)
    assert c.sent and c.closed


@pytest.mark.parametrize('info,allowed', [
    ({'state': 'awake'}, True),
    ({'state': 'app', 'running-app-name': 'Y2JB - Gaming'}, True),
    ({'state': 'app', 'running-app-name': 'A game'}, False),
    ({'state': 'app', 'running-app-name': 'Y2JB - Gaming Tools'}, False),
    ({'state': 'app'}, False), ({'state': 'offline'}, False), ({'state': 'unknown'}, False),
])
def test_recovery_only_accepts_home_or_the_known_failed_app(tmp_path, monkeypatch, info, allowed):
    c = M.Console({'host': 'example.invalid'}, tmp_path)
    monkeypatch.setattr(c, 'power_info', lambda: info, raising=False)
    if allowed: c.recovery_allowed()
    else:
        with pytest.raises(M.Refused): c.recovery_allowed()


def test_one_reboot_and_retry_then_stop_even_after_helper_restart(tmp_path, monkeypatch):
    outputs, restarts, launches = [], [], []
    def operate(*a, **kw):
        launches.append(True)
        return {'state': 'failed: no confirmation', 'launch_attempted': True, 'launched': None}
    monkeypatch.setattr(M, 'operate', operate)
    monkeypatch.setattr(M, 'operate_restart', lambda *a, **kw: restarts.append(True) or {'state': 'rebooted'}, raising=False)
    clock = [0]
    s = M.Launcher({}, tmp_path, outputs.append, now=lambda: clock[0]); s.set_enabled(True)
    s.tick('awake', False); clock[0] = 30; s.tick('awake', False)
    assert len(restarts) == 1 and outputs[-1]['state'] == 'automatic retry waiting'
    s = M.Launcher({}, tmp_path, outputs.append, now=lambda: clock[0])
    s.tick('awake', False); clock[0] = 60; s.tick('awake', False)
    assert len(launches) == 2 and len(restarts) == 1
    assert outputs[-1]['state'].startswith('failed:') and '30 minutes' in outputs[-1]['state']
    clock[0] = 600; s.tick('awake', False)
    assert len(launches) == 2


@pytest.mark.parametrize('source,result', [
    ('manual', {'state': 'failed: no confirmation', 'launch_attempted': True}),
    ('automatic', {'state': 'refused: target missing', 'launch_attempted': False}),
    ('automatic', {'state': 'failed: uncertain', 'launch_attempted': False}),
    ('automatic', {'state': 'jailbreak confirmed', 'launched': True}),
])
def test_other_outcomes_do_not_reboot(tmp_path, monkeypatch, source, result):
    monkeypatch.setattr(M, 'operate', lambda *a, **kw: result)
    monkeypatch.setattr(M, 'operate_restart', lambda *a, **kw: pytest.fail('must not restart'), raising=False)
    s = M.Launcher({}, tmp_path, lambda _: None); s.set_enabled(True)
    s.execute(source)


def test_cancel_during_failed_attempt_prevents_recovery(tmp_path, monkeypatch):
    out = []
    s = M.Launcher({}, tmp_path, out.append); s.set_enabled(True)
    def operate(*a, **kw):
        s.set_enabled(False)
        return {'state': 'failed: no confirmation', 'launch_attempted': True}
    monkeypatch.setattr(M, 'operate', operate)
    monkeypatch.setattr(M, 'operate_restart', lambda *a, **kw: pytest.fail('must not restart'), raising=False)
    s.execute('automatic')
    assert out[-1]['state'] == 'automatic launch cancelled'


def test_reboot_budget_survives_process_restart_and_corruption(tmp_path, monkeypatch):
    path = tmp_path / 'reboots.json'
    M.RebootBudget(path, now=lambda: 100).reserve()
    with pytest.raises(M.Refused): M.RebootBudget(path, now=lambda: 101).reserve()
    M.RebootBudget(path, now=lambda: 1901).reserve()
    path.write_text('{}')
    with pytest.raises(M.Refused): M.RebootBudget(path).reserve()


@pytest.mark.parametrize('raw', ['[NaN]', '[Infinity]', '[true]'])
def test_invalid_budget_values_fail_closed(tmp_path, raw):
    path = tmp_path / 'reboots.json'; path.write_text(raw)
    with pytest.raises(M.Refused): M.RebootBudget(path).reserve()


def test_late_success_after_navigation_does_not_press_restart():
    c = Console()
    def menu(target): c.ready = True
    c.shutdown_menu = menu
    assert M.restart(c)['state'] == 'already jailbroken'
    assert c.home and c.closed and not c.sent


def test_recovery_rechecks_foreground_before_confirming():
    c = Console()
    def menu(target): c.allowed = False
    c.shutdown_menu = menu
    with pytest.raises(M.Refused): M.restart(c)
    assert c.closed and not c.sent


def test_failed_app_may_answer_briefly_before_reboot_goes_offline():
    c = Console(); c.powers = iter(['app', 'awake', 'offline', 'offline', 'offline', 'awake'])
    assert M.restart(c)['state'] == 'rebooted'


def test_busy_remote_play_is_never_closed():
    c = Console()
    def busy(): raise M.Refused('Remote Play busy')
    c.open = busy
    with pytest.raises(M.Refused): M.restart(c)
    assert not c.closed and not c.sent


def test_restart_navigation_skips_turn_off_and_never_confirms_it(tmp_path, monkeypatch):
    c = M.Console({'host': 'example.invalid'}, tmp_path)
    calls = []
    monkeypatch.setattr(c, 'api', lambda path, data=None: calls.append((path, data)))
    monkeypatch.setattr(c, 'wait', lambda _: None)
    monkeypatch.setattr(c, 'snapshot', lambda: b'frame')
    labels = iter([('Games Media', 95), ('Power', 95), ('Power', 95)])
    monkeypatch.setattr(M, 'ocr', lambda *a, **kw: next(labels))
    choices = iter([('Enter Rest Mode', 95), ('Turn Off PS5', 95), ('Restart PS5', 95)])
    monkeypatch.setattr(c, 'shutdown_choice', lambda: next(choices))
    c.shutdown_menu('Restart PS5')
    presses = [d['button'] for path, d in calls if path == '/api/maker/press']
    assert presses.count('cross') == 1  # open Power only; selection never activates either power action
    assert presses[-2:] == ['down', 'down']


def test_bottom_restart_row_outline_is_readable(monkeypatch):
    import io
    from PIL import Image, ImageDraw
    frame = Image.new('RGB', (640, 360), (24, 24, 24))
    ImageDraw.Draw(frame).rectangle((395, 220, 611, 290), outline=(140, 140, 140), width=1)
    ImageDraw.Draw(frame).line((390, 295, 617, 295), fill=(140, 140, 140))
    png = io.BytesIO(); frame.save(png, 'PNG')
    monkeypatch.setattr(M, 'ocr', lambda *a, **kw: ('Restart PS5', 95))
    assert M.selected_power_option(png.getvalue()) == ('Restart PS5', 95)
FIXTURE = Path(__file__).resolve().parent / 'fixtures/power-menu-restart-focused.png'


def test_real_frame_with_restart_focused_and_no_outline_reads_restart(monkeypatch):
    # 6 Nov 13:07 (and the 09:56 refusal): Restart PS5 was the remembered row; the PS5 drew no outline, only its
    # description line ("Close all games and apps, and restart your PS5") under it.
    boxes = []
    def ocr(png, box, psm=7, scale=4):
        boxes.append(box)
        return ('Restart PSS', 72)  # what Tesseract read from this frame's row on the helper machine
    monkeypatch.setattr(M, 'ocr', ocr)
    assert M.selected_power_option(FIXTURE.read_bytes()) == ('Restart PS5', 72)
    assert all(248 <= b[1] <= 256 and b[3] - b[1] <= 20 for b in boxes)  # only the Restart row was read


def menu(focused=None, outline=None, descriptions=None):
    """A synthetic Power menu: three bright labels 33 px apart, dim description lines, optional outline."""
    import io
    from PIL import Image, ImageDraw
    frame = Image.new('RGB', (640, 360), (26, 26, 26))
    d = ImageDraw.Draw(frame)
    tops, y = [], 191
    for i in range(3):
        tops.append(y)
        d.rectangle((428, y, 520, y + 7), fill=(165, 165, 165))
        if i in (descriptions if descriptions is not None else ([focused] if focused is not None else [])):
            d.rectangle((428, y + 17, 520, y + 23), fill=(97, 97, 97))  # text-width, not an outline edge
            y += 16
        y += 33
    d.line((396, 291, 610, 291), fill=(82, 82, 82))
    if outline is not None:
        top = tops[outline] - 8
        d.rectangle((395, top, 611, top + 62), outline=(140, 140, 140), width=1)
    png = io.BytesIO(); frame.save(png, 'PNG')
    return png.getvalue(), tops


@pytest.mark.parametrize('focused,label', [(0, 'Enter Rest Mode'), (1, 'Turn Off PS5'), (2, 'Restart PS5')])
def test_the_one_row_with_a_description_is_the_focused_row(monkeypatch, focused, label):
    png, tops = menu(focused)
    read = {}
    def ocr(png_, box, psm=7, scale=4):
        row = min(range(3), key=lambda i: abs(tops[i] - box[1]))
        read[row] = True
        return (['Enter Rest Made', 'Tum Off PSS', 'Restart PSS'][row], 90)
    monkeypatch.setattr(M, 'ocr', ocr)
    assert M.selected_power_option(png) == (label, 90)
    assert set(read) == {focused}


@pytest.mark.parametrize('descriptions', [[], [0, 2]])
def test_no_description_or_two_descriptions_is_unclear(monkeypatch, descriptions):
    png, _ = menu(descriptions=descriptions)
    monkeypatch.setattr(M, 'ocr', lambda *a, **kw: pytest.fail('nothing should be read'))
    with pytest.raises(M.Refused, match='unclear'):
        M.selected_power_option(png)


def test_crops_that_disagree_about_the_label_are_unclear(monkeypatch):
    png, _ = menu(2)
    answers = iter([('Restart PSS', 75), ('Tum Off PSS', 80), ('Restart PSS', 71)])
    monkeypatch.setattr(M, 'ocr', lambda *a, **kw: next(answers))
    with pytest.raises(M.Refused, match='unclear'):
        M.selected_power_option(png)


@pytest.mark.parametrize('read,label', [('Restart P55', 'Restart PS5'), ('Restart PSS', 'Restart PS5'),
                                        ('Tum Off P55', 'Turn Off PS5'), ('Enter Rest Made', 'Enter Rest Mode')])
def test_the_fonts_5_s_confusion_maps_only_to_whole_labels(read, label):
    assert M.power_label(read) == label


@pytest.mark.parametrize('read', ['Restart', 'Restart PS5 now', 'Restart P5', 'Tum Off', 'Rest Mode'])
def test_partial_or_extra_words_are_not_labels(read):
    assert M.power_label(read) not in M.POWER_LABELS
