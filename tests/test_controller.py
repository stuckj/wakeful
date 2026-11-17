import pytest

from wakeful.agents import AgentSession
from wakeful.block import Mode
from wakeful.controller import AGENT_GRACE, Controller


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


class FakeAgent:
    def __init__(self, key="claude", label="Claude Code"):
        self.key, self.label = key, label
        self.sessions = []
        self.installed = True
        self.reads = 0

    def available(self):
        return self.installed

    def working(self):
        self.reads += 1
        return tuple(self.sessions)


def session(name, agent="Claude Code"):
    return AgentSession(agent, 1, name, "working", "/work")


@pytest.fixture
def agents_env(env):
    _, inhibitor, lid, clock, ended = env
    claude, codex = FakeAgent(), FakeAgent("codex", "Codex")
    c = Controller(inhibitor, lid, now=clock, agents=[claude, codex])
    c.on_ended = lambda block, why: ended.append((block, why))
    c.set_watched("codex", False)
    return c, inhibitor, lid, clock, claude, codex, ended


def test_agentic_checks_hold_only_while_an_agent_works(agents_env):
    c, inhibitor, _, clock, claude, _, _ = agents_env
    c.set_mode(Mode.SCREEN)
    c.set_agents_enabled(True)
    status = c.status()
    assert status.watching and not status.held and not status.active
    assert status.text == "Watching agents"
    assert inhibitor.held is None
    claude.sessions = [session("fix the tests")]
    c.tick()
    assert inhibitor.held == (Mode.SCREEN, "Agents working: fix the tests")
    assert c.status().held
    claude.sessions = []
    clock.t += AGENT_GRACE - 1
    c.tick()
    assert inhibitor.held is not None
    clock.t += 1
    c.tick()
    assert inhibitor.held is None
    assert c.status().watching and c.status().agents_enabled


def test_unticked_and_disabled_agents_are_not_watched(agents_env):
    c, inhibitor, _, _, claude, codex, _ = agents_env
    codex.sessions = [session("x", "Codex")]
    c.set_agents_enabled(True)
    c.tick()
    assert inhibitor.held is None and codex.reads == 0
    claude.sessions = [session("a")]
    c.set_agents_enabled(False)
    claude.reads = 0
    c.tick()
    assert inhibitor.held is None and claude.reads == 0
    assert not c.status().watching


def test_turning_agents_off_keeps_the_ticks(agents_env):
    c, _, _, _, _, _, _ = agents_env
    c.set_watched("codex", True)
    c.set_agents_enabled(True)
    c.set_agents_enabled(False)
    assert c.status().watched == ("claude", "codex")


def test_not_watching_when_no_ticked_agent_is_installed(agents_env):
    c, _, _, _, claude, _, _ = agents_env
    claude.installed = False
    c.set_agents_enabled(True)
    assert not c.status().watching


def test_unknown_agent_is_refused(agents_env):
    c, *_ = agents_env
    with pytest.raises(ValueError, match="unknown agent"):
        c.set_watched("cursor", True)


def test_timer_and_agents_hold_together_and_the_timer_ending_hands_over(agents_env):
    c, inhibitor, _, clock, claude, _, ended = agents_env
    c.set_agents_enabled(True)
    claude.sessions = [session("a")]
    c.start(60, Mode.SLEEP)
    assert inhibitor.held == (Mode.SLEEP, "Keeping awake: 1 min left")
    assert c.status().sessions == (session("a"),)
    clock.t += 61
    c.tick()
    assert ended[0][1] == "expired"
    assert inhibitor.held == (Mode.SLEEP, "Agents working: a")
    assert c.status().agents_enabled


def test_stopping_the_timer_leaves_agentic_checks_on(agents_env):
    c, inhibitor, _, _, claude, _, _ = agents_env
    c.set_agents_enabled(True)
    c.start(None, Mode.SLEEP)
    c.stop()
    assert inhibitor.held is None
    assert c.status().watching
    claude.sessions = [session("a")]
    c.tick()
    assert inhibitor.held is not None


