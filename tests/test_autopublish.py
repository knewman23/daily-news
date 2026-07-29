"""Coalescing publisher. No git, no network: publish() is injected.

The timer is injected too. A test that slept for the real quiet window would make
the suite slower than everything else in it put together, and testing "did it
wait" by waiting is not testing anything.
"""

import threading
from datetime import date

import pytest

from src import autopublish
from src.publish import PublishResult


class FakeTimer:
    """A threading.Timer that only fires when a test says so."""

    created = []

    def __init__(self, delay, function):
        self.delay = delay
        self.function = function
        self.cancelled = False
        self.started = False
        self.daemon = False
        FakeTimer.created.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    @classmethod
    def reset(cls):
        cls.created = []

    @classmethod
    def live(cls):
        """Timers that were started and not cancelled."""
        return [t for t in cls.created if t.started and not t.cancelled]


class Cfg:
    def __init__(self, enabled=True):
        self.publish = type("P", (), {"enabled": enabled})()


class PublishSpy:
    def __init__(self, result=None, raises=None):
        self.result = result or PublishResult(True, True, "published")
        self.raises = raises
        self.calls = []

    def __call__(self, cfg, day, summary=None, **kwargs):
        self.calls.append({"day": day, "summary": summary})
        if self.raises:
            raise self.raises
        return self.result


@pytest.fixture(autouse=True)
def clean_timers():
    FakeTimer.reset()
    yield
    FakeTimer.reset()


def make(spy=None, enabled=True, delay=15.0):
    return autopublish.Publisher(
        Cfg(enabled), delay=delay,
        publisher=spy or PublishSpy(), timer_factory=FakeTimer,
    )


def fire(pub):
    """Run whatever timer is currently armed."""
    live = FakeTimer.live()
    assert live, "no timer was armed"
    live[-1].function()


# --- coalescing ------------------------------------------------------------


def test_one_edit_publishes_once_the_timer_fires(tmp_path):
    spy = PublishSpy()
    pub = make(spy)

    pub.request("2026-07-29")
    assert spy.calls == []                      # nothing yet: still in the window

    fire(pub)
    assert len(spy.calls) == 1
    assert spy.calls[0]["day"] == date(2026, 7, 29)


def test_a_run_of_edits_becomes_a_single_publish():
    spy = PublishSpy()
    pub = make(spy)

    for _ in range(6):
        pub.request("2026-07-29")
    fire(pub)

    assert len(spy.calls) == 1
    assert "6 changes" in spy.calls[0]["summary"]


def test_each_edit_restarts_the_quiet_window():
    pub = make()

    pub.request("2026-07-29")
    first = FakeTimer.live()[-1]
    pub.request("2026-07-29")

    assert first.cancelled
    assert len(FakeTimer.live()) == 1


def test_the_timer_is_a_daemon():
    """A pending push must not keep the process alive on ctrl-c."""
    pub = make()
    pub.request("2026-07-29")
    assert FakeTimer.live()[-1].daemon is True


def test_the_configured_delay_is_used():
    pub = make(delay=3.5)
    pub.request("2026-07-29")
    assert FakeTimer.live()[-1].delay == 3.5


# --- the commit subject ----------------------------------------------------


def test_a_single_day_names_that_day():
    spy = PublishSpy()
    pub = make(spy)
    pub.request("2026-07-29")
    fire(pub)

    assert spy.calls[0]["summary"] == "news: 2026-07-29 topic visibility (1 change)"


def test_several_days_are_summarised_together():
    spy = PublishSpy()
    pub = make(spy)
    for day in ("2026-07-27", "2026-07-28", "2026-07-29"):
        pub.request(day)
    fire(pub)

    assert spy.calls[0]["summary"] == "news: topic visibility on 3 days (3 changes)"


def test_the_subject_does_not_claim_to_be_a_new_day_of_news():
    """publish()'s default subject is "news: <date> (N topics)", which would be a
    lie about a hand edit."""
    spy = PublishSpy()
    pub = make(spy)
    pub.request("2026-07-29")
    fire(pub)

    assert "topic" in spy.calls[0]["summary"]
    assert spy.calls[0]["summary"] != "news: 2026-07-29"


def test_a_date_object_is_accepted_as_well_as_a_string():
    spy = PublishSpy()
    pub = make(spy)
    pub.request(date(2026, 7, 29))
    fire(pub)
    assert spy.calls[0]["day"] == date(2026, 7, 29)


# --- nothing is lost -------------------------------------------------------


def test_an_edit_during_a_publish_is_not_dropped():
    """The dangerous case: a toggle landing while git is running."""
    spy = PublishSpy()
    pub = make(spy)
    started = threading.Event()
    release = threading.Event()

    def slow(cfg, day, summary=None, **kwargs):
        spy.calls.append({"day": day, "summary": summary})
        started.set()
        release.wait(timeout=5)
        return PublishResult(True, True, "published")

    pub._publish = slow
    pub.request("2026-07-29")

    worker = threading.Thread(target=fire, args=(pub,))
    worker.start()
    assert started.wait(timeout=5)

    # Arrives mid-publish.
    pub.request("2026-07-28")
    release.set()
    worker.join(timeout=5)

    assert pub.status()["state"] == "pending"
    assert pub.status()["pending"] == 1

    pub._publish = spy
    fire(pub)
    assert len(spy.calls) == 2
    assert spy.calls[1]["day"] == date(2026, 7, 28)


