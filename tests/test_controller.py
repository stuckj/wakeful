import pytest

from wakeful.block import Mode
from wakeful.claude import ClaudeSession
from wakeful.controller import CLAUDE_GRACE, Controller


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
    assert inhibitor.calls == [("release",)]


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
        self.installed = True
        self.reads = 0

    def available(self):
        return self.installed

    def working(self):
        self.reads += 1
        return self.sessions


def session(name, status="busy"):
    return ClaudeSession(1, name, status, "/work")


@pytest.fixture
def claude_env(env):
    _, inhibitor, lid, clock, ended = env
    claude = FakeClaude()
    c = Controller(inhibitor, lid, now=clock, claude=claude)
    c.on_ended = lambda block, why: ended.append((block, why))
    return c, inhibitor, lid, clock, claude, ended


def test_while_claude_holds_in_the_chosen_mode_and_names_the_sessions(claude_env):
    c, inhibitor, _, _, claude, _ = claude_env
    claude.sessions = [session("fix the tests"), session("docs", "shell")]
    c.start(None, Mode.SCREEN, while_claude=True)
    assert inhibitor.held == (Mode.SCREEN, "Claude is working: fix the tests, docs")
    status = c.status()
    assert status.active and status.while_claude and status.remaining is None
    assert status.text == "Keeping awake while Claude is working"
    c.set_mode(Mode.SLEEP)
    assert inhibitor.held[0] is Mode.SLEEP


def test_while_claude_started_with_nothing_working_waits_without_holding(claude_env):
    c, inhibitor, _, _, claude, _ = claude_env
    c.start(None, Mode.SLEEP, while_claude=True)
    status = c.status()
    assert status.active and status.while_claude and not status.held
    assert status.text == "Sleep allowed until Claude is working"
    assert inhibitor.held is None
    claude.sessions = [session("a")]
    c.tick()
    assert inhibitor.held == (Mode.SLEEP, "Claude is working: a")
    assert c.status().held


def test_while_claude_needs_claude_code():
    c = Controller(FakeInhibitor(), None, now=Clock())
    with pytest.raises(RuntimeError, match="Claude Code was not found"):
        c.start(None, Mode.SLEEP, while_claude=True)


def test_claude_code_installed_after_startup_is_seen(claude_env):
    c, inhibitor, _, _, claude, _ = claude_env
    claude.installed = False
    assert not c.claude_available
    with pytest.raises(RuntimeError, match="Claude Code was not found"):
        c.start(None, Mode.SLEEP, while_claude=True)
    claude.installed = True
    claude.sessions = [session("a")]
    assert c.claude_available
    c.start(None, Mode.SLEEP, while_claude=True)
    assert inhibitor.held is not None


def test_while_claude_releases_after_the_grace_period_and_keeps_waiting(claude_env):
    c, inhibitor, _, clock, claude, ended = claude_env
    claude.sessions = [session("a")]
    c.start(None, Mode.SLEEP, while_claude=True)
    claude.sessions = []
    clock.t += CLAUDE_GRACE - 1
    c.tick()
    assert inhibitor.held == (Mode.SLEEP, "Keeping awake while Claude is working")
    claude.sessions = [session("a")]  # back to work within the grace period
    c.tick()
    claude.sessions = []
    clock.t += CLAUDE_GRACE - 1
    c.tick()
    assert c.status().held
    clock.t += 1
    c.tick()
    assert inhibitor.held is None
    assert c.status().active and not c.status().held
    assert ended == []
    claude.sessions = [session("b")]
    c.tick()
    assert inhibitor.held == (Mode.SLEEP, "Claude is working: b")


def test_lid_is_ignored_only_while_claude_holds(claude_env):
    c, _, lid, clock, claude, _ = claude_env
    c.ignore_lid = True
    c.start(None, Mode.SLEEP, while_claude=True)
    assert not lid.engaged
    claude.sessions = [session("a")]
    c.tick()
    assert lid.engaged and c.status().lid_ignored
    claude.sessions = []
    clock.t += CLAUDE_GRACE
    c.tick()
    assert not lid.engaged


