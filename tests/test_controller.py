import pytest

from wakeful.block import Mode
from wakeful.claude import ClaudeSession
from wakeful.controller import Controller


class FakeInhibitor:
    def __init__(self):
        self.held = None
        self.calls = []
        self.fail = False

    def hold(self, mode, reason):
        if self.fail:
            raise RuntimeError("no PowerDevil")
        self.calls.append(("hold", mode, reason))
        self.held = (mode, reason)

    def release(self):
        self.calls.append(("release",))
        self.held = None


class FakeLid:
    def __init__(self):
        self.engaged = False
        self.calls = []
        self.fail_engage = False

    def engage(self):
        self.calls.append("engage")
        if self.fail_engage:
            raise RuntimeError("kwriteconfig6 failed")
        self.engaged = True

    def restore(self):
        self.calls.append("restore")
        self.engaged = False


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env():
    inhibitor, lid, clock = FakeInhibitor(), FakeLid(), Clock()
    c = Controller(inhibitor, lid, now=clock)
    ended = []
    c.on_ended = lambda block, why: ended.append((block, why))
    return c, inhibitor, lid, clock, ended


def test_start_holds_with_reason(env):
    c, inhibitor, _, _, _ = env
    c.start(47 * 60, Mode.SLEEP)
    assert inhibitor.held == (Mode.SLEEP, "Keeping awake: 47 min left")
    st = c.status()
    assert st.active and st.mode is Mode.SLEEP and st.remaining == 47 * 60


def test_tick_refreshes_reason(env):
    c, inhibitor, _, clock, _ = env
    c.start(3600, Mode.SLEEP)
    clock.t += 61
    c.tick()
    assert inhibitor.held == (Mode.SLEEP, "Keeping awake: 59 min left")


def test_expiry_releases_and_reports(env):
    c, inhibitor, _, clock, ended = env
    c.start(60, Mode.SCREEN)
    clock.t += 59
    c.tick()
    assert inhibitor.held is not None
    clock.t += 1
    c.tick()
    assert inhibitor.held is None
    assert not c.status().active
    assert [why for _, why in ended] == ["expired"]


def test_stop_releases_and_reports(env):
    c, inhibitor, _, _, ended = env
    c.start(None, Mode.SLEEP)
    c.stop()
    assert inhibitor.held is None
    assert [why for _, why in ended] == ["stopped"]


def test_stop_when_idle_reports_nothing(env):
    c, inhibitor, _, _, ended = env
    c.stop()
    assert ended == []
    assert inhibitor.held is None


def test_until_off_survives_ticks(env):
    c, inhibitor, _, clock, ended = env
    c.start(None, Mode.SLEEP)
    clock.t += 10**7
    c.tick()
    assert inhibitor.held == (Mode.SLEEP, "Keeping awake: until turned off")
    assert ended == []
    assert c.status().remaining is None


def test_extend_updates_reason(env):
    c, inhibitor, _, _, _ = env
    c.start(30 * 60, Mode.SLEEP)
    c.extend(60 * 60)
    assert inhibitor.held == (Mode.SLEEP, "Keeping awake: 1 h 30 min left")


def test_extend_without_block_raises(env):
    c, *_ = env
    with pytest.raises(RuntimeError):
        c.extend(60)


def test_set_mode_while_running(env):
    c, inhibitor, _, _, _ = env
    c.start(3600, Mode.SLEEP)
    c.set_mode(Mode.SCREEN)
    assert inhibitor.held[0] is Mode.SCREEN
    assert c.status().mode is Mode.SCREEN


def test_set_mode_failure_keeps_old_mode(env):
    c, inhibitor, _, _, _ = env
    c.start(3600, Mode.SLEEP)
    inhibitor.fail = True
    with pytest.raises(RuntimeError):
        c.set_mode(Mode.SCREEN)
    assert c.status().mode is Mode.SLEEP


def test_start_failure_leaves_nothing_running(env):
    c, inhibitor, lid, _, _ = env
    c.ignore_lid = True
    inhibitor.fail = True
    with pytest.raises(RuntimeError):
        c.start(60, Mode.SLEEP)
    assert not c.status().active
    assert not lid.engaged


def test_failed_restart_keeps_the_running_block_and_lid(env):
    c, inhibitor, lid, _, _ = env
    c.ignore_lid = True
    c.start(3600, Mode.SLEEP)
    inhibitor.fail = True
    with pytest.raises(RuntimeError):
        c.start(60, Mode.SCREEN)
    st = c.status()
    assert st.active and st.mode is Mode.SLEEP and st.remaining == 3600
    assert lid.engaged and st.lid_ignored
    assert inhibitor.held == (Mode.SLEEP, "Keeping awake: 1 h left")


def test_tick_failure_keeps_block(env):
    c, inhibitor, _, clock, _ = env
    c.start(3600, Mode.SLEEP)
    inhibitor.fail = True
    clock.t += 120
    c.tick()
    assert c.status().active


def test_lid_engaged_only_while_running(env):
    c, _, lid, clock, _ = env
    c.ignore_lid = True
    assert not lid.engaged
    c.start(60, Mode.SLEEP)
    assert lid.engaged and c.status().lid_ignored
    clock.t += 60
    c.tick()
    assert not lid.engaged
    assert lid.calls == ["engage", "restore"]