def test_timer_lists_ticked_sessions_without_agentic_checks(agents_env):
    c, inhibitor, _, _, claude, _, _ = agents_env
    claude.sessions = [session("a")]
    c.start(3600, Mode.SLEEP)
    assert c.status().sessions == (session("a"),)
    c.stop()
    assert inhibitor.held is None
    assert c.status().sessions == ()


def test_nothing_is_read_while_off(agents_env):
    c, _, _, _, claude, _, _ = agents_env
    claude.sessions = [session("a")]
    c.tick()
    assert claude.reads == 0


def test_a_watcher_that_keeps_failing_is_logged_once(agents_env, caplog):
    c, _, _, _, claude, _, _ = agents_env
    claude.working = lambda: 1 / 0
    c.set_agents_enabled(True)
    for _ in range(3):
        c.tick()
    assert sum(r.levelname == "ERROR" for r in caplog.records) == 1


def test_an_agent_that_is_not_installed_is_not_read(agents_env):
    c, _, _, _, _, codex, _ = agents_env
    c.set_watched("codex", True)
    codex.installed = False
    c.start(3600, Mode.SLEEP)
    assert codex.reads == 0


def test_reason_names_at_most_three_sessions(agents_env):
    c, inhibitor, _, _, claude, _, _ = agents_env
    claude.sessions = [session(n) for n in "abcde"]
    c.set_agents_enabled(True)
    assert inhibitor.held[1] == "Agents working: a, b, c, 2 more"


def test_one_failing_agent_does_not_hide_the_others(agents_env):
    c, inhibitor, _, _, claude, codex, _ = agents_env
    c.set_watched("codex", True)
    claude.working = lambda: 1 / 0
    codex.sessions = [session("x", "Codex")]
    c.set_agents_enabled(True)
    assert inhibitor.held == (Mode.SLEEP, "Agents working: x")


def test_mode_change_applies_to_agentic_holds(agents_env):
    c, inhibitor, _, _, claude, _, _ = agents_env
    claude.sessions = [session("a")]
    c.set_agents_enabled(True)
    c.set_mode(Mode.SCREEN)
    assert inhibitor.held[0] is Mode.SCREEN


def test_a_timer_started_in_another_mode_does_not_change_the_chosen_one(agents_env):
    c, _, _, _, _, _, _ = agents_env
    c.start(60, Mode.SCREEN)
    assert c.status().mode is Mode.SCREEN
    c.stop()
    assert c.status().mode is Mode.SLEEP


def test_lid_follows_agentic_holding(agents_env):
    c, _, lid, clock, claude, _, _ = agents_env
    c.ignore_lid = True
    c.set_agents_enabled(True)
    assert not lid.engaged
    claude.sessions = [session("a")]
    c.tick()
    assert lid.engaged and c.status().lid_ignored
    claude.sessions = []
    clock.t += AGENT_GRACE
    c.tick()
    assert not lid.engaged


@pytest.mark.parametrize("change", ["set_mode", "turn_off"])
def test_lid_follows_a_release_made_outside_the_tick(agents_env, change):
    c, inhibitor, lid, _, claude, _, _ = agents_env
    c.ignore_lid = True
    claude.sessions = [session("a")]
    c.set_agents_enabled(True)
    assert lid.engaged
    claude.sessions = []
    if change == "set_mode":
        c.set_mode(Mode.SCREEN)  # within the grace: still held
        assert lid.engaged
        return
    c.set_agents_enabled(False)
    assert inhibitor.held is None
    assert not lid.engaged


def test_failed_hold_is_not_reported_as_held(agents_env):
    c, inhibitor, lid, _, claude, _, _ = agents_env
    c.ignore_lid = True
    c.set_agents_enabled(True)
    claude.sessions = [session("a")]
    inhibitor.fail = True
    c.tick()
    assert not c.status().held and not lid.engaged
    inhibitor.fail = False
    c.tick()
    assert c.status().held and lid.engaged


