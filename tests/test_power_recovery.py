"""A crashed or hung console after a failed automatic jailbreak comes back with no one at it, within strict limits."""
import importlib.util
import json
import threading
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('ps5_autojb', Path(__file__).resolve().parents[1] / 'launcher/ps5_autojb.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class Bridge:
    """Home Assistant's side: the plug and the Bluetooth waker. Records every request."""
    def __init__(self, watts=0.0, plug='on', wakes=('Success',), cycle_ok=True):
        self.watts, self.plug, self.wakes, self.cycle_ok = watts, plug, list(wakes), cycle_ok
        self.requests = []
    def request(self, action, timeout=None, **extra):
        self.requests.append((action, extra))
        if action == 'status':
            return {'ok': True, 'plug': self.plug, 'watts': self.watts, 'waker': 'Ready'}
        if action == 'power_cycle':
            return {'ok': self.cycle_ok, 'message': '' if self.cycle_ok else 'drawing power'}
        if action == 'bt_wake':
            status = self.wakes.pop(0) if self.wakes else 'Page Timeout'
            return {'ok': status == 'Success', 'status': status}
        raise AssertionError(action)
    def actions(self):
        return [a for a, _ in self.requests]


class Console:
    """Discovery replies follow a script; Remote Play is blocked for the first `blocked` checks."""
    def __init__(self, powers, blocked=0):
        self.powers = list(powers)
        self.blocked = blocked
        self.checks = 0
        self.cancelled = lambda: False
        self.waited = 0
    def power(self):
        return self.powers.pop(0) if len(self.powers) > 1 else self.powers[0]
    def wait(self, seconds):
        self.waited += seconds
    def remote_play_ready(self):
        self.checks += 1
        return self.checks > self.blocked


def budgets(tmp_path, now=lambda: 10_000):
    return M.HardBudget(tmp_path / 'hard.json', now=now), M.HungBudget(tmp_path / 'hung.json', now=now)


def run(console, bridge, tmp_path, now=lambda: 10_000):
    progress = []
    hard, hung = budgets(tmp_path, now)
    result = M.hard_recover(console, bridge, progress.append, hard, hung)
    return result, progress


def test_crashed_off_console_wakes_over_bluetooth_without_cutting_power(tmp_path):
    console = Console(['offline'] * 30 + ['awake'])
    bridge = Bridge(watts=0.0)
    result, progress = run(console, bridge, tmp_path)
    assert result == {'state': 'recovered', 'power_cut': False}
    assert bridge.actions() == ['status', 'bt_wake']
    assert progress == ['automatic recovery: waking PS5']


def test_unanswered_wake_power_cycles_the_off_console_then_wakes_again(tmp_path):
    # 6 Nov: after a crash the console ignored Bluetooth pages until mains power was cycled.
    console = Console(['offline'] * 150 + ['awake'])
    bridge = Bridge(watts=0.0, wakes=['Page Timeout', 'Success'])
    result, progress = run(console, bridge, tmp_path)
    assert result == {'state': 'recovered', 'power_cut': True}
    assert bridge.actions() == ['status', 'bt_wake', 'power_cycle', 'bt_wake']
    assert bridge.requests[2] == ('power_cycle', {'hung': False})
    assert progress == ['automatic recovery: waking PS5', 'automatic recovery: power cycling']


def test_hung_console_drawing_power_gets_one_cut_per_day(tmp_path):
    console = Console(['offline'] * 120 + ['awake'])
    bridge = Bridge(watts=62.0)
    result, _ = run(console, bridge, tmp_path)
    assert result == {'state': 'recovered', 'power_cut': True}
    assert bridge.actions()[-2:] == ['power_cycle', 'bt_wake'] and set(bridge.actions()[:-2]) == {'status'}
    assert bridge.requests[-2] == ('power_cycle', {'hung': True})
    assert console.waited >= 180  # three minutes of silence before a hung console is cut
    # A second hang the same day stops before cutting power (and the hard budget is separate).
    (tmp_path / 'hard.json').unlink()
    again = Bridge(watts=62.0)
    with pytest.raises(M.Refused, match='today'):
        run(Console(['offline']), again, tmp_path, now=lambda: 10_000 + 3 * 3600)
    assert 'power_cycle' not in again.actions()


def test_console_answering_again_needs_no_power_action(tmp_path):
    for powers in (['offline'] * 5 + ['awake'], ['offline'] * 40 + ['app']):
        bridge = Bridge(watts=62.0)
        result, _ = run(Console(powers), bridge, tmp_path)
        assert result == {'state': 'answered'}
        assert 'power_cycle' not in bridge.actions() and 'bt_wake' not in bridge.actions()
    assert not (tmp_path / 'hard.json').exists() or json.loads((tmp_path / 'hard.json').read_text()) == []


@pytest.mark.parametrize('plug,watts', [('off', 0.0), ('unavailable', None), ('on', None)])
def test_switched_off_or_unknown_plug_is_left_alone(tmp_path, plug, watts):
    bridge = Bridge(watts=watts, plug=plug)
    with pytest.raises(M.Refused, match='plug'):
        run(Console(['offline']), bridge, tmp_path)
    assert bridge.actions() == ['status']


def test_hard_recovery_runs_at_most_once_in_two_hours_across_restarts(tmp_path):
    run(Console(['offline'] * 30 + ['awake']), Bridge(), tmp_path)
    bridge = Bridge()
    with pytest.raises(M.Refused, match='two hours'):
        run(Console(['offline'] * 30 + ['awake']), bridge, tmp_path, now=lambda: 10_000 + 3600)
    assert bridge.actions() == []
    run(Console(['offline'] * 30 + ['awake']), Bridge(), tmp_path, now=lambda: 10_000 + 7300)


def test_console_that_never_returns_is_reported(tmp_path):
    bridge = Bridge(watts=0.0, wakes=['Page Timeout', 'Page Timeout'])
    with pytest.raises(M.Refused, match='did not come back'):
        run(Console(['offline']), bridge, tmp_path)
    assert bridge.actions().count('power_cycle') == 1


def test_cancel_stops_before_any_power_action(tmp_path):
    console = Console(['offline'])
    console.cancelled = lambda: True
    bridge = Bridge()
    with pytest.raises(M.Refused, match='cancelled'):
        run(console, bridge, tmp_path)
    assert bridge.actions() == []


def test_unknown_local_network_is_not_mistaken_for_a_crash(tmp_path):
    bridge = Bridge()
    with pytest.raises(M.Refused, match='network'):
        run(Console(['offline'] * 3 + ['unknown']), bridge, tmp_path)
    assert bridge.actions() == []


def test_blocking_warning_screen_asks_for_x_then_continues_by_itself():
    console = Console(['awake'], blocked=2)
    progress = []
    M.await_remote_play(console, progress.append)
    assert progress == ['automatic recovery: press X on the PS5']
    assert console.checks == 3


def test_warning_screen_left_for_half_an_hour_is_reported():
    console = Console(['awake'], blocked=10_000)
    with pytest.raises(M.Refused, match='30 minutes'):
        M.await_remote_play(console, lambda _: None)


def test_bridge_matches_replies_by_id_and_times_out():
    sent = []
    bridge = M.Bridge(sent.append)
    def reply():
        while not sent: pass
        request = json.loads(sent[0])
        bridge.receive(json.dumps({'id': 'not-this-one', 'ok': True}))
        bridge.receive(b'\xff')
        bridge.receive(json.dumps({'id': request['id'], 'ok': True, 'watts': 1.5}))
    threading.Thread(target=reply).start()
    assert bridge.request('status', 2)['watts'] == 1.5
    request = json.loads(sent[0])
    assert request['action'] == 'status' and len(request['id']) == 32 and isinstance(request['at'], float)
    with pytest.raises(M.Refused, match='did not answer'):
        bridge.request('bt_wake', 0.05)


def failing_launcher(tmp_path, monkeypatch, outputs, bridge, hard_result):
    monkeypatch.setattr(M, 'operate', lambda *a, **kw: {'state': 'failed: no confirmation', 'launch_attempted': True})
    monkeypatch.setattr(M, 'operate_restart', lambda *a, **kw: {'state': 'refused', 'message': 'Selected Power option is unclear; no shutdown'})
    calls = []
    def operate_hard(config, directory, bridge_, progress, cancelled=None):
        calls.append(bridge_)
        progress('automatic recovery: waking PS5')
        if isinstance(hard_result, Exception):
            raise hard_result
        return hard_result
    monkeypatch.setattr(M, 'operate_hard', operate_hard, raising=False)
    s = M.Launcher({}, tmp_path, outputs.append, bridge=bridge)
    s.set_enabled(True)
    return s, calls


def test_failed_restart_then_crash_recovers_and_rearms_the_retry(tmp_path, monkeypatch):
    outputs, bridge = [], Bridge()
    s, calls = failing_launcher(tmp_path, monkeypatch, outputs, bridge, {'state': 'recovered', 'power_cut': True})
    s.auto.consume()
    s.execute('automatic')
    assert calls == [bridge]
    assert [o['state'] for o in outputs][-2:] == ['automatic recovery: waking PS5', 'automatic retry waiting']
    assert s.auto.state['handled'] is False


def test_console_still_answering_keeps_the_original_failure(tmp_path, monkeypatch):
    outputs = []
    s, _ = failing_launcher(tmp_path, monkeypatch, outputs, Bridge(), {'state': 'answered'})
    s.execute('automatic')
    assert outputs[-1]['state'] == 'failed: Selected Power option is unclear; no shutdown'


def test_hard_recovery_refusal_is_reported(tmp_path, monkeypatch):
    outputs = []
    s, _ = failing_launcher(tmp_path, monkeypatch, outputs, Bridge(), M.Refused('PS5 did not come back'))
    s.execute('automatic')
    assert outputs[-1]['state'] == 'failed: PS5 did not come back'


def test_manual_and_pre_input_failures_never_power_cycle(tmp_path, monkeypatch):
    outputs = []
    s, calls = failing_launcher(tmp_path, monkeypatch, outputs, Bridge(), {'state': 'recovered', 'power_cut': True})
    s.execute('manual')
    monkeypatch.setattr(M, 'operate', lambda *a, **kw: {'state': 'refused: timed out', 'launch_attempted': False})
    s.execute('automatic')
    assert calls == []


def test_without_a_bridge_failures_stay_as_before(tmp_path, monkeypatch):
    outputs = []
    monkeypatch.setattr(M, 'operate', lambda *a, **kw: {'state': 'failed: no confirmation', 'launch_attempted': True})
    monkeypatch.setattr(M, 'operate_restart', lambda *a, **kw: {'state': 'refused', 'message': 'unclear'})
    monkeypatch.setattr(M, 'operate_hard', lambda *a, **kw: pytest.fail('no bridge'), raising=False)
    s = M.Launcher({}, tmp_path, outputs.append)
    s.set_enabled(True)
    s.execute('automatic')
    assert outputs[-1]['state'] == 'failed: unclear'


def test_cancel_during_recovery_is_reported_as_cancelled(tmp_path, monkeypatch):
    outputs = []
    s, _ = failing_launcher(tmp_path, monkeypatch, outputs, Bridge(), {'state': 'recovered', 'power_cut': False})
    real = M.operate_hard
    def cancel_then_recover(*a, **kw):
        s.set_enabled(False)
        return real(*a, **kw)
    monkeypatch.setattr(M, 'operate_hard', cancel_then_recover)
    s.execute('automatic')
    assert outputs[-1]['state'] == 'automatic launch cancelled'


def test_real_remote_play_check_never_takes_a_busy_session_and_closes_a_failed_open(tmp_path, monkeypatch):
    c = M.Console({'host': 'example.invalid'}, tmp_path)
    calls = []
    def api(path, data=None, state={'holder': None, 'open': False}, fail=[True]):
        calls.append(path)
        if path == '/api/maker':
            return state
        if path == '/api/maker/open' and fail[0]:
            fail[0] = False
            raise TimeoutError('timed out')  # what the warning screen did on 6 Nov
        return {}
    monkeypatch.setattr(c, 'api', api)
    assert not c.remote_play_ready()
    assert calls == ['/api/maker', '/api/maker/open', '/api/maker/close']
    calls.clear()
    assert c.remote_play_ready()
    assert calls == ['/api/maker', '/api/maker/open', '/api/maker/close']
    monkeypatch.setattr(c, 'api', lambda path, data=None: calls.append(path) or {'holder': 'someone', 'open': True})
    calls.clear()
    with pytest.raises(M.Refused, match='busy'):
        c.remote_play_ready()
    assert calls == ['/api/maker']


class Tv:
    def __init__(self, works=True):
        self.works, self.presses = works, 0
    def press_x(self):
        self.presses += 1
        return self.works


def test_tv_presses_x_on_the_warning_screen_so_no_one_is_asked():
    console, tv, progress = Console(['awake'], blocked=1), Tv(), []
    M.await_remote_play(console, progress.append, tv)
    assert tv.presses == 1 and progress == []


def test_two_tv_presses_then_the_owner_is_asked_once():
    console, tv, progress = Console(['awake'], blocked=5), Tv(), []
    M.await_remote_play(console, progress.append, tv)
    assert tv.presses == 2 and progress == ['automatic recovery: press X on the PS5']


def test_unreachable_tv_asks_the_owner_straight_away():
    console, tv, progress = Console(['awake'], blocked=2), Tv(works=False), []
    M.await_remote_play(console, progress.append, tv)
    assert tv.presses == 1 and progress == ['automatic recovery: press X on the PS5']


def test_tv_remote_runs_only_the_forced_x_command_over_a_pinned_key(tmp_path, monkeypatch):
    key = tmp_path / 'tv-cec' / 'id_ed25519'
    monkeypatch.setattr(M, 'TV_HOST', '192.0.2.39')
    assert M.TvRemote.installed(tmp_path) is None
    key.parent.mkdir()
    key.write_text('k')
    runs = []
    def run(args, **kw):
        runs.append((args, kw))
        return type('R', (), {'returncode': 0})()
    tv = M.TvRemote.installed(tmp_path)
    tv.run = run
    assert tv.press_x()
    args, kw = runs[0]
    assert args[0] == 'ssh' and args[-2:] == ['root@192.0.2.39', 'x']
    assert ['-i', str(key)] == args[1:3]
    assert 'StrictHostKeyChecking=yes' in args and f'UserKnownHostsFile={key.parent / "known_hosts"}' in args
    assert 'BatchMode=yes' in args and kw['timeout'] <= 30
    tv.run = lambda *a, **kw: type('R', (), {'returncode': 3})()
    assert not tv.press_x()
    def timeout(*a, **kw):
        raise M.subprocess.TimeoutExpired('ssh', 20)
    tv.run = timeout
    assert not tv.press_x()


def test_rest_mode_is_woken_but_never_power_cut(tmp_path):
    # 6 Nov 12:05: the console went to rest (3.7 W) during a launch; 3 W counted that as hung and would have cut it.
    bridge = Bridge(watts=3.7, wakes=['Page Timeout', 'Page Timeout'])
    with pytest.raises(M.Refused, match='rest'):
        run(Console(['offline'] * 5 + ['rest']), bridge, tmp_path)
    assert 'power_cycle' not in bridge.actions() and bridge.actions().count('bt_wake') == 1


def test_rest_mode_wakes_over_bluetooth(tmp_path):
    bridge = Bridge(watts=3.7)
    result, progress = run(Console(['rest'] * 30 + ['awake']), bridge, tmp_path)
    assert result == {'state': 'recovered', 'power_cut': False}
    assert bridge.actions() == ['status', 'bt_wake'] and progress == ['automatic recovery: waking PS5']


@pytest.mark.parametrize('watts', [2.5, 9.0, 29.0])
def test_silent_console_at_standby_power_is_only_woken(tmp_path, watts):
    bridge = Bridge(watts=watts, wakes=['Page Timeout'])
    with pytest.raises(M.Refused, match='no power cut'):
        run(Console(['offline']), bridge, tmp_path)
    assert 'power_cycle' not in bridge.actions()



def test_crash_reading_still_lit_waits_for_the_plug_to_catch_up(tmp_path):
    # The Tapo reading lags about 30 s: right after a panic it can still show the running console's watts.
    class LaggingBridge(Bridge):
        def request(self, action, timeout=None, **extra):
            if action == 'status' and self.actions().count('status') >= 3:
                self.watts = 0.0
            return super().request(action, timeout, **extra)
    console = Console(['offline'] * 40 + ['awake'])
    bridge = LaggingBridge(watts=71.0)
    result, _ = run(console, bridge, tmp_path)
    assert result == {'state': 'recovered', 'power_cut': False}
    assert bridge.actions()[-1] == 'bt_wake' and 'power_cycle' not in bridge.actions()
    assert console.waited < 90


def test_off_console_is_woken_within_about_twenty_seconds(tmp_path):
    console = Console(['offline'] * 10 + ['awake'])
    bridge = Bridge(watts=0.0)
    result, _ = run(console, bridge, tmp_path)
    assert result == {'state': 'recovered', 'power_cut': False} and bridge.actions() == ['status', 'bt_wake']
    assert console.waited <= 22



def test_a_busy_remote_play_session_is_never_mistaken_for_the_warning_screen():
    # Someone else's session must not get a blind X from the TV.
    class Busy(Console):
        def remote_play_ready(self):
            raise M.Refused('Remote Play is busy; no X pressed')
    tv, progress = Tv(), []
    with pytest.raises(M.Refused, match='busy'):
        M.await_remote_play(Busy(['awake']), progress.append, tv)
    assert tv.presses == 0 and progress == []