def test_timed_block_lists_working_sessions_but_keeps_its_reason(claude_env):
    c, inhibitor, _, clock, claude, ended = claude_env
    claude.sessions = [session("a")]
    c.start(60, Mode.SLEEP)
    assert c.status().claude == (session("a"),)
    assert inhibitor.held == (Mode.SLEEP, "Keeping awake: 1 min left")
    clock.t += 61
    c.tick()
    assert inhibitor.held is None  # the timer decides, not Claude
    assert c.status().claude == ()
    assert ended[0][1] == "expired"


def test_no_sessions_are_read_without_a_block(claude_env):
    c, _, _, _, claude, _ = claude_env
    claude.sessions = [session("a")]
    c.tick()
    assert claude.reads == 0


def test_reason_names_at_most_three_sessions(claude_env):
    c, inhibitor, _, _, claude, _ = claude_env
    claude.sessions = [session(n) for n in "abcde"]
    c.start(None, Mode.SLEEP, while_claude=True)
    assert inhibitor.held[1] == "Claude is working: a, b, c, 2 more"


def test_watcher_error_counts_as_nothing_working(claude_env):
    c, inhibitor, _, clock, claude, _ = claude_env
    claude.sessions = [session("a")]
    c.start(None, Mode.SLEEP, while_claude=True)
    claude.working = lambda: 1 / 0
    clock.t += CLAUDE_GRACE
    c.tick()
    assert inhibitor.held is None


def test_failed_start_keeps_the_grace_of_the_running_while_claude_block(claude_env):
    c, inhibitor, _, clock, claude, _ = claude_env
    claude.sessions = [session("a")]
    c.start(None, Mode.SLEEP, while_claude=True)
    claude.sessions = []
    clock.t += CLAUDE_GRACE - 10
    c.tick()
    claude.sessions = [session("b")]  # seen by the failing start only
    inhibitor.fail = True
    with pytest.raises(RuntimeError):
        c.start(3600, Mode.SLEEP)
    inhibitor.fail = False
    assert c.status().claude == ()
    claude.sessions = []
    clock.t += 10
    c.tick()
    assert inhibitor.held is None
    assert c.status().while_claude


def test_restarting_a_while_claude_block_keeps_its_grace(claude_env):
    c, inhibitor, _, clock, claude, _ = claude_env
    claude.sessions = [session("a")]
    c.start(None, Mode.SLEEP, while_claude=True)
    claude.sessions = []
    clock.t += 20
    c.start(None, Mode.SCREEN, while_claude=True)
    assert inhibitor.held == (Mode.SCREEN, "Keeping awake while Claude is working")
    clock.t += CLAUDE_GRACE - 20
    c.tick()
    assert inhibitor.held is None


def test_lid_follows_claude_after_a_failed_start(claude_env):
    c, inhibitor, lid, _, claude, _ = claude_env
    c.ignore_lid = True
    c.start(None, Mode.SLEEP, while_claude=True)
    inhibitor.fail = True
    with pytest.raises(RuntimeError):
        c.start(3600, Mode.SLEEP)
    inhibitor.fail = False
    claude.sessions = [session("a")]
    c.tick()
    assert inhibitor.held is not None
    assert lid.engaged


def test_grace_does_not_carry_over_from_a_stopped_block(claude_env):
    c, inhibitor, _, _, claude, _ = claude_env
    claude.sessions = [session("a")]
    c.start(3600, Mode.SLEEP)
    c.stop()
    claude.sessions = []
    c.start(None, Mode.SLEEP, while_claude=True)
    assert inhibitor.held is None


def test_a_failing_lid_change_is_not_retried_on_every_tick(claude_env):
    c, _, lid, clock, _, _ = claude_env
    c.ignore_lid = True
    lid.fail_engage = True
    c.start(3600, Mode.SLEEP)
    for _ in range(3):
        clock.t += 5
        c.tick()
    assert lid.calls == ["engage"]


def test_failed_while_claude_start_keeps_the_running_block(claude_env):
    c, inhibitor, _, _, claude, _ = claude_env
    c.start(3600, Mode.SLEEP)
    claude.sessions = [session("a")]
    inhibitor.fail = True
    with pytest.raises(RuntimeError):
        c.start(None, Mode.SCREEN, while_claude=True)
    status = c.status()
    assert status.active and not status.while_claude and status.remaining == 3600
