#!/usr/bin/env python3
"""ps5-autojb: keep a PS5 jailbroken by launching the target app through refree's Remote Play, by its on-screen name.

Runs next to refree (https://gitlab.com/refree/refree) and talks to Home Assistant over MQTT only.
- Finds the target tile by OCR of its title, never by position, and presses Cross only after two matching frames.
- A failed automatic launch gets one verified normal-menu restart (Power menu read by OCR, never a full shutdown).
- If the console crashed off or hung, Home Assistant wakes it with an ESP32 Bluetooth waker or cycles its smart
  plug (never in rest mode or at standby power), within durable daily limits.
- After an unclean power-off, an optional rooted LG TV presses X over HDMI-CEC on the "wasn't turned off properly"
  screen; otherwise the owner is asked.
Settings: an optional autojb.json next to refree's config.json (see autojb.example.json).
Ubuntu dependencies: python3-pil, python3-paho-mqtt, tesseract-ocr-eng.
"""
import argparse
import csv
import fcntl
import io
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
import urllib.request
import uuid

def normalize(text):
    return re.sub(r'[\s\-–—]', '', text.casefold())


# Defaults; configure() overrides them from autojb.json at startup.
TARGET_NAME = 'Y2JB - Gaming'      # the app tile to open, as its title reads on screen
TARGET = normalize(TARGET_NAME)
PREFIX = 'ps5_autojb'              # MQTT topic prefix
MAX_TILES = 24                     # how far along the Media row to look
REMOTE_PLAY_URL = 'http://127.0.0.1:8091'  # refree's web API
PUBLIC_URL = ''                    # how Home Assistant reaches refree's screenshots, e.g. http://helper.lan:8091
TV_HOST = ''                       # rooted LG TV for the CEC X press; empty disables it


def configure(settings):
    """Apply autojb.json. Unknown keys are refused so a typo can't silently keep a default."""
    global TARGET_NAME, TARGET, PREFIX, MAX_TILES, REMOTE_PLAY_URL, PUBLIC_URL, TV_HOST, OFF_WATTS, HUNG_WATTS
    known = {'target_name', 'mqtt_prefix', 'max_tiles', 'remote_play_url', 'public_url', 'tv_host',
             'off_watts', 'hung_watts'}
    unknown = set(settings) - known
    if unknown:
        raise Refused(f'Unknown settings: {", ".join(sorted(unknown))}')
    TARGET_NAME = settings.get('target_name', TARGET_NAME)
    TARGET = normalize(TARGET_NAME)
    PREFIX = settings.get('mqtt_prefix', PREFIX)
    MAX_TILES = int(settings.get('max_tiles', MAX_TILES))
    REMOTE_PLAY_URL = settings.get('remote_play_url', REMOTE_PLAY_URL).rstrip('/')
    PUBLIC_URL = settings.get('public_url', PUBLIC_URL).rstrip('/')
    TV_HOST = settings.get('tv_host', TV_HOST)
    OFF_WATTS = float(settings.get('off_watts', OFF_WATTS))
    HUNG_WATTS = float(settings.get('hung_watts', HUNG_WATTS))


class Refused(RuntimeError):
    pass


def exact_target(text, confidence):
    # No fuzzy replacement of J/B or substring match: ambiguity means no launch.
    return confidence >= 50 and normalize(text) == TARGET


def ocr(png, box, psm=7, scale=4):
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(png)) as image:
        if image.size != (640, 360):
            raise Refused(f'Unexpected screenshot size: {image.size}')
        crop = image.crop(box)
        crop = ImageOps.grayscale(crop.resize((crop.width * scale, crop.height * scale)))
    data = io.BytesIO()
    crop.save(data, format='PNG')
    result = subprocess.run(['tesseract', 'stdin', 'stdout', '-l', 'eng', '--psm', str(psm), 'tsv'],
                            input=data.getvalue(), capture_output=True, check=True, timeout=10)
    words = [r for r in csv.DictReader(io.StringIO(result.stdout.decode()), delimiter='\t')
             if r.get('level') == '5' and r.get('text', '').strip()]
    confidence = min((float(w['conf']) for w in words if any(c.isalnum() for c in w['text'])), default=0)
    return ' '.join(w['text'] for w in words), confidence


POWER_LABELS = ('Enter Rest Mode', 'Turn Off PS5', 'Restart PS5')


def power_label(text):
    """At this Remote Play resolution the system font's rn/m, 5/S and o/a are ambiguous.
    Only these complete native menu labels are accepted; no substring/fuzzy match."""
    label = ' '.join(text.casefold().split())
    if re.fullmatch(r'(turn|tum) off p[s5][5s]', label): return 'Turn Off PS5'
    if re.fullmatch(r'enter rest m[oa]de', label): return 'Enter Rest Mode'
    if re.fullmatch(r'restart p[s5][5s]', label): return 'Restart PS5'
    return text


def outlined_row(grey):
    """The top of the outlined row, or None when there is no single outline (an incomplete one refuses)."""
    def edge(x, y):
        return grey.getpixel((x, y)) - min(grey.getpixel((x, y-2)), grey.getpixel((x, y+2))) > 12
    # Include Restart's lower outline at y=290, but exclude the menu border at y=295.
    rows = [y for y in range(145, 294)
            if sum(edge(x, y) for x in range(401, 606)) >= .75 * 205]
    groups = []
    for row in rows:
        if groups and row <= groups[-1][-1] + 4:
            groups[-1].append(row)
        else:
            groups.append([row])
    if len(groups) != 2 or not 55 <= groups[1][0] - groups[0][-1] <= 85:
        return None
    top, bottom = groups[0][0], groups[1][-1]
    # The horizontal edges must belong to one outlined row, not text or two dividers.
    if any(sum(max(grey.getpixel((x, y)) - min(grey.getpixel((x-3, y)), grey.getpixel((x+3, y)))
                   for x in xs) > 10 for y in range(top+4, bottom-3))
           < .8 * (bottom-top-7) for xs in (range(394, 399), range(608, 613))):
        raise Refused('Power selection outline is incomplete; no shutdown')
    return top


def described_row(grey):
    """The top of the one label with its description line under it: how the PS5 marks focus when it draws no
    outline (6 Nov: Restart PS5 remembered from an earlier restart). Three bright labels, one dim line; else unclear."""
    bands = []
    for y in range(172, 294):
        row = [grey.getpixel((x, y)) for x in range(426, 600)]
        if sum(v > 60 for v in row) >= 3:
            if bands and y <= bands[-1][1] + 2:
                bands[-1][1], bands[-1][2] = y, max(bands[-1][2], max(row))
            else:
                bands.append([y, y, max(row)])
    bands = [b for b in bands if b[1] - b[0] >= 3]  # not the menu's hairline edge
    labels = [b for b in bands if b[2] >= 130]
    focused = [l for l in labels if any(0 < d[0] - l[1] <= 14 and d[2] < 130 for d in bands)]
    if len(labels) != 3 or len(focused) != 1:
        raise Refused('Selected Power option is unclear; no shutdown')
    return focused[0][0]


