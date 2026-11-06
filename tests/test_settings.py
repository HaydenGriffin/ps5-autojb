"""autojb.json makes the target app, MQTT prefix and hosts configurable; discovery gives Home Assistant fixed ids."""
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('ps5_autojb', Path(__file__).resolve().parents[1] / 'launcher/ps5_autojb.py')


@pytest.fixture
def M():
    module = importlib.util.module_from_spec(SPEC)  # fresh module: settings are process-wide
    SPEC.loader.exec_module(module)
    return module


def test_target_app_is_configurable_and_matched_exactly(M):
    assert M.exact_target('Y2JB - Gaming', 90)
    M.configure({'target_name': 'GoldHEN Loader'})
    assert M.exact_target('GoldHEN  Loader', 90) and M.exact_target('goldhen-loader', 90)
    assert not M.exact_target('Y2JB - Gaming', 99)
    assert not M.exact_target('GoldHEN Loader Tools', 99)
    assert not M.exact_target('GoldHEN Loader', 40)


def test_unknown_settings_are_refused(M):
    with pytest.raises(M.Refused, match='target_nmae'):
        M.configure({'target_nmae': 'typo'})


def test_example_settings_are_all_known(M):
    example = json.loads((Path(__file__).resolve().parents[1] / 'launcher/autojb.example.json').read_text())
    M.configure(example)
    assert M.PREFIX == example['mqtt_prefix'] and M.TV_HOST == example['tv_host']


def test_discovery_uses_the_prefix_and_fixed_entity_ids(M):
    M.configure({'mqtt_prefix': 'den_ps5'})
    found = {config['default_entity_id']: (topic, config) for topic, config in M.discovery()}
    assert set(found) == {'button.ps5_autojb_launch', 'sensor.ps5_autojb_result', 'binary_sensor.ps5_autojb_ready',
                          'switch.ps5_autojb_auto', 'sensor.ps5_autojb_power', 'button.ps5_autojb_discover_titles',
                          'sensor.ps5_autojb_titles'}
    with_paths = {c['default_entity_id']: c for _, c in M.discovery({'linux': {'button': 'Boot Linux', 'steps': []}})}
    assert with_paths['button.ps5_autojb_path_linux']['command_topic'] == 'den_ps5/path/linux'
    assert found['switch.ps5_autojb_auto'][1]['command_topic'] == 'den_ps5/auto/set'
    assert found['sensor.ps5_autojb_power'][1]['options'] == ['awake', 'app', 'rest', 'offline', 'unknown']
    assert all(c['availability_topic'] == 'den_ps5/availability' for _, c in found.values())


def test_tv_press_needs_both_a_key_and_a_tv_host(M, tmp_path):
    (tmp_path / 'tv-cec').mkdir()
    (tmp_path / 'tv-cec' / 'id_ed25519').write_text('k')
    assert M.TvRemote.installed(tmp_path) is None
    M.configure({'tv_host': 'lg-tv.lan'})
    assert M.TvRemote.installed(tmp_path).host == 'lg-tv.lan'


def test_console_power_is_reported_for_home_assistants_rest_guard(M, tmp_path, monkeypatch):
    reported = []
    monkeypatch.setattr(M.Console, 'probe', lambda self: False)
    monkeypatch.setattr(M.Console, 'power', lambda self: 'rest')
    s = M.Launcher({'host': 'ps5.invalid'}, tmp_path, lambda r: None, ready=lambda v: None, console_power=reported.append)
    s.tick()
    assert reported == ['rest']


def test_preview_link_uses_the_public_url(M, tmp_path, monkeypatch):
    M.configure({'public_url': 'http://helper.lan:8091/'})
    c = M.Console({'host': 'ps5.invalid'}, tmp_path)
    monkeypatch.setattr(c, 'open', lambda: None)
    monkeypatch.setattr(c, 'close', lambda: None)
    monkeypatch.setattr(c, 'snapshot', lambda: b'png')
    assert c.ready_preview().startswith('http://helper.lan:8091/api/screenshots/autojb-ready.png?v=')


def test_probe_port_defaults_to_refrees_own_default(M, tmp_path):
    """Without probe_port in refree's config.json, refree probes 9021, so the launcher must too."""
    assert M.Console({'host': '192.0.2.5'}, tmp_path).port == 9021
    assert M.Console({'host': '192.0.2.5', 'probe_port': 3232}, tmp_path).port == 3232


class FakePaho:
    def __init__(self, v2):
        if v2:
            self.CallbackAPIVersion = type('CallbackAPIVersion', (), {'VERSION2': 'v2'})
        self.made = []

    def Client(self, *args, **kwargs):
        self.made.append((args, kwargs))
        return object()


@pytest.mark.parametrize('v2, args', [(True, ('v2',)), (False, ())])
def test_mqtt_client_works_with_paho_1_and_2(M, v2, args):
    paho = FakePaho(v2)
    M.mqtt_client(paho)
    assert paho.made == [(args, {'client_id': 'ps5-autojb'})]