def test_a_timer_firing_during_a_publish_does_not_start_a_second_one():
    spy = PublishSpy()
    pub = make(spy)
    started = threading.Event()
    release = threading.Event()

    def slow(cfg, day, summary=None, **kwargs):
        spy.calls.append({"day": day, "summary": summary})
        started.set()
        release.wait(timeout=5)
        return PublishResult(True, True, "published")

    pub._publish = slow
    pub.request("2026-07-29")
    worker = threading.Thread(target=fire, args=(pub,))
    worker.start()
    assert started.wait(timeout=5)

    pub._fire()                     # a stray timer, mid-flight
    release.set()
    worker.join(timeout=5)

    assert len(spy.calls) == 1


def test_firing_with_nothing_pending_does_nothing():
    spy = PublishSpy()
    pub = make(spy)
    pub._fire()
    assert spy.calls == []


def test_flush_publishes_without_waiting():
    spy = PublishSpy()
    pub = make(spy)
    pub.request("2026-07-29")
    pub.flush()

    assert len(spy.calls) == 1
    assert FakeTimer.created[0].cancelled


def test_flush_with_nothing_pending_is_a_no_op():
    spy = PublishSpy()
    pub = make(spy)
    pub.flush()
    assert spy.calls == []


def test_cancel_drops_the_timer():
    pub = make()
    pub.request("2026-07-29")
    pub.cancel()
    assert FakeTimer.created[0].cancelled


# --- status ----------------------------------------------------------------


def test_status_walks_from_idle_to_published():
    spy = PublishSpy()
    pub = make(spy)

    assert pub.status()["state"] == "idle"
    pub.request("2026-07-29")
    assert pub.status() == {"state": "pending", "message": "", "pending": 1}

    fire(pub)
    assert pub.status()["state"] == "published"
    assert pub.status()["pending"] == 0


def test_a_failed_publish_is_reported_rather_than_swallowed():
    """A silent failure would leave the reader believing the live site matches."""
    spy = PublishSpy(PublishResult(False, False, "git push failed: no upstream"))
    pub = make(spy)

    pub.request("2026-07-29")
    fire(pub)

    assert pub.status()["state"] == "failed"
    assert "no upstream" in pub.status()["message"]


def test_nothing_to_publish_settles_back_to_idle():
    spy = PublishSpy(PublishResult(True, False, "no change to publish"))
    pub = make(spy)

    pub.request("2026-07-29")
    fire(pub)

    assert pub.status()["state"] == "idle"


def test_a_raising_publisher_cannot_escape_into_the_request():
    spy = PublishSpy(raises=RuntimeError("git exploded"))
    pub = make(spy)

    pub.request("2026-07-29")
    fire(pub)                                    # must not raise

    assert pub.status()["state"] == "failed"
    assert "git exploded" in pub.status()["message"]


def test_a_finished_result_clears_itself_after_a_while():
    spy = PublishSpy()
    pub = make(spy)
    pub.request("2026-07-29")
    fire(pub)
    assert pub.status()["state"] == "published"

    linger = FakeTimer.created[-1]
    assert linger.delay == autopublish.LINGER_SECONDS
    linger.function()

    assert pub.status()["state"] == "idle"


def test_clearing_does_not_stamp_over_a_newer_edit():
    spy = PublishSpy()
    pub = make(spy)
    pub.request("2026-07-29")
    fire(pub)
    linger = FakeTimer.created[-1]

    pub.request("2026-07-28")        # a new edit before the linger expires
    linger.function()

    assert pub.status()["state"] == "pending"


# --- disabled --------------------------------------------------------------


def test_publishing_disabled_arms_nothing_and_says_so():
    spy = PublishSpy()
    pub = make(spy, enabled=False)

    pub.request("2026-07-29")

    assert spy.calls == []
    assert FakeTimer.created == []
    assert pub.status()["state"] == "disabled"
    assert pub.enabled is False


def test_a_config_without_a_publish_section_is_treated_as_disabled():
    pub = autopublish.Publisher(
        type("C", (), {})(), publisher=PublishSpy(), timer_factory=FakeTimer,
    )
    pub.request("2026-07-29")
    assert pub.enabled is False
    assert FakeTimer.created == []


def test_an_old_linger_timer_cannot_clear_a_newer_result():
    """Two publishes both settle on "published", so a state-string comparison
    would let the first one's timer clear the second one's result. Observed
    against a real server before the generation counter existed."""
    spy = PublishSpy()
    pub = make(spy)

    pub.request("2026-07-29")
    fire(pub)
    stale_linger = FakeTimer.created[-1]
    assert pub.status()["state"] == "published"

    pub.request("2026-07-28")
    fire(pub)
    assert pub.status()["state"] == "published"

    stale_linger.function()          # the first publish's timer, arriving late

    assert pub.status()["state"] == "published"
    assert pub.status()["message"] == "published"


def test_an_old_linger_timer_cannot_retract_a_failure_warning():
    """The case that actually matters: silently dropping "publish failed" leaves
    the reader believing the live site is current."""
    pub = make(PublishSpy())
    pub.request("2026-07-29")
    fire(pub)
    stale_linger = FakeTimer.created[-1]

    pub._publish = PublishSpy(PublishResult(False, False, "git push failed"))
    pub.request("2026-07-28")
    fire(pub)
    assert pub.status()["state"] == "failed"

    stale_linger.function()

    assert pub.status()["state"] == "failed"
    assert "git push failed" in pub.status()["message"]


def test_the_current_linger_still_clears_its_own_result():
    spy = PublishSpy()
    pub = make(spy)
    pub.request("2026-07-29")
    fire(pub)

    FakeTimer.created[-1].function()
    assert pub.status()["state"] == "idle"
