"""Moved targets, uncertain frames and crashes must never launch the wrong app."""
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('ps5_autojb', Path(__file__).resolve().parents[1] / 'launcher/ps5_autojb.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class Console:
    def __init__(self, titles, confirmed=True):
        self.titles = titles
        self.index = 0
        self.closed = False
        self.opened = False
        self.launched = []
        self.jailbroken = False
        self.confirmed = confirmed
        self.reads = 0
        self.changed_title = None
        self.error = None
        self.notes = []

    def awake(self):
        if self.error == 'off':
            raise M.Refused('off')

    def power(self):
        powers = getattr(self, 'powers', [])
        return powers.pop(0) if powers else 'awake'

    def probe(self):
        assert not getattr(self, 'live', False), 'Remote Play session still open while Y2JB starts'
        return self.jailbroken or (bool(self.launched) and self.confirmed)

    def open(self):
        if self.error == 'busy':
            raise M.Refused('busy')
        self.opened = True
        self.live = True
        self.sessions = getattr(self, 'sessions', 0) + 1

    def close(self):
        self.closed = True
        self.live = False

    def media(self):
        if self.error == 'dialog':
            raise M.Refused('Home screen not recognized')

    def title(self):
        self.reads += 1
        if self.changed_title and self.reads == 2:
            return self.changed_title
        if self.error == 'screenshot':
            raise M.Refused('no screenshot')
        return self.titles[self.index]


    def press(self, button, wait=0):
        if button == 'right':
            self.index = min(self.index + 1, len(self.titles) - 1)
        elif button == 'cross':
            self.launched.append(self.titles[self.index][0])
        else:
            pytest.fail(f'unexpected button {button}')

    def wait(self, seconds):
        pass

    def proof(self):
        pass

    def report(self, text):
        self.notes.append(text)


@pytest.fixture
def budget(tmp_path):
    return M.Budget(tmp_path / 'attempts.json', now=lambda: 2000)


@pytest.mark.parametrize('position', [1, 2, 5, 10, 24])
def test_moved_target_is_found_by_name(position, budget):
    titles = [('Other media app', 95)] * M.MAX_TILES
    titles[position - 1] = ('Y2JB - Gaming', 65)
    console = Console(titles)
    result = M.launch(console, budget)
    assert console.launched == ['Y2JB - Gaming']
    assert len(result['scanned']) == position
    assert console.closed
    assert budget.path.read_text() == '[]'


@pytest.mark.parametrize('text,confidence', [
    ('Y2JB', 99), ('Gaming', 99), ('Y2JB - Gaming Tools', 99),
    ('Y2JB - Gaming', 49), ('Y2)B - Gaming', 90), ('', 0),
])
def test_missing_or_ambiguous_target_never_launches(text, confidence, budget):
    console = Console([(text, confidence)])
    with pytest.raises(M.Refused, match='not positively identified'):
        M.launch(console, budget)
    assert console.launched == []
    assert console.closed
    assert console.reads == M.MAX_TILES


def test_selected_title_changes_before_cross(budget):
    console = Console([('Y2JB - Gaming', 80)])
    console.changed_title = ('Y2JB', 90)
    with pytest.raises(M.Refused, match='changed'):
        M.launch(console, budget)
    assert console.launched == []
    assert console.closed


@pytest.mark.parametrize('error', ['off', 'busy', 'dialog', 'screenshot'])
def test_refusals_never_launch_or_close_someone_elses_session(error, budget):
    console = Console([('Y2JB - Gaming', 80)])
    console.error = error
    with pytest.raises(M.Refused):
        M.launch(console, budget)
    assert console.launched == []
    assert console.closed == console.opened


def test_already_jailbroken_does_nothing(budget):
    console = Console([])
    console.jailbroken = True
    assert M.launch(console, budget)['state'] == 'already jailbroken'
    assert not console.opened
    assert not budget.path.exists()


def test_focus_only_identifies_without_launching_or_consuming_budget(budget):
    console = Console([('Y2JB', 95), ('Y2JB - Gaming', 90)])
    result = M.launch(console, budget, focus_only=True)
    assert result['state'] == 'target verified; not launched'
    assert console.launched == []
    assert console.closed
    assert not budget.path.exists()


def test_no_retry_when_exploit_does_not_confirm(budget):
    console = Console([('Y2JB - Gaming', 80)], confirmed=False)
    with pytest.raises(M.Refused, match='did not confirm'):
        M.launch(console, budget)
    assert len(console.launched) == 1
    assert console.closed


def test_failure_budget_survives_restart_and_expires(tmp_path):
    path = tmp_path / 'attempts.json'
    for _ in range(3):
        M.Budget(path, now=lambda: 2000).reserve()
    with pytest.raises(M.Refused, match='Three failed'):
        M.Budget(path, now=lambda: 2100).reserve()
    M.Budget(path, now=lambda: 3801).reserve()


def test_invalid_budget_is_not_silently_reset(tmp_path):
    path = tmp_path / 'attempts.json'
    path.write_text('{"broken":true}')
    with pytest.raises(M.Refused):
        M.Budget(path).reserve()


@pytest.mark.parametrize('active', [0, 1])
def test_real_navigation_activates_media_before_selecting_first_unknown_tile(tmp_path, monkeypatch, active):
    console = M.Console({'host': 'example.invalid'}, tmp_path)
    state = {'active': active, 'row': 'tiles', 'tab': active, 'tile': 8, 'launches': []}
    def api(path, data=None):
        if path == '/api/maker/home':
            state['row'] = 'tiles'
        elif path == '/api/maker/press':
            b = data['button']
            if b == 'up':
                state.update(row='tabs', tab=state['active'])
            elif b in ('left', 'right'):
                key = 'tab' if state['row'] == 'tabs' else 'tile'
                state[key] = max(0, min(24, state[key] + (-1 if b == 'left' else 1)))
            elif b == 'cross':
                if state['row'] == 'tabs':
                    state.update(active=state['tab'], row='tiles')
                else:
                    state['launches'].append(state['tile'])
    monkeypatch.setattr(console, 'api', api)
    monkeypatch.setattr(console, 'wait', lambda _: None)
    monkeypatch.setattr(console, 'snapshot', lambda: b'frame')
    monkeypatch.setattr(M, 'ocr', lambda *args: ('Games Media', 95))
    console.media()
    assert (state['active'], state['row'], state['tile']) == (1, 'tiles', 1)
    assert state['launches'] == []


def test_unconfirmed_launch_is_not_reported_as_no_input(tmp_path, monkeypatch):
    console = Console([('Y2JB - Gaming', 80)], confirmed=False)
    monkeypatch.setattr(M, 'Console', lambda *args: console)
    result = M.operate({}, tmp_path)
    assert result['state'].startswith('failed:')
    assert result['launch_attempted'] is True
    assert result['launched'] is None



def test_one_cross_and_no_remote_play_while_y2jb_starts(budget):
    # 6 Nov: refree segfaulted when a session was open, or opened, while Y2JB was starting. Exactly one Cross,
    # one session, closed before confirmation is awaited; an unopened tile is left to the restart/recovery path.
    console = Console([('Y2JB - Gaming', 80)], confirmed=False)
    with pytest.raises(M.Refused, match='did not confirm'):
        M.launch(console, budget)
    assert console.launched == ['Y2JB - Gaming'] and console.sessions == 1 and not console.live


def test_console_crashing_after_cross_fails_in_seconds_not_three_minutes(budget):
    # 6 Nov 12:13: Y2JB was selected and the console panicked off 6 s later; the launcher waited the full 180 s.
    console = Console([('Y2JB - Gaming', 80)], confirmed=False)
    console.powers = ['awake', 'offline', 'offline', 'offline']
    waits = []
    console.wait = waits.append
    with pytest.raises(M.Refused, match='went offline'):
        M.launch(console, budget)
    assert console.launched == ['Y2JB - Gaming'] and sum(waits) <= 10


def test_a_single_missed_reply_does_not_end_the_wait(budget):
    console = Console([('Y2JB - Gaming', 80)], confirmed=False)
    console.powers = ['offline', 'awake', 'offline', 'offline', 'awake'] + ['awake'] * 100
    with pytest.raises(M.Refused, match='180 seconds'):
        M.launch(console, budget)


@pytest.mark.parametrize('error', ['busy', 'dialog', 'screenshot'])
def test_a_refusal_before_y2jb_opens_does_not_use_up_an_attempt(error, budget):
    # 6 Nov: "refused: HTTP Error 502" (nothing launched) counted, and the post-recovery retry hit the 3-in-30 limit.
    console = Console([('Y2JB - Gaming', 80)])
    console.error = error
    with pytest.raises(M.Refused):
        M.launch(console, budget)
    assert budget.path.read_text() == '[]'


def test_an_opened_y2jb_that_never_confirms_still_counts(budget):
    console = Console([('Y2JB - Gaming', 80)], confirmed=False)
    with pytest.raises(M.Refused):
        M.launch(console, budget)
    assert len(json.loads(budget.path.read_text())) == 1
