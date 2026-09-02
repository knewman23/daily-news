"""Pulling recent video posts, one handle at a time.

Two things drive the design here.

**Per-handle watermarks.** Each source carries its own `last_pull_at`, and the
cutoff for a handle is that timestamp — so everything posted since its last
*successful* pull is collected and a missed run is made up on the next one. The
watermark advances only after that handle's fetch succeeds; a handle that got
rate-limited keeps its old watermark and retries the same window tomorrow while
the handles that succeeded move forward independently. Advancing a watermark
past a window that was never read loses those posts permanently, with no later
signal that they ever existed — which is why the advance is on the success path
and not in a `finally`.

**One failure never sinks the run.** A dead handle is skipped and the day is
flagged incomplete. Two failures do abort everything, because in both cases no
handle can succeed and continuing just burns requests: an expired session, and
an action block (`feedback_required`), where each further request also makes
the block worse rather than merely wasted.

The boundary is strictly greater-than the cutoff. With `>=`, the newest post of
the previous run — whose timestamp *equals* the stored watermark — would
reappear in the digest every single day.

**The feed is not strictly newest-first.** Instagram returns pinned posts ahead
of the chronological run, and a pinned post can be months old. Verified against
a live account: the first three results were 3347h, 5723h, and 6337h old, with
the actually-recent posts starting at position four. Stopping at the first
post older than the cutoff therefore stops immediately and silently collects
nothing, so the walk tolerates a run of old posts before deciding it has left
the recent window.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

import instaloader
from instaloader import exceptions as ig

from src import sources
from src.config import FetchConfig
from src.records import PostRef, Stats
from src.sources import Source

log = logging.getLogger(__name__)

# No credentials means no handle can succeed — abort rather than hammer the API.
FATAL = (
    ig.LoginRequiredException,
    ig.BadCredentialsException,
    ig.TwoFactorAuthRequiredException,
    ig.LoginException,
)

# The account is gone, renamed, or private. Retrying spends rate limit to learn
# the same thing again.
PERMANENT = (
    ig.ProfileNotExistsException,
    ig.PrivateProfileNotFollowedException,
    ig.QueryReturnedNotFoundException,
)


# Instagram allows up to 3 pinned posts and returns them before the
# chronological run, so this many consecutive old posts is not yet evidence that
# the recent window has ended.
PINNED_TOLERANCE = 3

# Hard backstop on how deep to walk a profile in one run. Reached only if a
# handle genuinely posted this many times inside its window; without it, a bug
# in the stop condition could crawl a whole 5000-post history and burn the
# rate limit for every other handle.
MAX_SCAN = 60


class SessionExpired(Exception):
    """The stored Instagram session is missing or no longer accepted."""


class ActionBlocked(Exception):
    """Instagram is refusing the access pattern, not the credentials."""


# Instagram answers a soft block with a 200-shaped 400: `"status": "fail"` and
# `"message": "feedback_required"`. instaloader wraps it in whichever exception
# the calling path happens to use -- AbortDownloadException live on 2026-09-02,
# QueryReturnedBadRequestException elsewhere -- and AbortDownloadException does
# not even descend from InstaloaderException, so matching on class misses cases.
# The message string is the only stable signal.
BLOCK_SIGNAL = "feedback_required"


def is_action_block(exc: BaseException) -> bool:
    return BLOCK_SIGNAL in str(exc)


class InstaloaderClient:
    """The real backend. Kept behind three methods so tests can substitute it."""

    def __init__(self, cfg: FetchConfig):
        self._loader = instaloader.Instaloader(
            save_metadata=False,              # no .json.xz sidecars
            download_comments=False,
            download_geotags=False,
            download_video_thumbnails=False,
            post_metadata_txt_pattern="",     # no caption .txt files
            quiet=True,
        )
        if not cfg.session_user:
            raise SessionExpired(
                "config.toml has no [fetch] session_user. Create a session with:\n"
                "  .venv/bin/instaloader --load-cookies chrome "
                "--sessionfile ~/.config/instaloader/session-<username>"
            )
        try:
            self._loader.load_session_from_file(cfg.session_user)
        except FileNotFoundError as exc:
            raise SessionExpired(
                f"no saved session for {cfg.session_user!r}. Re-authenticate with:\n"
                f"  .venv/bin/instaloader --load-cookies chrome "
                f"--sessionfile ~/.config/instaloader/session-{cfg.session_user}"
            ) from exc

    def posts_for(self, handle: str) -> Iterator[Any]:
        """Newest first. The profile lookup happens on the first item pulled."""
        profile = instaloader.Profile.from_username(self._loader.context, handle)
        yield from profile.get_posts()

    def download(self, post: Any, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._loader.context.get_and_write_raw(post.video_url, str(dest))

    def download_image(self, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._loader.context.get_and_write_raw(url, str(dest))

    def slide_urls(self, post: Any) -> list[str]:
        """Image URLs for a post: one for a single image, one per slide for a carousel.

        Video slides inside a carousel are skipped — only their thumbnail would be
        available here, and a thumbnail is not the post's content.
        """
        if getattr(post, "typename", "") != "GraphSidecar":
            return [post.url]

        return [
            node.display_url for node in post.get_sidecar_nodes()
            if not node.is_video
        ]


def make_loader(cfg: FetchConfig) -> Any:
    """Build the configured backend.

    `chrome` reads the same posts out of a real logged-in browser, which is the
    fallback when Instagram refuses this client's request pattern outright. See
    `src/fetch_chrome.py` for why that works when a fresh session does not.
    """
    if cfg.backend == "instaloader":
        return InstaloaderClient(cfg)

    if cfg.backend == "chrome":
        # Imported here, not at module scope: fetch_chrome imports this module.
        from src import fetch_chrome
        return fetch_chrome.ChromeClient(cfg)

    raise ValueError(
        f"unknown [fetch] backend {cfg.backend!r}; expected "
        f"'instaloader' or 'chrome'"
    )


def lookup_profile(loader: Any, handle: str) -> None:
    """Confirm a handle is reachable. Raises if it is not.

    This is the callable sources.add injects, so adding a handle from the web UI
    fails loudly on a typo instead of producing an account that silently never
    yields anything.
    """
    next(iter(loader.posts_for(handle)), None)


def start_of_day(day: date) -> datetime:
    """Local midnight for a date, as UTC-aware.

    Local, not UTC: the digest is dated by the local day, so "everything from
    today" has to mean the same day the file is named after. In a negative-offset
    timezone, UTC midnight is the previous afternoon.
    """
    naive = datetime(day.year, day.month, day.day)
    return naive.astimezone(timezone.utc)


def cutoff_for(
    source: Source,
    now: datetime,
    cfg: FetchConfig,
    override: datetime | None = None,
) -> datetime:
    """The oldest post timestamp this handle still owes us, as UTC-aware.

    Clamped to max_lookback_days so a long-stale watermark — a handle disabled
    for a month, say — cannot trigger a crawl back through a whole profile.

    An override replaces the watermark entirely, which is how a full pull re-scans
    a day the watermark has already moved past. It is still clamped, so `--full`
    on an ancient date cannot crawl a whole profile either.
    """
    floor = now - timedelta(days=cfg.max_lookback_days)

    if override is not None:
        return max(_utc(override), floor)

    if not source.last_pull_at:
        return max(now - timedelta(hours=cfg.first_run_lookback_hours), floor)

    return max(_utc(datetime.fromisoformat(source.last_pull_at)), floor)


def fetch_handle(
    loader: Any,
    source: Source,
    cutoff: datetime,
    dest: Path,
) -> list[PostRef]:
    """Download this handle's video posts newer than the cutoff.

    Old posts are skipped rather than treated as the end of the window, because
    pinned posts arrive first and can be arbitrarily old. Only a run of
    consecutive old posts longer than the pin limit means the chronological tail
    has been reached.
    """
    refs: list[PostRef] = []
    consecutive_old = 0

    for index, post in enumerate(loader.posts_for(source.handle)):
        if index >= MAX_SCAN:
            log.warning("%s: stopped after scanning %d posts", source.handle, MAX_SCAN)
            break

        posted = _utc(post.date_utc)
        if posted <= cutoff:
            consecutive_old += 1
            if consecutive_old > PINNED_TOLERANCE:
                break
            continue

        consecutive_old = 0

        ref = PostRef(
            handle=source.handle,
            shortcode=post.shortcode,
            posted_at=posted.isoformat(),
            permalink=f"https://www.instagram.com/p/{post.shortcode}/",
            caption=post.caption or "",
        )
        stem = f"{source.handle}_{post.shortcode}"

        if post.is_video:
            mp4 = dest / f"{stem}.mp4"
            if not mp4.exists():
                loader.download(post, mp4)
            kind = "video"
        elif _download_slides(loader, post, dest, stem):
            kind = "image"
        else:
            continue        # nothing usable on this post

        _write_sidecar(dest / f"{stem}.json", ref, kind)
        refs.append(ref)

    return refs


def fetch_day(
    sources_path: str | Path,
    dest: str | Path,
    cfg: FetchConfig,
    loader: Any = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] = None,
    since: datetime | None = None,
) -> tuple[list[PostRef], Stats]:
    """Fetch every enabled handle. Returns the day's posts and a Stats record.

    `since` overrides every handle's watermark, for a full re-pull of a day the
    watermarks have already passed. Posts already on disk are not downloaded
    again, so the cost is one profile walk per handle.
    """
    import time

    moment = now or datetime.now(timezone.utc)
    pause = sleep or time.sleep
    destination = Path(dest)
    stats = Stats()
    posts: list[PostRef] = []

    # Snapshot once: a handle added mid-run takes effect tomorrow rather than
    # half-way through today, so the day's digest matches one coherent list.
    enabled = sources.enabled_sources(sources_path)
    if not enabled:
        log.info("no enabled sources, nothing to fetch")
        return posts, stats

    client = loader or make_loader(cfg)

    for source in enabled:
        cutoff = cutoff_for(source, moment, cfg, override=since)
        try:
            found = _with_retries(client, source, cutoff, destination, cfg, pause)
        except (SessionExpired, ActionBlocked):
            raise
        except Exception as exc:
            log.warning("giving up on %s: %s", source.handle, exc)
            stats.fail(f"fetch {source.handle}: {exc}")
            continue

        posts.extend(found)
        stats.post_count += len(found)

        # Success: this handle owes us nothing before `moment` any more.
        sources.advance_watermark(sources_path, source.handle, moment)
        if found:
            sources.stamp_last_seen(sources_path, source.handle, moment.date())

    return posts, stats


# --- internals -------------------------------------------------------------


def _with_retries(
    client: Any,
    source: Source,
    cutoff: datetime,
    dest: Path,
    cfg: FetchConfig,
    pause: Callable[[float], None],
) -> list[PostRef]:
    """Retry transient failures with exponential backoff; re-raise the rest."""
    last: Exception | None = None

    for attempt in range(1, max(cfg.max_retries, 1) + 1):
        try:
            return fetch_handle(client, source, cutoff, dest)
        except FATAL as exc:
            raise SessionExpired(
                f"Instagram session rejected ({exc}). Re-authenticate with:\n"
                f"  .venv/bin/instaloader --load-cookies chrome "
                f"--sessionfile ~/.config/instaloader/session-{cfg.session_user}"
            ) from exc
        except SessionExpired:
            raise                       # nothing can work; do not spend retries
        except PERMANENT as exc:
            raise                       # not worth a second request
        except Exception as exc:
            # Retrying a soft block is read as confirmation that a bot is
            # driving the session, so this deepens the block it is trying to
            # ride out. Abort the whole run: the block is on the account, so
            # no other handle can succeed either.
            if is_action_block(exc):
                raise ActionBlocked(
                    f"Instagram refused the request pattern on {source.handle} "
                    f"({exc}). The session is valid; the automated access is "
                    f"what is blocked, so re-authenticating will not clear it. "
                    f"Leave it alone and let the next scheduled run retry."
                ) from exc
            last = exc
            if attempt < cfg.max_retries:
                delay = cfg.backoff_seconds * (2 ** (attempt - 1))
                log.warning("%s failed (%s), retrying in %ss", source.handle, exc, delay)
                pause(delay)

    raise last if last else RuntimeError(f"{source.handle}: no attempt was made")


def _download_slides(loader: Any, post: Any, dest: Path, stem: str) -> bool:
    """Save a post's images. Returns False when there was nothing to save.

    A single image lands at `<stem>.jpg`; a carousel becomes `<stem>_1.jpg`,
    `<stem>_2.jpg`, … in slide order, which is the order ocr.py concatenates them.
    """
    urls = loader.slide_urls(post)
    if not urls:
        return False

    single = len(urls) == 1
    for index, url in enumerate(urls, start=1):
        target = dest / (f"{stem}.jpg" if single else f"{stem}_{index}.jpg")
        if not target.exists():
            loader.download_image(url, target)
    return True


def _write_sidecar(path: Path, ref: PostRef, kind: str) -> None:
    """The durable record of a post.

    Attribution for transcribe.py, which must not parse it out of the filename.
    `kind` is what lets the extract stages find their posts from the sidecars
    rather than by globbing media, so the media can be deleted once transcribed
    without a re-run seeing an empty day.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "handle": ref.handle,
        "shortcode": ref.shortcode,
        "kind": kind,
        "posted_at": ref.posted_at,
        "permalink": ref.permalink,
        "caption": ref.caption,
    }, indent=2) + "\n", encoding="utf-8")


def _utc(moment: datetime) -> datetime:
    """instaloader hands back naive UTC; everything downstream compares aware."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)
