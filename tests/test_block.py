import pytest

from wakeful.block import (
    Mode,
    Session,
    describe_duration,
    describe_remaining,
    parse_duration,
)


def test_start_sets_end_from_now():
    s = Session()
    block = s.start(3600, Mode.SLEEP, now=100)
    assert block.ends_at == 3700
    assert block.remaining(1000) == 2700
    assert s.active


def test_start_replaces_running_block():
    s = Session()
    s.start(60, Mode.SLEEP, now=0)
    s.start(120, Mode.SCREEN, now=30)
    assert s.block is not None
    assert s.block.mode is Mode.SCREEN
    assert s.block.ends_at == 150


@pytest.mark.parametrize("bad", [0, -5])
def test_start_rejects_non_positive(bad):
    with pytest.raises(ValueError):
        Session().start(bad, Mode.SLEEP, now=0)


def test_until_off_never_expires():
    s = Session()
    block = s.start(None, Mode.SLEEP, now=0)
    assert block.remaining(10**9) is None
    assert not block.expired(10**9)
    assert s.reap(10**9) is None
    assert s.active


def test_expiry_reaps_at_end_not_before():
    s = Session()
    s.start(60, Mode.SLEEP, now=0)
    assert s.reap(59.9) is None
    ended = s.reap(60)
    assert ended is not None and ended.ends_at == 60
    assert not s.active


def test_remaining_never_negative():
    s = Session()
    block = s.start(60, Mode.SLEEP, now=0)
    assert block.remaining(500) == 0


def test_extend_adds_to_end():
    s = Session()
    s.start(60, Mode.SLEEP, now=0)
    s.extend(30, now=10)
    assert s.block is not None and s.block.ends_at == 90


def test_extend_after_unreaped_expiry_counts_from_now():
    s = Session()
    s.start(60, Mode.SLEEP, now=0)
    s.extend(30, now=100)
    assert s.block is not None and s.block.ends_at == 130


def test_extend_until_off_is_unchanged():
    s = Session()
    s.start(None, Mode.SCREEN, now=0)
    s.extend(30, now=10)
    assert s.block is not None and s.block.ends_at is None


def test_extend_without_block_raises():
    with pytest.raises(RuntimeError):
        Session().extend(30, now=0)


def test_extend_rejects_non_positive():
    s = Session()
    s.start(60, Mode.SLEEP, now=0)
    with pytest.raises(ValueError):
        s.extend(0, now=0)


def test_cancel_returns_block_and_clears():
    s = Session()
    s.start(60, Mode.SLEEP, now=0)
    assert s.cancel() is not None
    assert not s.active
    assert s.cancel() is None


def test_set_mode_keeps_end():
    s = Session()
    s.start(60, Mode.SLEEP, now=0)
    s.set_mode(Mode.SCREEN)
    assert s.block is not None
    assert (s.block.mode, s.block.ends_at) == (Mode.SCREEN, 60)


@pytest.mark.parametrize(
    "seconds, text",
    [
        (None, "until turned off"),
        (0, "1 min left"),
        (1, "1 min left"),
        (60, "1 min left"),
        (61, "2 min left"),
        (47 * 60, "47 min left"),
        (3600, "1 h left"),
        (3601, "1 h 1 min left"),
        (2 * 3600 + 5 * 60, "2 h 5 min left"),
    ],
)
def test_describe_remaining_rounds_up(seconds, text):
    assert describe_remaining(seconds) == text


@pytest.mark.parametrize(
    "seconds, text",
    [
        (None, "until turned off"),
        (60, "1 minute"),
        (30 * 60, "30 minutes"),
        (3600, "1 hour"),
        (4 * 3600, "4 hours"),
        (90 * 60, "1 h 30 min"),
    ],
)
def test_describe_duration(seconds, text):
    assert describe_duration(seconds) == text


@pytest.mark.parametrize(
    "text, seconds",
    [
        ("90", 5400),
        ("45m", 2700),
        ("2h", 7200),
        ("1h30m", 5400),
        ("1h 30m", 5400),
        ("45s", 45),
        ("forever", None),
        ("until-off", None),
        (" OFF ", None),
    ],
)
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize(
    "text", ["", "0", "0m", "abc", "10x", "h", "1h30", "-5", "600000h", "2147483648s"]
)
def test_parse_duration_rejects(text):
    with pytest.raises(ValueError):
        parse_duration(text)