def test_failed_timer_start_leaves_agentic_holding_alone(agents_env):
    c, inhibitor, lid, _, claude, _, _ = agents_env
    c.ignore_lid = True
    claude.sessions = [session("a")]
    c.set_agents_enabled(True)
    inhibitor.fail = True
    with pytest.raises(RuntimeError):
        c.start(3600, Mode.SCREEN)
    inhibitor.fail = False
    assert not c.status().active and c.status().held
    assert inhibitor.held == (Mode.SLEEP, "Agents working: a")
    assert lid.engaged


def test_a_failing_lid_change_is_not_retried_on_every_tick(agents_env):
    c, _, lid, clock, _, _, _ = agents_env
    c.ignore_lid = True
    lid.fail_engage = True
    c.start(3600, Mode.SLEEP)
    for _ in range(3):
        clock.t += 5
        c.tick()
    assert lid.calls == ["engage"]


def test_shutdown_releases_agentic_holding(agents_env):
    c, inhibitor, lid, _, claude, _, _ = agents_env
    c.ignore_lid = True
    claude.sessions = [session("a")]
    c.set_agents_enabled(True)
    c.shutdown()
    assert inhibitor.held is None and not lid.engaged


def test_the_grace_period_can_be_changed(agents_env):
    c, inhibitor, _, clock, claude, _, _ = agents_env
    c.set_agents_enabled(True)
    c.set_grace(300)
    assert c.status().grace == 300
    claude.sessions = [session("a")]
    c.tick()
    claude.sessions = []
    clock.t += 299
    c.tick()
    assert inhibitor.held is not None
    clock.t += 1
    c.tick()
    assert inhibitor.held is None


def test_no_grace_releases_as_soon_as_agents_stop(agents_env):
    c, inhibitor, _, _, claude, _, _ = agents_env
    c.set_agents_enabled(True)
    c.set_grace(0)
    claude.sessions = [session("a")]
    c.tick()
    claude.sessions = []
    c.tick()
    assert inhibitor.held is None


def test_shortening_the_grace_releases_at_once_if_already_past_it(agents_env):
    c, inhibitor, _, clock, claude, _, _ = agents_env
    c.set_agents_enabled(True)
    claude.sessions = [session("a")]
    c.tick()
    claude.sessions = []
    clock.t += 30
    c.tick()
    assert inhibitor.held is not None
    c.set_grace(10)
    assert inhibitor.held is None


def test_a_negative_grace_is_refused(agents_env):
    c, *_ = agents_env
    with pytest.raises(ValueError):
        c.set_grace(-1)


def test_unticking_the_working_agent_drops_its_grace(agents_env):
    c, inhibitor, _, _, claude, _, _ = agents_env
    c.set_watched("codex", True)
    c.set_grace(900)
    c.set_agents_enabled(True)
    claude.sessions = [session("a")]
    c.tick()
    c.set_watched("claude", False)
    assert inhibitor.held is None


def test_the_grace_left_is_shown_while_agents_hold_with_none_working(agents_env):
    c, inhibitor, _, clock, claude, _, _ = agents_env
    c.set_grace(300)
    c.set_agents_enabled(True)
    claude.sessions = [session("a")]
    c.tick()
    assert c.status().grace_left is None
    claude.sessions = []
    clock.t += 60
    c.tick()
    status = c.status()
    assert status.grace_left == 240
    assert status.text == "Keeping awake: agents stopped, 4 min left"
    assert inhibitor.held[1] == "Keeping awake: agents stopped, 4 min left"


def test_status_says_agents_stopped_until_the_tick_after_the_grace(agents_env):
    c, _, _, clock, claude, _, _ = agents_env
    c.set_agents_enabled(True)
    claude.sessions = [session("a")]
    c.tick()
    claude.sessions = []
    c.tick()
    clock.t += AGENT_GRACE + 2  # past the grace, before the next tick
    status = c.status()
    assert status.held and status.grace_left == 0
    assert status.text.startswith("Keeping awake: agents stopped")
