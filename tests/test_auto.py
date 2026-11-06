"""Automatic launch is once per wake, durable, cancellable and never a reboot loop."""
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('ps5_autojb', Path(__file__).resolve().parents[1] / 'launcher/ps5_autojb.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def gate(tmp_path):
    clock = [100.0]
    g = M.AutoBoot(tmp_path / 'auto.json', now=lambda: clock[0])
    g.set_enabled(True)
    return g, clock


def test_boot_waits_then_claims_once_and_survives_helper_restart(tmp_path):
    g, clock = gate(tmp_path)
    assert g.observe('awake', False) is None
    clock[0] += 29
    assert g.observe('awake', False) is None
    clock[0] += 1
    assert g.observe('awake', False) == 'launch'
    restarted = M.AutoBoot(g.path, now=lambda: clock[0])
    clock[0] += 3000
    assert restarted.enabled
    assert restarted.observe('awake', False) is None


def test_only_sustained_offline_or_standby_rearms(tmp_path):
    g, clock = gate(tmp_path)
    assert g.observe('awake', True) == 'already jailbroken'
    assert g.observe('offline', False) is None
    assert g.observe('awake', False) is None  # one dropped packet cannot rearm
    for _ in range(3):
        g.observe('offline', False)
    assert g.observe('awake', False) is None
    clock[0] += 30
    assert g.observe('awake', False) == 'launch'
    g.observe('rest', False)
    assert g.observe('awake', True) == 'already jailbroken'  # preserved jailbreak on resume


def test_game_running_consumes_wake_instead_of_launching_later(tmp_path):
    g, clock = gate(tmp_path)
    assert g.observe('app', False) == 'automatic skipped: an app is running'
    clock[0] += 100
    assert g.observe('awake', False) is None


def test_disabled_and_unknown_discovery_never_launch(tmp_path):
    g, clock = gate(tmp_path)
    g.set_enabled(False)
    assert not M.AutoBoot(g.path).enabled
    assert g.observe('awake', False) is None
    g.set_enabled(True)
    g.observe('awake', False)
    clock[0] += 30
    assert g.observe('unknown', False) is None
    assert g.observe('awake', False) is None
    clock[0] += 30
    assert g.observe('awake', False) == 'launch'


def test_repeated_on_command_does_not_rearm(tmp_path):
    g, clock = gate(tmp_path)
    g.consume()
    g.set_enabled(True)
    clock[0] += 300
    assert g.observe('awake', False) is None


def test_invalid_persistent_state_fails_closed(tmp_path):
    path = tmp_path / 'auto.json'
    path.write_text('{"enabled":"true","handled":false}')
    with pytest.raises(M.Refused):
        M.AutoBoot(path)


def test_automatic_failure_is_reported_once_and_budget_shared_with_manual(tmp_path, monkeypatch):
    outputs = []
    clock = [0]
    runs = []
    def operate(config, directory, focus_only=False, cancelled=None):
        runs.append(cancelled)
        return {'state': 'failed: no confirmation', 'launched': None}
    monkeypatch.setattr(M, 'operate', operate)
    service = M.Launcher({}, tmp_path, outputs.append, now=lambda: clock[0])
    service.set_enabled(True)
    service.tick('awake', False)
    clock[0] = 30
    service.tick('awake', False)
    clock[0] = 3000
    service.tick('awake', False)
    assert len(runs) == 1 and callable(runs[0])
    assert outputs[-1]['source'] == 'automatic'
    assert outputs[-1]['state'] == 'failed: no confirmation'
    assert json.loads(service.auto.path.read_text())['handled'] is True


def test_manual_launch_consumes_pending_automatic_and_is_serialized(tmp_path, monkeypatch):
    runs = []
    monkeypatch.setattr(M, 'operate', lambda *a, **kw: runs.append(kw) or {'state': 'jailbreak confirmed', 'launched': True})
    service = M.Launcher({}, tmp_path, lambda r: None, now=lambda: 100)
    service.set_enabled(True)
    with service.busy:
        service.manual()
        service.tick('awake', False)
    assert not runs
    service.manual()
    service.tick('awake', False)
    assert len(runs) == 1


def test_switch_off_cancels_before_next_controller_request(tmp_path, monkeypatch):
    service = M.Launcher({}, tmp_path, lambda r: None)
    service.set_enabled(True)
    console = M.Console({'host': 'example.invalid'}, tmp_path)
    console.cancelled = service.cancelled.is_set
    calls = []
    monkeypatch.setattr(M.urllib.request, 'urlopen', lambda *a, **kw: calls.append(a))
    service.set_enabled(False)
    with pytest.raises(M.Refused, match='disabled'):
        console.api('/api/maker/press', {'button': 'cross'})
    assert calls == []


def test_three_failed_launches_block_after_real_reboots(tmp_path, monkeypatch):
    # Exercise the real operate -> launch -> Budget path, not a second model of the limit.
    from test_launch import Console
    consoles = []
    def factory(*args):
        c = Console([('Y2JB - Gaming', 90)], confirmed=False)
        consoles.append(c)
        return c
    monkeypatch.setattr(M, 'Console', factory)
    # Recovery has its own guard/budget tests; here real user reboots exercise the shared launch budget.
    monkeypatch.setattr(M, 'operate_restart', lambda *a, **kw: {'state': 'refused', 'message': 'not testing recovery'})
    clock = [100]
    results = []
    service = M.Launcher({}, tmp_path, results.append, now=lambda: clock[0])
    service.set_enabled(True)
    for _ in range(4):
        service.tick('rest', False)
        service.tick('awake', False)
        clock[0] += 30
        service.tick('awake', False)
    assert [len(c.launched) for c in consoles] == [1, 1, 1, 0]
    assert 'Three failed attempts' in results[-1]['state']


def test_discovery_ignores_wrong_peer_and_malformed_or_binary_packets(tmp_path, monkeypatch):
    response = [b'HTTP/1.1 200 Ok\nrunning-app-name:Game\n', ('192.0.2.5', 9302)]
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def settimeout(self, *a): pass
        def sendto(self, *a): pass
        def recvfrom(self, *a): return tuple(response)
    monkeypatch.setattr(M.socket, 'socket', lambda *a: Socket())
    console = M.Console({'host': '192.0.2.5'}, tmp_path)
    assert console.power() == 'app'
    response[0] = b'HTTP/1.1 200 Ok\n'
    assert console.power() == 'awake'
    response[0] = b'HTTP/1.1 620 Server Standby\n'
    assert console.power() == 'rest'
    response[0] = b'\xff\x00\x01'
    assert console.power() == 'unknown'
    response[:] = [b'HTTP/1.1 200 Ok\n', ('192.0.2.6', 9302)]
    assert console.power() == 'unknown'


def test_ha_switch_discovery_and_retained_commands_cannot_enable_auto(tmp_path, monkeypatch):
    import sys
    import types
    sent = []
    class Client:
        def __init__(self, **kwargs): pass
        def username_pw_set(self, *a): pass
        def will_set(self, *a, **kw): pass
        def connect(self, *a, **kw): pass
        def subscribe(self, topics):
            assert (M.PREFIX + '/auto/set', 0) in topics
        def publish(self, topic, payload, **kw): sent.append((topic, payload))
        def loop_forever(self):
            self.on_connect(self, None, {}, 0)
            topic = M.PREFIX + '/auto/set'
            for payload, retained, expected in [(b'ON', True, False), (b'ON', False, True), (b'OFF', False, False)]:
                self.on_message(self, None, types.SimpleNamespace(topic=topic, payload=payload, retain=retained))
                assert M.AutoBoot(tmp_path / 'launcher-auto.json').enabled is expected
    mqtt = types.ModuleType('paho.mqtt.client')
    mqtt.Client = Client
    parent = types.ModuleType('paho.mqtt')
    parent.client = mqtt
    paho = types.ModuleType('paho')
    paho.mqtt = parent
    for name, module in [('paho', paho), ('paho.mqtt', parent), ('paho.mqtt.client', mqtt)]:
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(M.threading, 'Thread', lambda **kw: types.SimpleNamespace(start=lambda: None))
    M.serve({'mqtt': {'host': 'broker.invalid', 'username': 'test', 'password': 'test'}}, tmp_path)
    config = json.loads(next(p for t, p in sent if t == 'homeassistant/switch/ps5_autojb_auto/config'))
    assert config['command_topic'] == M.PREFIX + '/auto/set'
    assert config['state_topic'] == M.PREFIX + '/auto/state'
    assert [p for t, p in sent if t == M.PREFIX + '/auto/state'] == ['OFF', 'ON', 'OFF']


def test_local_network_failure_does_not_look_like_console_poweroff(tmp_path, monkeypatch):
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def settimeout(self, *a): pass
        def sendto(self, *a): raise OSError('Network is unreachable')
    monkeypatch.setattr(M.socket, 'socket', lambda *a: Socket())
    assert M.Console({'host': '192.0.2.5'}, tmp_path).power() == 'unknown'


def test_readiness_stays_live_when_auto_is_off_or_manual_launcher_busy(tmp_path, monkeypatch):
    from types import SimpleNamespace
    readings = iter([True, False])
    monkeypatch.setattr(M, 'Console', lambda *a: SimpleNamespace(probe=lambda: next(readings)))
    clock = [0]
    published = []
    service = M.Launcher({}, tmp_path, lambda r: pytest.fail('No launch requested'),
                         now=lambda: clock[0], ready=published.append)
    assert not service.auto.enabled
    service.tick()
    clock[0] = 2
    service.tick()
    assert published == [True]
    clock[0] = 5
    with service.busy:
        service.tick()
    assert published == [True, False]
