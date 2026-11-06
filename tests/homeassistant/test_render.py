"""The package renders in a real Home Assistant: Android notification data, the power steps, the shutdown request."""
import json
import re
from pathlib import Path

import yaml
from homeassistant.core import ServiceCall
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

PKG = yaml.safe_load((Path(__file__).resolve().parents[2] / 'homeassistant/ps5_autojb.yaml').read_text())


async def setup(hass, calls, scripts=None):
    async def record(call: ServiceCall):
        data = dict(call.data)
        if 'payload' in data:  # what mqtt.publish's cv.string does with a rendered template
            data['payload'] = getattr(data['payload'], 'render_result', data['payload'])
        calls.append((f'{call.domain}.{call.service}', data))
    for dom, svc in [('mqtt', 'publish'), ('notify', 'mobile_app_your_phone'), ('button', 'press'),
                     ('switch', 'turn_off'), ('switch', 'turn_on')]:
        hass.services.async_register(dom, svc, record)
    assert await async_setup_component(hass, 'script', {'script': {**PKG['script'], **(scripts or {})}})
    assert await async_setup_component(hass, 'automation', {'automation': PKG['automation']})
    await hass.async_block_till_done()


async def test_shutdown_request_is_fresh_32_hex(hass):
    calls = []
    await setup(hass, calls)
    await hass.services.async_call('script', 'ps5_autojb_full_shutdown', {}, blocking=True)
    (svc, data), = calls
    payload = json.loads(data['payload'])
    assert svc == 'mqtt.publish' and data['topic'] == 'ps5_autojb/shutdown'
    assert re.fullmatch(r'[a-f0-9]{32}', payload['id']) and abs(payload['at'] - dt_util.utcnow().timestamp()) < 5


async def test_notify_levels_render_android_data(hass):
    calls = []
    await setup(hass, calls)
    for level in ('status', 'ask', 'done'):
        await hass.services.async_call('script', 'ps5_autojb_notify',
                                       {'title': 't', 'message': 'm', 'level': level, 'image': 'http://x/y.png' if level == 'done' else ''}, blocking=True)
    await hass.services.async_call('script', 'ps5_autojb_notify', {'clear': True}, blocking=True)
    datas = [d for s, d in calls if s == 'notify.mobile_app_your_phone']
    assert datas[0]['data']['timeout'] == 300 and datas[0]['data']['tag'] == 'ps5-autojb'
    assert all(d['data']['notification_icon'] == 'mdi:sony-playstation' for d in datas[:3])
    assert datas[1]['data']['actions'][0]['action'] == 'PS5_AUTOJB_RUN'
    assert datas[2]['data']['image'] == 'http://x/y.png' and 'timeout' not in datas[2]['data']
    assert datas[3] == {'message': 'clear_notification', 'data': {'tag': 'ps5-autojb'}}


async def test_recovery_status_step_answers(hass):
    calls = []
    await setup(hass, calls)
    hass.states.async_set('switch.ps5_power_plug', 'on')
    hass.states.async_set('sensor.ps5_power_plug_current_power', '0.0')
    hass.states.async_set('sensor.ps5_bt_waker_status', 'Ready')
    rid = 'a' * 32
    hass.states.async_set('sensor.ps5_autojb_recovery_request', rid,
                          {'id': rid, 'at': dt_util.utcnow().timestamp(), 'action': 'status'})
    await hass.async_block_till_done()
    reply = json.loads(calls[-1][1]['payload'])
    assert reply == {'id': rid, 'action': 'status', 'ok': True, 'plug': 'on', 'watts': 0.0, 'waker': 'Ready'}


async def test_power_cycle_refused_in_rest(hass):
    calls = []
    await setup(hass, calls)
    hass.states.async_set('switch.ps5_power_plug', 'on')
    hass.states.async_set('sensor.ps5_power_plug_current_power', '0.4')
    hass.states.async_set('sensor.ps5_autojb_power', 'rest')
    rid = 'b' * 32
    hass.states.async_set('sensor.ps5_autojb_recovery_request', rid,
                          {'id': rid, 'at': dt_util.utcnow().timestamp(), 'action': 'power_cycle'})
    await hass.async_block_till_done()
    assert not [c for c in calls if c[0].startswith('switch.')]
    assert 'rest' in json.loads(calls[-1][1]['payload'])['message']


async def test_failure_notifies_with_run_button(hass):
    calls = []
    await setup(hass, calls)
    hass.states.async_set('sensor.ps5_autojb_result', 'checking PS5', {'source': 'automatic', 'at': '1'})
    await hass.async_block_till_done()
    hass.states.async_set('sensor.ps5_autojb_result', 'failed: no confirmation', {'source': 'automatic', 'at': '2'})
    await hass.async_block_till_done()
    asks = [d for s, d in calls if s == 'notify.mobile_app_your_phone' and 'actions' in d['data']]
    assert asks and 'no confirmation' in asks[-1]['message']


async def test_ready_passes_the_screenshot_to_the_tv_notice(hass):
    """The ready message hands the TV hook its picture; the docs' wiring example turns it into an image URL."""
    calls = []

    async def tv(call: ServiceCall):
        calls.append(('tv', dict(call.data)))
    hass.services.async_register('notify', 'your_android_tv', tv)
    example = (Path(__file__).resolve().parents[2] / 'docs/notifications.md').read_text()
    example = example.split('```yaml\n', 1)[1].split('```', 1)[0]
    await setup(hass, calls, scripts=yaml.safe_load(example))
    hass.states.async_set('sensor.ps5_autojb_result', 'checking PS5', {'source': 'automatic'})
    await hass.async_block_till_done()
    hass.states.async_set('sensor.ps5_autojb_result', 'jailbreak confirmed',
                          {'launched': True, 'at': 'now', 'preview': 'http://x/ready.png'})
    await hass.async_block_till_done()
    tv = [d for s, d in calls if s == 'tv']
    assert tv[-1]['title'] == 'PS5 ready' and tv[-1]['data'] == {'image': {'url': 'http://x/ready.png'}}