def selected_power_option(png):
    """After positively opening Power, read only its selected row (outlined, or the one showing its description);
    refuse other layouts."""
    from PIL import Image
    with Image.open(io.BytesIO(png)) as image:
        if image.size != (640, 360):
            raise Refused('Unexpected Power menu size; no shutdown')
        grey = image.convert('L')
        top = outlined_row(grey)
        label_top = described_row(grey) if top is None else None
    if top is not None:
        text, confidence = ocr(png, (424, top+6, 525, top+25), scale=6)
        return power_label(text), confidence
    # Crops measured on the 6 Nov frame: 5-7 px above the label's first bright row read it cleanly.
    reads = [ocr(png, (424, label_top + d, 525, label_top + d + 16), scale=6) for d in (-7, -6, -5)]
    known = [(power_label(t), c) for t, c in reads if power_label(t) in POWER_LABELS]
    if not known or len({label for label, _ in known}) != 1:
        raise Refused('Selected Power option is unclear; no shutdown')
    return max(known, key=lambda r: r[1])


class Budget:
    """Record before input so a killed process still consumes an attempt; survives restart."""
    limit = 3
    window = 1800
    refusal = 'Three failed attempts in 30 minutes; wait before retrying'

    def __init__(self, path, now=time.time):
        self.path, self.now = Path(path), now

    def recent(self):
        attempts = json.loads(self.path.read_text()) if self.path.exists() else []
        if not isinstance(attempts, list) or any(type(t) not in (float, int) or not math.isfinite(t) for t in attempts):
            raise Refused('Invalid failure budget; inspect state before retrying')
        return [t for t in attempts if t > self.now() - self.window]

    def check(self):
        if len(self.recent()) >= self.limit:
            raise Refused(self.refusal)

    def reserve(self):
        now = self.now()
        attempts = self.recent()
        if len(attempts) >= self.limit:
            raise Refused(self.refusal)
        attempts.append(now)
        self.write(attempts)
        return now

    def success(self, ticket):
        attempts = json.loads(self.path.read_text())
        attempts.remove(ticket)
        self.write(attempts)

    def write(self, attempts):
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w') as f:
            os.chmod(tmp, 0o600)
            json.dump(attempts, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)


