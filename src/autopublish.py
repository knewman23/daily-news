"""Push the site after the archive is edited by hand, once the edits stop.

Skipping or restoring a topic changes what the published page should show, and
without this the hosted copy silently drifts from the local one until the next
11am run. But a push per click is the wrong shape: triaging a day is half a dozen
toggles in a row, and each one would be a near-identical commit and its own queued
Pages build, which GitHub serialises.

So requests coalesce. Every change restarts a quiet timer, and one publish covers
whatever arrived in the meantime.

Three rules, the same ones publish.py works to:

**A publish failure never fails the edit.** By the time this runs the digest is
already written; being unable to reach GitHub is worth reporting, not worth
pretending the local edit did not happen. Nothing here raises into a request.

**Only one publish runs at a time.** A change arriving mid-push re-arms the timer
rather than starting a second git process against the same repository.

**Nothing is lost.** A batch is only cleared once it has been handed to
publish(), and a change that arrives during a publish rearms for the next one.
"""

from __future__ import annotations

import logging
import threading
from datetime import date
from typing import Callable

from src import publish as publish_module

log = logging.getLogger(__name__)

# Long enough to swallow a run of toggles, short enough that the site is live
# within about a minute of stopping. The Pages build dominates either way.
QUIET_SECONDS = 15.0

# How long a finished result stays readable before the status goes back to idle,
# so a page polling every couple of seconds cannot miss it.
LINGER_SECONDS = 20.0


class Publisher:
    """Coalescing, background publisher. Safe to call from request threads."""

    def __init__(
        self,
        cfg,
        delay: float = QUIET_SECONDS,
        publisher: Callable[..., publish_module.PublishResult] | None = None,
        timer_factory: Callable[..., threading.Timer] = threading.Timer,
    ):
        self._cfg = cfg
        self._delay = delay
        self._publish = publisher or publish_module.publish
        self._timer_factory = timer_factory

        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._pending: set[str] = set()      # dates edited since the last publish
        self._changes = 0
        self._running = False
        self._state = "idle"
        self._message = ""
        # Bumped for every settled result, so a linger timer can tell whether the
        # result it was armed for is still the one being shown.
        self._generation = 0

    @property
    def enabled(self) -> bool:
        return bool(getattr(getattr(self._cfg, "publish", None), "enabled", False))

    def request(self, day: date | str) -> None:
        """Note an edit. Publishes once nothing further arrives for `delay`."""
        if not self.enabled:
            return

        stamp = day.isoformat() if isinstance(day, date) else str(day)
        with self._lock:
            self._pending.add(stamp)
            self._changes += 1
            self._state = "pending"
            self._message = ""
            self._arm()

    def status(self) -> dict:
        """What to tell the page. A silent background push that failed would
        leave the reader believing the live site matches when it does not."""
        if not self.enabled:
            return {"state": "disabled", "message": "publishing is disabled",
                    "pending": 0}
        with self._lock:
            return {"state": self._state, "message": self._message,
                    "pending": self._changes}

    def flush(self, timeout: float | None = None) -> None:
        """Publish any pending batch now instead of waiting out the timer.

        For tests, and for shutdown — a queued edit should not be dropped just
        because the server is stopping.
        """
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if not self._pending and not self._running:
                return
        self._fire()

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    # --- internals ---------------------------------------------------------

    def _arm(self) -> None:
        """Restart the quiet timer. Caller holds the lock."""
        if self._timer is not None:
            self._timer.cancel()
        timer = self._timer_factory(self._delay, self._fire)
        timer.daemon = True          # a pending push must not block shutdown
        self._timer = timer
        timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
            if self._running:
                # Re-arm rather than run a second git against the same repo.
                self._arm()
                return
            if not self._pending:
                return

            days = sorted(self._pending)
            changes = self._changes
            self._pending = set()
            self._changes = 0
            self._running = True
            self._state = "publishing"
            self._message = ""

        state, message = self._run(days, changes)

        with self._lock:
            self._running = False
            if self._pending:
                # Edits arrived while this was in flight; they get the next one.
                self._state = "pending"
                self._message = ""
                self._arm()
            else:
                self._state = state
                self._message = message
                self._generation += 1
                self._linger(self._generation)

    def _run(self, days: list[str], changes: int) -> tuple[str, str]:
        """Do the publish. Returns the state and message to report."""
        try:
            result = self._publish(
                self._cfg,
                date.fromisoformat(days[-1]),
                summary=_summary(days, changes),
            )
        except Exception as exc:                        # pragma: no cover
            # publish() is documented never to raise; if that ever changes, an
            # exception in a timer thread would be invisible.
            log.exception("auto-publish raised: %s", exc)
            return "failed", str(exc)

        if not result.ok:
            log.error("auto-publish failed: %s", result.message)
            return "failed", result.message
        if not result.published:
            log.info("auto-publish: %s", result.message)
            return "idle", result.message

        log.info("auto-publish: %s", result.message)
        return "published", result.message

    def _linger(self, generation: int) -> None:
        """Clear a finished result after a while. Caller holds the lock.

        Keyed on the generation, not on the state string. Comparing states looks
        equivalent and is not: two publishes in a row both settle on "published",
        so the first one's timer would clear the second one's result early. The
        same bug on a "failed" result would quietly retract the only warning that
        the hosted copy is behind. Found by watching the status while publishing
        twice inside one linger window.
        """
        if self._state not in ("published", "failed"):
            return

        def clear():
            with self._lock:
                # Only clear the result this timer was armed for, and only if no
                # newer edit has already moved things on.
                if self._generation == generation and not self._pending:
                    self._state = "idle"
                    self._message = ""

        timer = self._timer_factory(LINGER_SECONDS, clear)
        timer.daemon = True
        timer.start()


def _summary(days: list[str], changes: int) -> str:
    """A commit subject that does not pretend to be a new day's news."""
    edits = f"{changes} change{'s' if changes != 1 else ''}"
    if len(days) == 1:
        return f"news: {days[0]} topic visibility ({edits})"
    return f"news: topic visibility on {len(days)} days ({edits})"
