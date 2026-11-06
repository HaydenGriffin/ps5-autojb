"""Run paths: named step lists (fresh boot, open a tile by name, wait, press PS) and titles learned from the PS5."""
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('ps5_autojb', Path(__file__).resolve().parents[1] / 'launcher/ps5_autojb.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)

LINUX = {'button': 'Boot Linux', 'steps': [
    {'fresh_boot': {}},
    {'open': {'tile': 'Y2JB - Linux'}},
    {'wait': {'console': 'rest', 'timeout': 120}},
    {'wait': {'seconds': 45}},
    {'press_ps': {}},
    {'wait': {'port': 22, 'timeout': 120}},
]}


class Console:
    """Media row titles by tile, and what each tile reads as; discovery replies in order."""
    def __init__(self, titles, powers=('awake',), ports=(), jailbroken=False):
        self.titles, self.powers, self.ports = titles, list(powers), set(ports)
        self.jailbroken, self.index, self.session, self.sessions = jailbroken, 0, False, 0
        self.pressed, self.waited, self.cancelled = [], 0, lambda: False
    def probe(self): return self.jailbroken
    def power(self): return self.powers.pop(0) if len(self.powers) > 1 else self.powers[0]
    def port_open(self, port): return port in self.ports
    def open(self): self.session = True; self.sessions += 1
    def close(self): self.session = False
    def media(self): assert self.session; self.index = 0
    def title(self):
        assert self.session
        reading = self.titles[self.index]
        return (reading, 35) if reading.startswith('Y2') else (reading, 90)  # the J font reads at 31-40%
    def press(self, button, wait=0):
        assert self.session
        self.pressed.append(button)
        if button == 'right': self.index = min(self.index + 1, len(self.titles) - 1)
    def wait(self, seconds): self.waited += seconds
    def api(self, path, data=None): pass


class Bridge:
    def __init__(self, status='Success'): self.status, self.requests = status, []
    def request(self, action, timeout=None, **kw):
        self.requests.append(action)
        return {'ok': self.status == 'Success', 'status': self.status}


ROW = ['Y2JB - Gaming', 'Prism', 'Payload Manager', 'Y2)B - Backup', 'Y2IB - Linux', 'Pegasus DL']
TITLES = [{'tile': 5, 'title': 'Y2IB - Linux', 'readings': {'Y2IB - Linux': 40, 'Y2)B - Linux': 0}},
          {'tile': 4, 'title': 'Y2)B - Backup', 'readings': {'Y2)B - Backup': 9}}]


def run(console, bridge=Bridge(), path=LINUX, titles=TITLES, restart=None):
    progress = []
    restarts = []
    def fake_restart(c, fresh=False):
        restarts.append(fresh); c.jailbroken = False
        return {'state': 'rebooted'}
    result = M.run_path(console, bridge, 'linux', path, progress.append, titles, restart=restart or fake_restart)
    return result, progress, restarts


def test_linux_path_runs_every_step_in_order():
    c = Console(ROW, powers=['awake', 'awake', 'awake', 'rest'], ports=[22], jailbroken=True)
    bridge = Bridge()
    result, progress, restarts = run(c, bridge)
    assert result['state'] == 'linux done'
    assert restarts == [True]                        # jailbroken, so a verified restart for a fresh boot
    assert c.pressed.count('cross') == 1 and not c.session
    assert bridge.requests == ['bt_wake']
    assert [p.split(': ', 1)[1] for p in progress] == [
        'fresh boot', 'opening Y2JB - Linux', 'waiting for rest', 'waiting 45 s', 'pressing PS', 'waiting for port 22']


def test_a_fresh_console_is_not_restarted():
    c = Console(ROW, powers=['awake', 'rest'], ports=[22])
    _, _, restarts = run(c)
    assert restarts == []


def test_the_tile_is_found_by_its_learned_readings_wherever_it_is():
    row = ['Y2IB - Linux', 'Y2JB - Gaming']        # the Media row reorders by recent use
    c = Console(row, powers=['awake', 'rest'], ports=[22])
    assert run(c)[0]['state'] == 'linux done'
    assert c.pressed == ['cross']


def test_a_missing_tile_opens_nothing():
    c = Console(['Y2JB - Gaming', 'Prism'], powers=['awake'])
    with pytest.raises(M.Refused, match='not found'):
        run(c, titles=[])
    assert 'cross' not in c.pressed and not c.session


def test_unanswered_ps_press_stops_the_path():
    c = Console(ROW, powers=['awake', 'rest'], ports=[22])
    with pytest.raises(M.Refused, match='Page Timeout'):
        run(c, Bridge('Page Timeout'))


def test_waits_time_out():
    c = Console(ROW, powers=['awake'], ports=[])
    with pytest.raises(M.Refused, match='rest'):
        run(c)


def test_cancel_stops_before_the_next_step():
    c = Console(ROW, powers=['awake', 'rest'], ports=[22])
    c.cancelled = lambda: True
    with pytest.raises(M.Refused, match='cancelled'):
        run(c)
    assert c.pressed == []


@pytest.mark.parametrize('raw,match', [
    ({'linux': {'button': 'Boot Linux', 'steps': []}}, 'no steps'),
    ({'linux': {'button': 'Boot Linux', 'steps': [{'jump': {}}]}}, 'unknown step'),
    ({'linux': {'button': 'Boot Linux', 'steps': [{'open': {}}]}}, 'tile'),
    ({'linux': {'button': 'Boot Linux', 'steps': [{'wait': {'console': 'off'}}]}}, 'console'),
    ({'linux': {'button': 'Boot Linux', 'steps': [{'wait': {'port': 22, 'seconds': 3}}]}}, 'one of'),
    ({'linux': {'button': 'Boot Linux', 'steps': [{'press': {'buttons': ['cross', 'eject']}}]}}, 'eject'),
    ({'Bad Name': {'button': 'x', 'steps': [{'press_ps': {}}]}}, 'name'),
    ({'linux': {'steps': [{'press_ps': {}}]}}, 'button'),
])
def test_bad_paths_are_refused_when_loading(tmp_path, raw, match):
    (tmp_path / 'paths.json').write_text(json.dumps(raw))
    with pytest.raises(M.Refused, match=match):
        M.load_paths(tmp_path)


def test_no_paths_file_means_no_paths_and_the_example_loads(tmp_path):
    assert M.load_paths(tmp_path) == {}
    example = Path(__file__).resolve().parents[1] / 'launcher/paths.example.json'
    (tmp_path / 'paths.json').write_text(example.read_text())
    assert M.load_paths(tmp_path)['linux']['button'] == 'Boot Linux'


def test_titles_are_discovered_with_every_reading():
    c = Console(['Y2JB - Gaming', 'Y2IB - Linux', 'Prism'])
    c.open()
    titles = M.discover_titles(c, tiles=3, reads=2)
    assert [t['title'] for t in titles] == ['Y2JB - Gaming', 'Y2IB - Linux', 'Prism']
    assert 'cross' not in c.pressed


def test_learned_readings_match_their_name_and_no_other():
    assert M.tile_names('Y2JB - Linux', TITLES, ['Y2I)B - Linux']) == {'Y2JB - Linux', 'Y2IB - Linux', 'Y2)B - Linux', 'Y2I)B - Linux'}
    assert M.tile_names('Y2JB - Gaming', TITLES, []) == {'Y2JB - Gaming'}


def test_the_power_menu_goes_up_from_a_remembered_restart_row(tmp_path, monkeypatch):
    # 6 Nov 14:55: Restart PS5 was remembered; shutdown only pressed down and never reached Turn Off PS5.
    c = M.Console({'host': 'example.invalid'}, tmp_path)
    presses = []
    monkeypatch.setattr(c, 'api', lambda path, data=None: presses.append(data['button']) if path == '/api/maker/press' else None)
    monkeypatch.setattr(c, 'wait', lambda _: None)
    monkeypatch.setattr(c, 'snapshot', lambda: b'frame')
    labels = iter([('Games Media', 95), ('Power', 95), ('Power', 95)])
    monkeypatch.setattr(M, 'ocr', lambda *a, **kw: next(labels))
    rows = iter([('Restart PS5', 90), ('Turn Off PS5', 93)])
    monkeypatch.setattr(c, 'shutdown_choice', lambda: next(rows))
    c.shutdown_menu('Turn Off PS5')
    assert presses[-1] == 'up' and 'down' not in presses[3:]


def test_unknown_home_screen_only_ever_presses_back(tmp_path, monkeypatch):
    c = M.Console({'host': 'example.invalid'}, tmp_path)
    calls = []
    monkeypatch.setattr(c, 'api', lambda *a: calls.append(a))
    monkeypatch.setattr(c, 'wait', lambda _: None)
    monkeypatch.setattr(c, 'snapshot', lambda: b'frame')
    monkeypatch.setattr(M, 'ocr', lambda *a, **kw: ('Select a user', 95))
    with pytest.raises(M.Refused, match='Home screen'):
        c.media()
    presses = [a[1]['button'] for a in calls if a[0] == '/api/maker/press']
    assert presses == ['circle', 'circle']


def test_a_dialog_over_home_is_closed_with_back_once(tmp_path, monkeypatch):
    c = M.Console({'host': 'example.invalid'}, tmp_path)
    calls = []
    monkeypatch.setattr(c, 'api', lambda *a: calls.append(a))
    monkeypatch.setattr(c, 'wait', lambda _: None)
    monkeypatch.setattr(c, 'snapshot', lambda: b'frame')
    reads = iter([('A dialog', 90)] + [('Games Media', 95)] * 50)
    monkeypatch.setattr(M, 'ocr', lambda *a, **kw: next(reads))
    c.media()
    presses = [a[1]['button'] for a in calls if a[0] == '/api/maker/press']
    assert presses[0] == 'circle' and presses.count('circle') == 1


class WattsBridge(Bridge):
    def __init__(self, watts): super().__init__(); self.watts = list(watts)
    def request(self, action, timeout=None, **kw):
        if action == 'status':
            self.requests.append(action)
            return {'ok': True, 'plug': 'on', 'watts': self.watts.pop(0) if len(self.watts) > 1 else self.watts[0]}
        return super().request(action, timeout, **kw)


def test_linux_is_up_when_the_ps5_system_is_silent_and_the_plug_reads_running_power():
    # 6 Nov 15:23: Linux reached its login prompt with no network yet, so port 22 never opened.
    path = {'button': 'Boot Linux', 'steps': [{'wait': {'console': 'offline', 'timeout': 60}},
                                              {'wait': {'watts_above': 100, 'timeout': 120}}]}
    c = Console(ROW, powers=['rest', 'offline'])
    bridge = WattsBridge([6.0, 6.0, 150.0])
    result, progress, _ = run(c, bridge, path)
    assert result['state'] == 'linux done'
    assert progress[-1] == 'linux: waiting for more than 100 W'
    assert bridge.requests.count('status') == 3


def test_a_watts_wait_times_out_below_the_line():
    path = {'button': 'x', 'steps': [{'wait': {'watts_above': 100, 'timeout': 10}}]}
    with pytest.raises(M.Refused, match='100 W'):
        run(Console(ROW), WattsBridge([6.0]), path)


def test_discovery_stops_at_the_end_of_the_row_and_drops_junk():
    row = ['Y2JB - Gaming', 'Prism', '* Zi a', '~ Zw a', 'never reached']
    c = Console(row)
    c.title = lambda: (row[c.index], 90 if c.index < 2 else 5)
    c.open()
    titles = M.discover_titles(c, tiles=5, reads=1)
    assert [t['title'] for t in titles] == ['Y2JB - Gaming', 'Prism']
    assert c.index == 3