class RebootBudget(Budget):
    limit = 1
    refusal = 'Automatic restart already tried in the last 30 minutes; check the PS5'

    def reserve(self):
        with open(self.path.with_suffix('.lock'), 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return super().reserve()


class HardBudget(RebootBudget):
    window = 7200
    refusal = 'Automatic power recovery already ran in the last two hours; check the PS5'


class HungBudget(RebootBudget):
    window = 86400
    refusal = 'A hung PS5 was already power-cut today; check it before cutting power again'


class Bridge:
    """Narrow request/reply to Home Assistant for the PS5 plug and Bluetooth waker. No Home Assistant token on this machine."""
    def __init__(self, publish, timeout=60):
        self.publish, self.timeout = publish, timeout
        self.pending, self.lock = {}, threading.Lock()

    def request(self, action, timeout=None, **extra):
        rid = uuid.uuid4().hex
        slot = [threading.Event(), None]
        with self.lock:
            self.pending[rid] = slot
        try:
            self.publish(json.dumps({'id': rid, 'at': time.time(), 'action': action, **extra}))
            if not slot[0].wait(self.timeout if timeout is None else timeout):
                raise Refused(f'Home Assistant did not answer {action}; no further power action')
            return slot[1]
        finally:
            with self.lock:
                self.pending.pop(rid, None)

    def receive(self, payload):
        try:
            reply = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(reply, dict):
            return
        with self.lock:
            slot = self.pending.get(reply.get('id'))
            if slot and slot[1] is None:
                slot[1] = reply
                slot[0].set()


class Console:
    def __init__(self, config, state_dir, report=print):
        self.host = config['host']
        self.port = config.get('probe_port', 9021)  # refree's own default, so both read the same port
        self.base = REMOTE_PLAY_URL
        self.directory, self.report = Path(state_dir), report
        self.deadline = None
        self.last_png = None
        self.launch_attempted = False
        self.cancelled = lambda: False

    def api(self, path, data=None):
        if path != '/api/maker/close' and self.cancelled():
            raise Refused('Automatic jailbreak disabled; no further input')
        if self.deadline and time.monotonic() > self.deadline and path != '/api/maker/close':
            raise Refused('Navigation timed out; no further input')
        req = urllib.request.Request(self.base + path,
            None if data is None else json.dumps(data).encode(),
            {} if data is None else {'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=35 if path.endswith('/open') else 15) as r:
            return json.load(r)

    def power_info(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1)
            try:
                sock.sendto(b'SRCH * HTTP/1.1\ndevice-discovery-protocol-version:00030010\n', (self.host, 9302))
                packet, peer = sock.recvfrom(4096)
            except TimeoutError:
                return {'state': 'offline'}
            except OSError:
                return {'state': 'unknown'}  # Local network failure is not evidence of a reboot.
        if peer[0] != socket.gethostbyname(self.host):
            return {'state': 'unknown'}
        reply = packet.decode(errors='replace')
        if reply.startswith('HTTP/1.1 620'):
            return {'state': 'rest'}
        if not reply.startswith('HTTP/1.1 200 '):
            return {'state': 'unknown'}
        fields = {key.strip().lower(): value.strip() for line in reply.splitlines()
                  if ':' in line for key, value in [line.split(':', 1)]}
        fields['state'] = 'app' if any(k.startswith('running-app-') for k in fields) else 'awake'
        return fields

    def power(self):
        return self.power_info()['state']

    def recovery_allowed(self):
        info = self.power_info()
        if info['state'] == 'awake':
            return
        if info['state'] == 'app' and exact_target(info.get('running-app-name', ''), 100):
            return
        raise Refused('PS5 is off, unreachable, or another app is running; no automatic restart')

    def awake(self):
        state = self.power()
        if state == 'offline':
            raise Refused('PS5 off or unreachable; turn it on with the controller')
        if state not in ('awake', 'app'):
            raise Refused('PS5 is not awake; wake it before launching')
        if state == 'app':
            raise Refused('An app is running; return to Home before launching')

    def port_open(self, port):
        try:
            with socket.create_connection((self.host, port), timeout=1):
                return True
        except OSError:
            return False

    def probe(self):
        try:
            with socket.create_connection((self.host, self.port), timeout=1):
                return True
        except OSError:
            return False

    def open(self):
        before = self.api('/api/maker')
        if before['holder'] is not None or before['open']:
            raise Refused('Remote Play is busy; close its other session first')
        self.deadline = time.monotonic() + 180
        self.api('/api/maker/open', {})

    def close(self):
        self.api('/api/maker/close', {})
        self.deadline = None

    def remote_play_ready(self):
        """A session opens. The 'wasn't turned off properly' screen refuses Remote Play until someone presses X."""
        if self.cancelled():
            raise Refused('Automatic recovery cancelled')
        before = self.api('/api/maker')
        if before['holder'] is not None or before['open']:
            # Someone else's session is not the warning screen: never take it, never press X into it.
            raise Refused('Remote Play is busy with another session; no X pressed')
        try:
            self.api('/api/maker/open', {})
        except OSError:
            try:
                self.api('/api/maker/close', {})
            except OSError:
                pass
            return False
        self.close()
        return True

    def wait(self, seconds):
        time.sleep(seconds)

    def press(self, button, wait=.5):
        self.api('/api/maker/press', {'button': button, 'hold_ms': 150})
        self.wait(wait)

    def snapshot(self):
        shot = self.api('/api/maker/screenshot', {'name': 'resilient-scan'})
        filename = shot.get('png')
        try:
            if shot.get('decode_error') or not filename or Path(filename).name != filename:
                raise Refused('No readable screenshot; no launch')
            with urllib.request.urlopen(self.base + '/api/screenshots/' + filename, timeout=10) as r:
                self.last_png = r.read()
            return self.last_png
        finally:
            # These are our own temporary captures, not the user's gallery.
            for key in ('png', 'h264'):
                name = shot.get(key)
                if name and Path(name).name == name and name.endswith(('-resilient-scan.png', '-resilient-scan.h264')):
                    (self.directory / 'screenshots' / name).unlink(missing_ok=True)

    def media(self):
        for attempt in range(3):
            self.api('/api/maker/home', {})
            self.wait(3)
            text, _ = ocr(self.snapshot(), (20, 5, 145, 35))
            # The inactive tab is very dim; Tesseract often only sees the active tab.
            if {'games', 'media'} & set(re.findall(r'[a-z]+', text.casefold())):
                break
            if attempt < 2:
                self.press('circle', 1.5)  # a dialog or overlay over Home: back once, then Home again
        else:
            raise Refused('Home screen not recognized; no menu selection')
        self.press('up')
        for _ in range(6):
            self.press('left', .3)
        self.press('right')
        self.press('cross', 1.5)  # Activate Media, not merely highlight it.
        for _ in range(MAX_TILES):
            self.press('left', .2)
        self.press('right', 1)  # First actual tile, after All Apps. Its identity is unknown.

    def title(self):
        return ocr(self.snapshot(), (45, 238, 330, 266), scale=6)

    def proof(self):
        if self.last_png:
            (self.directory / 'launcher-proof.png').write_bytes(self.last_png)

    def ready_preview(self):
        """One fresh frame after confirmation. No Home/press/wake; never take another session."""
        self.open()
        try:
            png = self.snapshot()
            folder = self.directory / 'screenshots'
            folder.mkdir(exist_ok=True)
            tmp = folder / 'autojb-ready.tmp'
            tmp.write_bytes(png)
            tmp.chmod(0o600)
            os.replace(tmp, folder / 'autojb-ready.png')
            base = PUBLIC_URL or self.base
            return f'{base}/api/screenshots/autojb-ready.png?v={time.time_ns()}'
        finally:
            self.close()

    def shutdown_choice(self):
        for attempt in range(3):
            try:
                text, confidence = selected_power_option(self.snapshot())
                if confidence < 70 or text not in ('Enter Rest Mode', 'Turn Off PS5', 'Restart PS5'):
                    raise Refused('Power option unreadable; no shutdown')
                return text, confidence
            except Refused:
                if attempt == 2:
                    raise
                self.wait(.8)  # a still-fading menu may not have a complete outline yet

    def shutdown_menu(self, target='Turn Off PS5'):
        if target not in ('Turn Off PS5', 'Restart PS5'):
            raise Refused('Unsupported Power action')
        self.api('/api/maker/home', {})
        self.wait(3)
        text, _ = ocr(self.snapshot(), (20, 5, 145, 35))
        if not {'games', 'media'} & set(re.findall(r'[a-z]+', text.casefold())):
            raise Refused('Home screen not recognized; no shutdown')
        self.press('ps', 1.5)
        self.press('down')
        self.press('left')
        for _ in range(14):
            text, confidence = ocr(self.snapshot(), (90, 303, 555, 328), psm=6, scale=8)
            if text.strip().casefold() == 'power' and confidence >= 70:
                self.wait(.4)
                text, confidence = ocr(self.snapshot(), (90, 303, 555, 328), psm=6, scale=8)
                if text.strip().casefold() != 'power' or confidence < 70:
                    raise Refused('Power selection changed; no shutdown')
                self.press('cross', 2)
                break
            self.press('right')
        else:
            raise Refused('Power control not found; no shutdown')
        for _ in range(4):
            text, confidence = self.shutdown_choice()
            if confidence < 70:
                raise Refused('Power option unreadable; no shutdown')
            if text == target:
                return
            if text not in POWER_LABELS:
                raise Refused('Unexpected Power option; no shutdown')
            # The PS5 remembers the last row used (6 Nov: Restart PS5), so go towards the target.
            self.press('down' if POWER_LABELS.index(text) < POWER_LABELS.index(target) else 'up', 1.5)
        raise Refused(f'{target} not selected; no power action')


def shutdown(console, dry_run=False):
    if console.power() not in ('awake', 'app'):
        raise Refused('PS5 is not awake; no shutdown')
    opened = False
    try:
        console.open()
        opened = True
        console.deadline = time.monotonic() + 90
        console.shutdown_menu()
        for check in range(2):
            text, confidence = console.shutdown_choice()
            if text.strip().casefold() != 'turn off ps5' or confidence < 70:
                raise Refused('Turn Off PS5 selection changed or is unclear; no shutdown')
            if check == 0:
                console.wait(.4)
        if dry_run:
            console.api('/api/maker/home', {})
            return {'state': 'target verified; not shut down'}
        console.press('cross', 0)
    finally:
        if opened:
            console.close()
    misses = 0
    for _ in range(23):
        console.wait(2)
        state = console.power()
        misses = misses + 1 if state == 'offline' else 0
        if misses >= 3:
            return {'state': 'off', 'message': 'Use the controller to turn it back on.'}
        if state == 'rest':
            raise Refused('Console entered Rest Mode; full shutdown not confirmed')
    raise Refused('Turn-off not confirmed. Check the PS5 before trying again.')


def operate_shutdown(config, directory, dry_run=False):
    with open(directory / 'launcher.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'state': 'refused', 'message': 'PS5 control is busy. Try again later.'}
        try:
            return shutdown(Console(config, directory), dry_run)
        except Exception as error:
            return {'state': 'failed', 'message': str(error)[:200]}


def restart(console, dry_run=False, fresh=False):
    """One verified Restart selection; a confirmed power cycle precedes another launch.
    fresh: a path asked for a fresh boot of a jailbroken console, so being jailbroken is the reason, not a stop."""
    def allowed():
        if console.cancelled():
            raise Refused('Automatic recovery cancelled')
        if fresh:
            if console.power() not in ('awake', 'app'):
                raise Refused('PS5 is not awake; no restart')
        else:
            console.recovery_allowed()

    if console.cancelled():
        raise Refused('Automatic recovery cancelled')
    if console.probe() and not fresh:
        return {'state': 'already jailbroken'}
    allowed()
    opened = False
    try:
        console.open()
        opened = True
        console.deadline = time.monotonic() + 90
        console.shutdown_menu('Restart PS5')
        for check in range(2):
            allowed()
            if console.probe() and not fresh:
                console.api('/api/maker/home', {})
                return {'state': 'already jailbroken'}
            text, confidence = console.shutdown_choice()
            if text != 'Restart PS5' or confidence < 70:
                raise Refused('Restart PS5 selection changed or is unclear; no restart')
            if check == 0:
                console.wait(.4)
        if dry_run:
            console.api('/api/maker/home', {})
            return {'state': 'restart target verified; not restarted'}
        console.press('cross', 0)
    finally:
        if opened:
            console.close()
    misses, down = 0, False
    for _ in range(60):
        if console.cancelled():
            raise Refused('Automatic recovery cancelled')
        console.wait(2)
        power = console.power()
        if power == 'offline':
            misses += 1
            down = down or misses >= 3
        elif power == 'awake':
            if down:
                return {'state': 'rebooted'}
            misses = 0
        elif power == 'app' and not down:
            if not fresh:
                allowed()  # the failed Y2JB may still answer briefly while restart begins
            misses = 0
        else:
            raise Refused('Restart not confirmed or an app is running; no retry')
    raise Refused('Restart did not complete; check the PS5. No further restart attempted')


def operate_restart(config, directory, cancelled=None, dry_run=False):
    with open(directory / 'launcher.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'state': 'refused', 'message': 'PS5 control is busy; no automatic restart'}
        console = Console(config, directory)
        console.cancelled = cancelled or (lambda: False)
        try:
            return restart(console, dry_run=dry_run)
        except Exception as error:
            return {'state': 'refused', 'message': str(error)[:200]}


OFF_WATTS = 2.0    # the Tapo plug reads 0 W for a console that is fully off; rest mode reads about 4 W
HUNG_WATTS = 30.0  # a running or hung console draws 50+ W


def went_down(console, seconds):
    """'rest' or 'offline' when discovery never answers awake for the whole period; None once it does."""
    seen = 'offline'
    for _ in range(int(seconds / 2)):
        if console.cancelled():
            raise Refused('Automatic recovery cancelled')
        power = console.power()
        if power in ('awake', 'app'):
            return None
        if power == 'unknown':
            raise Refused('Local network state unknown; no power action')
        if power == 'rest':
            seen = 'rest'
        console.wait(2)
    return seen


def comes_up(console, seconds):
    for _ in range(int(seconds / 2)):
        if console.cancelled():
            raise Refused('Automatic recovery cancelled')
        if console.power() in ('awake', 'app'):
            return True
        console.wait(2)
    return False


def plug_watts(bridge):
    status = bridge.request('status', 15)
    watts = status.get('watts')
    if not status.get('ok') or status.get('plug') != 'on' or type(watts) not in (int, float):
        raise Refused('PS5 plug is off or unreadable; no power action')
    return watts


def bluetooth_wake(bridge):
    return bridge.request('bt_wake', 30).get('status') == 'Success'


def power_cycle(bridge, console, hung):
    reply = bridge.request('power_cycle', 60, hung=hung)
    if not reply.get('ok'):
        raise Refused(f"Power cycle refused: {reply.get('message') or 'no reason given'}")
    console.wait(20)  # let standby settle before paging it


def hard_recover(console, bridge, progress, hard_budget, hung_budget):
    """Bring back a console that went down after a failed automatic jailbreak.

    Fully off (no reply, under 2 W): Bluetooth wake; if it does not answer, cycle the plug and wake again.
    Hung (no reply, 30 W or more, for over three minutes): one plug cycle per day, then wake.
    Rest mode or standby-level power: Bluetooth wake only. Cutting power in rest mode can corrupt data.
    A plug someone switched off, an unknown network or a console that answers again: no power action.
    """
    down = went_down(console, 20)
    if not down:
        return {'state': 'answered'}
    hard_budget.check()
    watts = plug_watts(bridge)
    for _ in range(6):  # the plug's reading lags about 30 s behind a crash; a restart keeps drawing power
        if down == 'rest' or watts < OFF_WATTS:
            break
        down = went_down(console, 10)
        if not down:
            return {'state': 'answered'}
        watts = plug_watts(bridge)
    if down == 'offline' and watts >= HUNG_WATTS:
        down = went_down(console, 120)
        if not down:
            return {'state': 'answered'}
        watts = plug_watts(bridge)
    if down == 'offline' and watts >= HUNG_WATTS:
        hung_budget.check()
        hard_budget.reserve()
        hung_budget.reserve()
        progress('automatic recovery: power cycling')
        power_cycle(bridge, console, hung=True)
        bluetooth_wake(bridge)
    elif down == 'offline' and watts < OFF_WATTS:
        hard_budget.reserve()
        progress('automatic recovery: waking PS5')
        if bluetooth_wake(bridge) and comes_up(console, 120):
            return {'state': 'recovered', 'power_cut': False}
        progress('automatic recovery: power cycling')
        power_cycle(bridge, console, hung=False)
        bluetooth_wake(bridge)
    else:
        hard_budget.reserve()
        progress('automatic recovery: waking PS5')
        if bluetooth_wake(bridge) and comes_up(console, 120):
            return {'state': 'recovered', 'power_cut': False}
        raise Refused('PS5 did not wake from rest or standby over Bluetooth; no power cut')
    if not comes_up(console, 300):
        raise Refused('PS5 did not come back after a power cycle and Bluetooth wake')
    return {'state': 'recovered', 'power_cut': True}


class TvRemote:
    """X on the PS5 over HDMI-CEC from the rooted LG TV (tv/lg-c1/ps5-cec.py), whatever input the TV shows.

    The key is forced to that one command on the TV, and the TV's host key is pinned next to it. When the key is not
    installed, no tv_host is set, or the TV is in standby and unreachable, recovery asks the owner instead.
    """
    def __init__(self, key, host, run=subprocess.run):
        self.key, self.host, self.run = Path(key), host, run

    @classmethod
    def installed(cls, directory):
        key = Path(directory) / 'tv-cec' / 'id_ed25519'
        return cls(key, TV_HOST) if key.exists() and TV_HOST else None

    def press_x(self):
        try:
            result = self.run(['ssh', '-i', str(self.key), '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                               '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={self.key.parent / "known_hosts"}',
                               '-T', f'root@{self.host}', 'x'], capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0


def await_remote_play(console, progress, tv=None, checks=120):
    """After an unclean power-off the console asks for X before Remote Play works.

    Only reached after this helper's own crash or power-cut recovery, so a refused session means that screen: the TV
    presses X (at most twice), then the owner is asked once, and checking continues for 30 minutes.
    """
    presses, asked = 0, False
    for _ in range(checks):
        console.wait(15)
        if console.remote_play_ready():
            return
        if tv and presses < 2:
            presses += 1
            if tv.press_x():
                continue
            tv = None  # TV unreachable (standby) or no PS5 on CEC: ask now
        if not asked:
            progress('automatic recovery: press X on the PS5')
            asked = True
    raise Refused('Remote Play stayed unavailable for 30 minutes after recovery; check the PS5 screen')


def operate_hard(config, directory, bridge, progress, cancelled=None):
    with open(directory / 'launcher.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('PS5 control is busy; no power recovery')
        console = Console(config, directory)
        console.cancelled = cancelled or (lambda: False)
        result = hard_recover(console, bridge, progress, HardBudget(directory / 'launcher-hard.json'),
                              HungBudget(directory / 'launcher-hung.json'))
        if result['state'] == 'recovered':
            await_remote_play(console, progress, TvRemote.installed(directory))
        return result


PATH_NAME = re.compile(r'[a-z][a-z0-9_]{0,30}')
BUTTONS = {'cross', 'circle', 'square', 'triangle', 'up', 'down', 'left', 'right', 'ps', 'options',
           'l1', 'r1', 'l2', 'r2'}
CONSOLE_STATES = {'rest', 'awake', 'offline'}


def check_step(step):
    if not isinstance(step, dict) or len(step) != 1:
        raise Refused(f'Each step is one kind: {step!r}')
    (kind, args), = step.items()
    if not isinstance(args, dict):
        raise Refused(f'{kind}: settings must be an object')
    allowed = {'fresh_boot': set(), 'press_ps': set(), 'open': {'tile', 'also_reads'},
               'wait': {'console', 'port', 'seconds', 'watts_above', 'timeout'}, 'press': {'buttons'}}
    if kind not in allowed:
        raise Refused(f'unknown step {kind!r}')
    if set(args) - allowed[kind]:
        raise Refused(f'{kind}: unknown settings {sorted(set(args) - allowed[kind])}')
    if kind == 'open' and not (isinstance(args.get('tile'), str) and args['tile'].strip()):
        raise Refused('open: needs a tile name')
    if kind == 'wait':
        given = [k for k in ('console', 'port', 'seconds', 'watts_above') if k in args]
        if len(given) != 1:
            raise Refused('wait: needs exactly one of console, port, seconds or watts_above')
        if 'console' in args and args['console'] not in CONSOLE_STATES:
            raise Refused(f'wait: console must be one of {sorted(CONSOLE_STATES)}')
    if kind == 'press':
        bad = [b for b in args.get('buttons', []) if b not in BUTTONS]
        if bad or not args.get('buttons'):
            raise Refused(f'press: unknown buttons {bad}')


def load_paths(directory):
    """paths.json next to refree's config: named step lists, each a Home Assistant button. No file, no paths."""
    path = Path(directory) / 'paths.json'
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise Refused('paths.json must be an object of named paths')
    for name, spec in raw.items():
        if not PATH_NAME.fullmatch(name):
            raise Refused(f'Path name {name!r}: lowercase letters, digits and _')
        if not isinstance(spec, dict) or not isinstance(spec.get('button'), str):
            raise Refused(f'{name}: needs a button label')
        if not spec.get('steps'):
            raise Refused(f'{name}: no steps')
        for step in spec['steps']:
            check_step(step)
    return raw


def canonical(text):
    """For learning: the OCR confuses J, I, l, ) and | in this font (6 Nov: "Y2JB - Linux" read as "Y2)B - Linux")."""
    return re.sub(r'[\s\-–—]', '', text.casefold()).translate(str.maketrans('ij)|!1]', 'lllllll'))


def tile_names(tile, titles, also_reads=()):
    """The exact readings that count as this tile: its name, configured readings, and every reading discovery saw
    for a tile whose readings match the name. Launching still needs two consecutive frames to read one of them."""
    names = {tile, *also_reads}
    for seen in titles or []:
        if any(canonical(r) == canonical(tile) for r in seen['readings']):
            names |= set(seen['readings'])
    return names


def discover_titles(console, tiles=MAX_TILES, reads=2):
    """Walk the Media row with Remote Play open and record what each tile's title reads as. Arrows only.
    Readings under 20% are noise (past the end of the row); two such tiles in a row end the walk."""
    console.media()
    found, junk = [], 0
    for index in range(tiles):
        readings = {}
        for _ in range(reads):
            text, confidence = console.title()
            if text.strip() and confidence >= 20:
                readings[text] = max(readings.get(text, 0), round(confidence))
            console.wait(.4)
        if readings:
            found.append({'tile': index + 1, 'title': max(readings, key=readings.get), 'readings': readings})
            junk = 0
        else:
            junk += 1
            if junk == 2:
                break
        if index + 1 < tiles:
            console.press('right', 1)
    return found


def open_tile(console, tile, names):
    if console.power() not in ('awake', 'app'):
        raise Refused('PS5 is not awake; nothing opened')
    pressed = False
    console.open()
    try:
        console.media()
        for index in range(MAX_TILES):
            if console.title()[0] in names:
                console.wait(.8)
                if console.title()[0] in names:
                    console.press('cross', .3)
                    pressed = True
                break
            if index + 1 < MAX_TILES:
                console.press('right', 1)
    finally:
        console.close()  # never leave a session open while an app starts (refree segfaulted, 6 Nov)
    if not pressed:
        raise Refused(f'{tile} not found by name; nothing opened')


def wait_for(console, args, bridge=None):
    if 'seconds' in args:
        console.wait(args['seconds'])
        return
    for _ in range(max(1, int(args.get('timeout', 180)) // 2)):
        if console.cancelled():
            raise Refused('Path cancelled')
        if 'port' in args and console.port_open(args['port']):
            return
        if 'watts_above' in args and bridge:
            watts = bridge.request('status', 15).get('watts')
            if type(watts) in (int, float) and watts > args['watts_above']:
                return
        if 'console' in args:
            power = console.power()
            if power == args['console'] or (args['console'] == 'awake' and power == 'app'):
                return
        console.wait(2)
    what = (f"port {args['port']}" if 'port' in args else f"more than {args['watts_above']} W" if 'watts_above' in args
            else args['console'])
    raise Refused(f'Timed out waiting for {what}')


def describe(kind, args):
    return {'fresh_boot': lambda: 'fresh boot', 'press_ps': lambda: 'pressing PS',
            'open': lambda: f"opening {args['tile']}",
            'press': lambda: 'pressing ' + ', '.join(args['buttons']),
            'wait': lambda: (f"waiting {args['seconds']} s" if 'seconds' in args else
                             f"waiting for port {args['port']}" if 'port' in args else
                             f"waiting for more than {args['watts_above']} W" if 'watts_above' in args else
                             f"waiting for {args['console']}"),
            }[kind]()


def run_path(console, bridge, name, spec, progress, titles=None, restart=restart):
    """One configured path, step by step. Any unclear state stops it; nothing is retried here."""
    for step in spec['steps']:
        (kind, args), = step.items()
        if console.cancelled():
            raise Refused('Path cancelled')
        progress(f'{name}: {describe(kind, args)}')
        if kind == 'fresh_boot':
            if console.probe():
                if restart(console, fresh=True)['state'] != 'rebooted':
                    raise Refused('Fresh boot: restart not confirmed')
                console.wait(20)  # Home settles after the boot
        elif kind == 'open':
            open_tile(console, args['tile'], tile_names(args['tile'], titles, args.get('also_reads', ())))
        elif kind == 'wait':
            wait_for(console, args, bridge)
        elif kind == 'press_ps':
            status = bridge.request('bt_wake', 30).get('status') if bridge else 'no waker'
            if status != 'Success':
                raise Refused(f'PS press over Bluetooth not answered: {status}')
        elif kind == 'press':
            console.open()
            try:
                for button in args['buttons']:
                    console.press(button, 1)
            finally:
                console.close()
    return {'state': f'{name} done', 'launched': True}


def operate_path(config, directory, bridge, name, spec, progress, cancelled=None):
    with open(directory / 'launcher.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'state': 'refused: launcher busy', 'launched': False}
        console = Console(config, directory)
        console.cancelled = cancelled or (lambda: False)
        titles_file = directory / 'titles.json'
        titles = json.loads(titles_file.read_text()) if titles_file.exists() else []
        try:
            return run_path(console, bridge, name, spec, progress, titles)
        except Exception as error:
            return {'state': f'failed: {error}'[:250], 'launched': None}


def operate_discovery(config, directory):
    with open(directory / 'launcher.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('PS5 control is busy')
        console = Console(config, directory)
        console.awake()
        console.open()
        try:
            titles = discover_titles(console)
            console.api('/api/maker/home', {})
        finally:
            console.close()
    tmp = directory / 'titles.tmp'
    tmp.write_text(json.dumps(titles, indent=1))
    os.replace(tmp, directory / 'titles.json')
    return titles


def launch(console, budget, focus_only=False):
    """No launch input unless two successive fresh frames name the exact target."""
    console.awake()
    if console.probe() and not focus_only:
        return {'state': 'already jailbroken', 'launched': False, 'scanned': []}
    ticket = None if focus_only else budget.reserve()
    opened = False
    scanned = []
    try:
        console.open()
        opened = True
        console.media()
        for index in range(MAX_TILES):
            title, confidence = console.title()
            scanned.append({'tile': index + 1, 'title': title, 'confidence': round(confidence, 1)})
            console.report(f'Tile {index + 1}: {title!r} ({confidence:.0f})')
            if exact_target(title, confidence):
                console.wait(.8)
                second, second_confidence = console.title()
                if not exact_target(second, second_confidence):
                    raise Refused('Selected title changed or became unreadable; no launch')
                console.proof()
                if not focus_only:
                    console.launch_attempted = True
                    console.press('cross', 1)
                break
            if index + 1 < MAX_TILES:
                console.press('right', 1)
        else:
            raise Refused(f'{TARGET_NAME} not positively identified; no launch')
    except Exception:
        if ticket is not None and not getattr(console, 'launch_attempted', False):
            budget.success(ticket)  # nothing was launched: a refusal is not a failed attempt (6 Nov)
        raise
    finally:
        if opened:
            console.close()
    if focus_only:
        return {'state': 'target verified; not launched', 'launched': False, 'scanned': scanned}
    misses = 0
    for _ in range(90):
        if console.probe():
            budget.success(ticket)
            return {'state': 'jailbreak confirmed', 'launched': True, 'scanned': scanned}
        # 6 Nov: a panic 6 s after Cross left the console off while this waited out the full three minutes.
        misses = misses + 1 if console.power() in ('offline', 'rest') else 0
        if misses >= 3:
            raise Refused('PS5 went offline after Y2JB opened; no confirmation')
        console.wait(2)
    raise Refused('Y2JB opened but jailbreak did not confirm within 180 seconds')


def operate(config, directory, focus_only=False, cancelled=None):
    with open(directory / 'launcher.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'state': 'refused: launcher busy', 'launched': False}
        console = Console(config, directory)
        console.cancelled = cancelled or (lambda: False)
        try:
            result = launch(console, Budget(directory / 'launcher-attempts.json'), focus_only)
            if result['state'] == 'jailbreak confirmed':
                try:
                    result['preview'] = console.ready_preview()
                except Exception as error:
                    # A screenshot is optional: never turn a confirmed success into a failure/retry.
                    console.report(f'Ready preview unavailable: {error}')
                    result['preview'] = ''
            return result
        except Exception as error:
            attempted = getattr(console, 'launch_attempted', False)
            if cancelled and cancelled() and not attempted:
                return {'state': 'automatic launch cancelled', 'launched': False}
            return {'state': f'{"failed" if attempted else "refused"}: {error}'[:250],
                    'launch_attempted': attempted, 'launched': None if attempted else False}


class AutoBoot:
    """Persist a claim BEFORE launching; restarting the helper cannot retry the same wake."""
    def __init__(self, path, now=time.monotonic):
        self.path, self.now = Path(path), now
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {'enabled': False, 'handled': False}
        if (not isinstance(self.state, dict) or set(self.state) != {'enabled', 'handled'}
                or any(type(v) is not bool for v in self.state.values())):
            raise Refused('Invalid automatic-launch state; inspect it before enabling')
        self.ready_since = None
        self.misses = 0

    @property
    def enabled(self):
        return self.state['enabled']

    def save(self):
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w') as f:
            os.chmod(tmp, 0o600)
            json.dump(self.state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def set_enabled(self, enabled):
        if enabled != self.enabled:
            self.state.update(enabled=enabled, handled=False)
            self.ready_since = None
            self.save()

    def consume(self):
        if not self.state['handled']:
            self.state['handled'] = True
            self.save()
        self.ready_since = None

    def rearm(self):
        self.state['handled'] = False
        self.ready_since = None
        self.misses = 0
        self.save()

    def observe(self, power, jailbroken):
        if power in ('offline', 'rest'):
            self.ready_since = None
            self.misses += 1
            if (power == 'rest' or self.misses >= 3) and self.state['handled']:
                self.state['handled'] = False
                self.save()
            return None
        self.misses = 0
        if power not in ('awake', 'app') or not self.enabled or self.state['handled']:
            self.ready_since = None
            return None
        if jailbroken or power == 'app':
            self.consume()
            return 'already jailbroken' if jailbroken else 'automatic skipped: an app is running'
        if self.ready_since is None:
            self.ready_since = self.now()
        if self.now() - self.ready_since < 30:
            return None
        self.consume()
        return 'launch'


class Launcher:
    """One lock and failure budget for manual and automatic requests."""
    def __init__(self, config, directory, publish, now=time.monotonic, ready=None, publish_power=None, bridge=None,
                 console_power=None):
        self.config, self.directory, self.publish = config, directory, publish
        self.console_power = console_power  # reports awake/app/rest/offline for Home Assistant's rest-mode guard
        self.bridge = bridge  # without it a crashed console is reported, not powered back on
        self.now, self.ready, self.next_ready = now, ready, 0
        self.publish_power = publish_power or (lambda result: None)
        self.busy, self.state_lock = threading.Lock(), threading.Lock()
        self.auto = AutoBoot(directory / 'launcher-auto.json', now)
        self.cancelled = threading.Event()
        if not self.auto.enabled:
            self.cancelled.set()

    def set_enabled(self, enabled):
        with self.state_lock:
            self.auto.set_enabled(enabled)
            # Never revive an in-flight cancelled pass by quickly switching back on.
            if not enabled:
                self.cancelled.set()

    def execute(self, source):
        if source == 'automatic':
            with self.state_lock:
                if not self.auto.enabled:
                    return
                self.cancelled = threading.Event()
                cancelled = self.cancelled.is_set
        else:
            cancelled = None
        self.publish({'state': 'checking PS5', 'launched': False, 'source': source})
        result = operate(self.config, self.directory, cancelled=cancelled)
        if source == 'automatic' and cancelled():
            result = {'state': 'automatic launch cancelled', 'launched': result.get('launched')}
        elif (source == 'automatic' and result.get('launch_attempted') is True
              and result['state'].startswith('failed:')):
            try:
                RebootBudget(self.directory / 'launcher-reboots.json').reserve()
                self.publish({'state': 'automatic reboot starting', 'launched': False, 'source': source})
                recovery = operate_restart(self.config, self.directory, cancelled=cancelled)
                with self.state_lock:
                    if cancelled():
                        result = {'state': 'automatic launch cancelled', 'launched': None}
                    elif recovery['state'] == 'rebooted':
                        self.auto.rearm()  # only after three misses and a fresh awake reply
                        result = {'state': 'automatic retry waiting', 'launched': False}
                    elif recovery['state'] == 'already jailbroken':
                        result = {'state': 'jailbreak confirmed', 'launched': True}
                    else:
                        result = {'state': 'failed: ' + recovery['message'], 'launched': None}
            except Exception as error:
                result = {'state': f'failed: {error}'[:250], 'launched': None}
            if result['state'].startswith('failed:') and self.bridge and not cancelled():
                result = self.recover_power(result, cancelled)
        self.publish({**result, 'source': source})

    def recover_power(self, failure, cancelled):
        """The restart could not help. If the console has crashed off or hung, bring it back and rearm the retry."""
        def progress(state):
            self.publish({'state': state, 'launched': False, 'source': 'automatic'})
        try:
            outcome = operate_hard(self.config, self.directory, self.bridge, progress, cancelled=cancelled)
        except Exception as error:
            if cancelled():
                return {'state': 'automatic launch cancelled', 'launched': None}
            return {'state': f'failed: {error}'[:250], 'launched': None}
        with self.state_lock:
            if cancelled():
                return {'state': 'automatic launch cancelled', 'launched': None}
            if outcome['state'] != 'recovered':
                return failure
            self.auto.rearm()
        return {'state': 'automatic retry waiting', 'launched': False}

    def run_path(self, name):
        """A configured path from its button. The automatic jailbreak won't act on this boot afterwards."""
        if not self.busy.acquire(blocking=False):
            return
        try:
            source = f'path:{name}'
            spec = load_paths(self.directory).get(name)
            if spec is None:
                self.publish({'state': f'refused: no path {name}', 'launched': False, 'source': source})
                return
            with self.state_lock:
                self.auto.consume()
            self.cancelled = threading.Event()
            progress = lambda state: self.publish({'state': state, 'launched': False, 'source': source})
            result = operate_path(self.config, self.directory, self.bridge, name, spec, progress, self.cancelled.is_set)
            self.publish({**result, 'source': source})
        finally:
            self.busy.release()

    def discover_titles(self, report):
        if not self.busy.acquire(blocking=False):
            return
        try:
            report(operate_discovery(self.config, self.directory))
        except Exception as error:
            report([], error=str(error)[:200])
        finally:
            self.busy.release()

    def manual(self):
        if not self.busy.acquire(blocking=False):
            return
        try:
            with self.state_lock:
                self.auto.consume()
            self.execute('manual')
        finally:
            self.busy.release()

    def power_off(self, request):
        # Commands cannot survive an offline/restart window or replay a retained message.
        if (not isinstance(request, dict) or not re.fullmatch(r'[a-f0-9]{32}', str(request.get('id', '')))
                or type(request.get('at')) not in (int, float)
                or not -5 <= time.time() - request['at'] <= 30):
            return
        if not self.busy.acquire(blocking=False):
            self.publish_power({'id': request['id'], 'state': 'refused', 'message': 'PS5 control is busy. Try again later.'})
            return
        try:
            path = self.directory / 'shutdown-claimed.json'
            claims = json.loads(path.read_text()) if path.exists() else []
            claims = [r for r in claims if time.time() - r['at'] <= 35]
            if any(r['id'] == request['id'] for r in claims):
                return
            tmp = path.with_suffix('.tmp')
            with tmp.open('w') as file:
                os.chmod(tmp, 0o600)
                json.dump(claims + [request], file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, path)  # claim before any console input; survives process restart
            with self.state_lock:
                self.auto.consume()
            self.publish_power({'id': request['id'], **operate_shutdown(self.config, self.directory)})
        except Exception as error:
            self.publish_power({'id': request['id'], 'state': 'failed', 'message': str(error)[:200]})
        finally:
            self.busy.release()

    def tick(self, power=None, jailbroken=None):
        # Live readiness is independent of the automatic-launch switch and manual lock.
        # During an automatic pass the existing closed-port reading remains until confirmation.
        if self.ready and self.now() >= self.next_ready:
            console = Console(self.config, self.directory)
            self.ready(console.probe())
            if self.console_power:
                self.console_power(console.power())
            self.next_ready = self.now() + 5
        if not self.busy.acquire(blocking=False):
            return
        try:
            with self.state_lock:
                if not self.auto.enabled:
                    return
                handled = self.auto.state['handled']
            if power is None:
                console = Console(self.config, self.directory)
                power = console.power()
                jailbroken = not handled and power in ('awake', 'app') and console.probe()
            with self.state_lock:
                decision = self.auto.observe(power, jailbroken)
            if decision == 'launch':
                self.execute('automatic')
            elif decision:
                self.publish({'state': decision, 'launched': False, 'source': 'automatic'})
        finally:
            self.busy.release()


def discovery(paths=None):
    """Home Assistant MQTT discovery: (topic, config) pairs with fixed entity ids the package can rely on."""
    device = {'identifiers': ['ps5_autojb'], 'name': 'PS5 auto-jailbreak', 'model': 'ps5-autojb'}
    common = {'device': device, 'availability_topic': PREFIX + '/availability'}
    def entity(component, key, name, **extra):
        return (f'homeassistant/{component}/ps5_autojb_{key}/config',
                {**common, 'name': name, 'unique_id': f'ps5_autojb_{key}',
                 'default_entity_id': f'{component}.ps5_autojb_{key}', **extra})
    return [
        entity('button', 'launch', 'Run jailbreak', icon='mdi:play-circle-outline',
               command_topic=PREFIX + '/launch', payload_press='PRESS'),
        entity('sensor', 'result', 'Last jailbreak result', entity_category='diagnostic',
               state_topic=PREFIX + '/result', value_template='{{ value_json.state }}',
               json_attributes_topic=PREFIX + '/result'),
        entity('binary_sensor', 'ready', 'Jailbreak ready', icon='mdi:lock-open-variant',
               state_topic=PREFIX + '/ready', payload_on='ON', payload_off='OFF'),
        entity('switch', 'auto', 'Automatic jailbreak', icon='mdi:autorenew',
               command_topic=PREFIX + '/auto/set', state_topic=PREFIX + '/auto/state',
               payload_on='ON', payload_off='OFF'),
        entity('sensor', 'power', 'Console power', icon='mdi:sony-playstation', device_class='enum',
               options=['awake', 'app', 'rest', 'offline', 'unknown'], state_topic=PREFIX + '/power'),
        entity('button', 'discover_titles', 'Discover titles', icon='mdi:magnify-scan', entity_category='config',
               command_topic=PREFIX + '/titles/discover', payload_press='PRESS'),
        entity('sensor', 'titles', 'Titles', icon='mdi:view-carousel', entity_category='diagnostic',
               state_topic=PREFIX + '/titles', value_template='{{ value_json.count }}',
               json_attributes_topic=PREFIX + '/titles'),
    ] + [entity('button', f'path_{name}', spec['button'], icon='mdi:map-marker-path',
                command_topic=f'{PREFIX}/path/{name}', payload_press='PRESS') for name, spec in (paths or {}).items()]


def mqtt_client(mqtt):
    """paho-mqtt 2 (pip) wants the callback API named; 1.6 (Ubuntu's package) has no such argument."""
    if hasattr(mqtt, 'CallbackAPIVersion'):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='ps5-autojb')
    return mqtt.Client(client_id='ps5-autojb')


def serve(config, directory):
    import paho.mqtt.client as mqtt
    client = mqtt_client(mqtt)
    broker = config['mqtt']
    client.username_pw_set(broker['username'], broker['password'])
    client.will_set(PREFIX + '/availability', 'offline', qos=1, retain=True)

    def publish_result(result):
        result.setdefault('preview', '')  # clear an older image on progress, failure or a missed capture
        result['at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        path = directory / 'launcher-last.json'
        path.write_text(json.dumps(result, indent=2) + '\n')
        client.publish(PREFIX + '/result', json.dumps(result), qos=1, retain=True)

    bridge = Bridge(lambda payload: client.publish(PREFIX + '/recover/request', payload, qos=1, retain=False))
    launcher = Launcher(config, directory, publish_result, bridge=bridge,
        publish_power=lambda result: client.publish(PREFIX + '/shutdown/result', json.dumps(result), qos=0, retain=False),
        ready=lambda value: client.publish(PREFIX + '/ready', 'ON' if value else 'OFF', qos=1, retain=True),
        console_power=lambda state: client.publish(PREFIX + '/power', state, qos=1, retain=True))

    def publish_titles(titles, error=''):
        client.publish(PREFIX + '/titles', json.dumps({'count': len(titles), 'titles': [t['title'] for t in titles],
                                                      'tiles': titles, 'error': error}), qos=1, retain=True)

    def publish_auto():
        with launcher.state_lock:
            enabled = launcher.auto.enabled
        client.publish(PREFIX + '/auto/state', 'ON' if enabled else 'OFF', qos=1, retain=True)

    def discover():
        try:
            paths = load_paths(directory)
        except Refused as error:
            print(f'paths.json not loaded: {error}', flush=True)
            paths = {}
        for topic, config in discovery(paths):
            client.publish(topic, json.dumps(config), qos=1, retain=True)
        publish_auto()
        client.publish(PREFIX + '/availability', 'online', qos=1, retain=True)
        saved = directory / 'launcher-last.json'
        if saved.exists():
            client.publish(PREFIX + '/result', saved.read_text(), qos=1, retain=True)

    def on_connect(client, userdata, flags, rc, properties=None):
        if rc != 0:
            return
        client.subscribe([(PREFIX + '/launch', 0), (PREFIX + '/auto/set', 0), (PREFIX + '/shutdown', 0),
                          (PREFIX + '/recover/result', 1), (PREFIX + '/path/+', 0), (PREFIX + '/titles/discover', 0),
                          ('homeassistant/status', 0)])

        discover()

    def on_message(client, userdata, message):
        if message.topic == 'homeassistant/status' and message.payload == b'online':
            discover()
        elif message.topic == PREFIX + '/launch' and message.payload == b'PRESS' and not message.retain:
            threading.Thread(target=launcher.manual, daemon=True).start()
        elif message.topic == PREFIX + '/shutdown' and not message.retain:
            try:
                request = json.loads(message.payload)
            except (ValueError, UnicodeDecodeError):
                return
            threading.Thread(target=launcher.power_off, args=(request,), daemon=True).start()
        elif message.topic == PREFIX + '/recover/result' and not message.retain:
            bridge.receive(message.payload)
        elif message.topic.startswith(PREFIX + '/path/') and message.payload == b'PRESS' and not message.retain:
            name = message.topic.rsplit('/', 1)[1]
            if PATH_NAME.fullmatch(name):
                threading.Thread(target=launcher.run_path, args=(name,), daemon=True).start()
        elif message.topic == PREFIX + '/titles/discover' and message.payload == b'PRESS' and not message.retain:
            threading.Thread(target=launcher.discover_titles, args=(publish_titles,), daemon=True).start()
        elif message.topic == PREFIX + '/auto/set' and message.payload in (b'ON', b'OFF') and not message.retain:
            launcher.set_enabled(message.payload == b'ON')
            publish_auto()

    def monitor():
        while True:
            # Do not consume a boot while MQTT is down: reporting/control must be available first.
            if client.is_connected():
                try:
                    launcher.tick()
                except Exception as error:
                    print(f'Automatic monitor stopped: {error}', flush=True)
                    # Fail closed. Restarting the process still preserves the claimed wake.
                    launcher.set_enabled(False)
                    publish_auto()
                    publish_result({'state': f'failed: automatic monitor: {error}'[:250],
                                    'source': 'automatic', 'launched': None})
            time.sleep(1)

    client.on_connect, client.on_message = on_connect, on_message
    client.connect(broker['host'], broker.get('port', 1883), keepalive=30)
    threading.Thread(target=monitor, daemon=True).start()
    client.loop_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/var/lib/refree/.config/refree/config.json',
                        help="refree's config.json (console host, MQTT login); its folder holds this helper's state")
    parser.add_argument('--settings', help='autojb.json (default: next to --config)')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--focus-only', action='store_true')
    args = parser.parse_args()
    path = Path(args.config)
    config = json.loads(path.read_text())
    settings = Path(args.settings) if args.settings else path.parent / 'autojb.json'
    if settings.exists():
        configure(json.loads(settings.read_text()))
    if args.serve:
        serve(config, path.parent)
    else:
        result = operate(config, path.parent, args.focus_only)
        print(json.dumps(result, indent=2))
        return int(result['state'].startswith(('refused:', 'failed:')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
