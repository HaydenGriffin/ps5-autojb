"""The Home Assistant package parses, uses the launcher's discovery ids and the default MQTT prefix."""
import importlib.util
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = yaml.safe_load((ROOT / 'homeassistant/ps5_autojb.yaml').read_text())
TEXT = (ROOT / 'homeassistant/ps5_autojb.yaml').read_text()
SPEC = importlib.util.spec_from_file_location('ps5_autojb', ROOT / 'launcher/ps5_autojb.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
HARDWARE = {'switch.ps5_power_plug', 'sensor.ps5_power_plug_current_power', 'button.ps5_bt_waker_power_on',
            'sensor.ps5_bt_waker_status'}


def test_every_ps5_autojb_entity_comes_from_discovery_or_the_package():
    discovered = {config['default_entity_id'] for _, config in M.discovery()}
    own = {s['default_entity_id'] for s in PACKAGE['mqtt']['sensor']} | {f'script.{k}' for k in PACKAGE['script']}
    used = set(re.findall(r'\b(?:sensor|binary_sensor|button|switch|script)\.ps5_autojb_[a-z_]+', TEXT))
    assert used <= discovered | own


def test_hardware_entities_are_the_documented_ones():
    used = set(re.findall(r'\b(?:sensor|button|switch)\.ps5_(?:power_plug|bt_waker)[a-z_]*', TEXT))
    assert used == HARDWARE


def test_topics_use_the_default_prefix_and_automation_ids_are_unique():
    topics = set(re.findall(r'\b([a-z0-9_]+)/(?:recover|shutdown)\b', TEXT))
    assert topics == {M.PREFIX}
    ids = [a['id'] for a in PACKAGE['automation']]
    assert len(ids) == len(set(ids))


def test_selectors_are_never_empty_dicts():
    # Home Assistant's package merge drops empty dicts; a field selector of {} arrives empty.
    assert '{}' not in TEXT.replace('sequence: []', '')


def test_docs_name_only_entities_that_exist_and_their_yaml_parses():
    """The docs (entity tables, the dashboard card, the TV example) can't drift from the launcher and the package."""
    discovered = {config['default_entity_id'] for _, config in M.discovery(json.loads(
        (ROOT / 'launcher/paths.example.json').read_text()))}
    own = {s['default_entity_id'] for s in PACKAGE['mqtt']['sensor']} | {f'script.{k}' for k in PACKAGE['script']}
    docs = [ROOT / 'README.md', ROOT / 'llms.txt', *sorted((ROOT / 'docs').rglob('*.md'))]
    for doc in docs:
        text = doc.read_text()
        used = set(re.findall(r'\b(?:sensor|binary_sensor|button|switch|script)\.ps5_autojb_[a-z_]+', text))
        used = {u for u in used if not u.endswith('_path_')}  # the button.ps5_autojb_path_<name> pattern
        assert used <= discovered | own | HARDWARE, (doc.name, used - discovered - own)
        for block in re.findall(r'```yaml\n(.*?)```', text, re.S):
            yaml.load(block, HaLoader)


class HaLoader(yaml.SafeLoader):
    """Accepts Home Assistant's tags (!include_dir_named, !secret) as plain values."""


HaLoader.add_multi_constructor('!', lambda loader, tag, node: loader.construct_scalar(node))