def test_lid_restored_on_stop_and_shutdown(env):
    c, _, lid, _, _ = env
    c.ignore_lid = True
    c.start(None, Mode.SLEEP)
    c.stop()
    assert not lid.engaged
    c.start(None, Mode.SLEEP)
    c.shutdown()
    assert not lid.engaged


def test_lid_toggled_during_block(env):
    c, _, lid, _, _ = env
    c.start(3600, Mode.SLEEP)
    assert not lid.engaged
    c.ignore_lid = True
    assert lid.engaged
    c.ignore_lid = False
    assert not lid.engaged


def test_lid_engage_failure_does_not_stop_block(env):
    c, inhibitor, lid, _, _ = env
    lid.fail_engage = True
    c.ignore_lid = True
    c.start(3600, Mode.SLEEP)
    assert c.status().active
    assert not c.status().lid_ignored
    assert inhibitor.held is not None


def test_ignore_lid_without_guard_raises():
    c = Controller(FakeInhibitor(), None, now=Clock())
    with pytest.raises(RuntimeError):
        c.ignore_lid = True
    c.ignore_lid = False


def test_shutdown_releases_without_reporting(env):
    c, inhibitor, _, _, ended = env
    c.start(3600, Mode.SCREEN)
    c.shutdown()
    assert inhibitor.held is None
    assert ended == []


def test_on_changed_fires(env):
    c, *_ = env
    seen = []
    c.on_changed = seen.append
    c.start(60, Mode.SLEEP)
    c.stop()
    assert [s.active for s in seen] == [True, False]


class FakeClaude:
    def __init__(self):
        self.sessions = []

    def working(self):
        return self.sessions


def session(name, status="busy"):
    return ClaudeSession(1, name, status, "/work")


@pytest.fixture
def claude_env(env):
    _, inhibitor, lid, clock, ended = env
    claude = FakeClaude()
    c = Controller(inhibitor, lid, now=clock, claude=claude)
    c.on_ended = lambda block, why: ended.append((block, why))
    c.watch_claude = True
    return c, inhibitor, lid, clock, claude


def test_working_claude_holds_sleep_only_until_it_stops(claude_env):
    c, inhibitor, lid, _, claude = claude_env
    c.tick()
    assert inhibitor.held is None
    claude.sessions = [session("fix the tests"), session("docs", "shell")]
    c.tick()
    assert inhibitor.held == (Mode.SLEEP, "Claude is working: fix the tests, docs")
    status = c.status()
    assert not status.active and status.held
    assert status.text == "Keeping awake: Claude is working"
    assert not lid.engaged
    claude.sessions = []
    c.tick()
    assert inhibitor.held is None
    assert not c.status().held


def test_reason_names_at_most_three_sessions(claude_env):
    c, inhibitor, _, _, claude = claude_env
    claude.sessions = [session(n) for n in "abcde"]
    c.tick()
    assert inhibitor.held[1] == "Claude is working: a, b, c, 2 more"


def test_block_mode_and_reason_win_while_both_hold(claude_env):
    c, inhibitor, _, _, claude = claude_env
    claude.sessions = [session("a")]
    c.start(60, Mode.SCREEN)
    c.tick()
    assert inhibitor.held == (Mode.SCREEN, "Keeping awake: 1 min left")
    assert c.status().claude == (session("a"),)


def test_block_expiring_falls_back_to_claude_not_release(claude_env):
    c, inhibitor, _, clock, claude = claude_env
    claude.sessions = [session("a")]
    c.start(60, Mode.SCREEN)
    inhibitor.calls.clear()
    clock.t += 61
    c.tick()
    assert inhibitor.held == (Mode.SLEEP, "Claude is working: a")
    assert ("release",) not in inhibitor.calls


def test_stop_ends_the_block_but_claude_still_holds(claude_env):
    c, inhibitor, _, _, claude = claude_env
    claude.sessions = [session("a")]
    c.start(None, Mode.SLEEP)
    c.stop()
    assert inhibitor.held == (Mode.SLEEP, "Claude is working: a")


def test_failed_start_keeps_claudes_hold(claude_env):
    c, inhibitor, _, _, claude = claude_env
    claude.sessions = [session("a")]
    c.tick()
    inhibitor.fail = True
    with pytest.raises(RuntimeError):
        c.start(60, Mode.SCREEN)
    assert inhibitor.held == (Mode.SLEEP, "Claude is working: a")


def test_turning_the_option_off_releases(claude_env):
    c, inhibitor, _, _, claude = claude_env
    claude.sessions = [session("a")]
    c.tick()
    c.watch_claude = False
    assert inhibitor.held is None
    assert c.status().claude == ()
    c.tick()
    assert inhibitor.held is None


def test_watcher_error_is_treated_as_no_sessions(claude_env):
    c, inhibitor, _, _, claude = claude_env
    claude.sessions = [session("a")]
    c.tick()
    claude.working = lambda: 1 / 0
    c.tick()
    assert inhibitor.held is None


def test_watch_claude_without_watcher_raises():
    c = Controller(FakeInhibitor(), None, now=Clock())
    with pytest.raises(RuntimeError):
        c.watch_claude = True
    c.watch_claude = False


def test_shutdown_releases_claudes_hold(claude_env):
    c, inhibitor, _, _, claude = claude_env
    claude.sessions = [session("a")]
    c.tick()
    c.shutdown()
    assert inhibitor.held is None
