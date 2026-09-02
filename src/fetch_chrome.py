"""Reading a profile's recent posts out of a real logged-in browser.

**Why this backend exists.** On 2026-09-02 every handle came back
`400 Bad Request - "fail" status, message "feedback_required"` from
`api/v1/users/web_profile_info/`. A new session did not help. What settled the
diagnosis was running both clients against the same account minutes apart:

    instaloader  api/v1/users/web_profile_info/  ->  400 feedback_required
    Chrome       graphql/query                   ->  200, 12 posts

Same `sessionid`, same IP. The credentials were never the problem; the request
pattern was. instaloader hits a bare API endpoint with no browser fingerprint,
no `Referer`, and none of the supporting requests a page load makes. This module
lets the browser make its own query and reads the answer off the wire.

**Interception, not replay.** The obvious shortcut is to POST `graphql/query`
ourselves with the `doc_id` observed live. Do not. That `doc_id` is pinned to a
release of the web app and rotates, and replaying it by hand recreates exactly
the fingerprint-less client that got blocked in the first place. The page issues
its own query; we watch for the response.

**One page load per handle, usually.** The web app asks for 12 posts at a time,
and a handle normally has 0-3 posts newer than its watermark, so paging is rare.
`posts_for` is a generator for that reason: `fetch.fetch_handle` abandons it
once it has seen enough consecutive old posts, and a page that is never
iterated is never requested. That makes this backend cheaper in requests than
the profile walk it replaces, not more expensive.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import urlparse

from src import fetch
from src.config import FetchConfig

log = logging.getLogger(__name__)

# The friendly names of the query the profile page issues for its post grid,
# and the root field of the payload that comes back. Read off live captures.
#
# There is more than one name. On 2026-09-02, in one browser on one session,
# `carolinegleich` issued PolarisProfilePostsQuery while `aaronparnas` issued
# PolarisProfilePostsTabContentQuery_connection -- the profile route has
# variants, and which one a handle gets is not ours to decide. Matching a
# single name silently waited out the full timeout on the other variant.
POSTS_QUERIES = (
    "PolarisProfilePostsQuery",
    "PolarisProfilePostsTabContentQuery_connection",
)
POSTS_QUERY = POSTS_QUERIES[0]          # for messages that name just one
POSTS_ROOT_FIELD = "xdt_api__v1__feed__user_timeline_graphql_connection"


def is_posts_query(friendly_name: str | None) -> bool:
    """Whether a graphql call's friendly name is the profile post grid.

    Several unrelated queries share `/graphql/query` during one page load, so
    this has to be an exact match against the known variants rather than a
    substring test -- `PolarisProfilePageContentQuery` is not the posts query.
    """
    return friendly_name in POSTS_QUERIES

# `media_type` in the web payload: 1 image, 2 video, 8 carousel. Verified
# against a live response containing one of each.
VIDEO = 2
CAROUSEL = 8


class ChromeUnavailable(fetch.SessionExpired):
    """No usable browser, or one that is not logged in.

    Deliberately a SessionExpired: `fetch.fetch_day` re-raises that and aborts,
    which is the right response when no handle can possibly succeed. Anything
    else is recorded as a single dead handle and the run limps on through six
    more pointless attempts.
    """


class PostsQueryTimeout(Exception):
    """The page did not issue the posts query in time.

    Deliberately *not* a ChromeUnavailable. A cold profile load competes with
    the feed, stories and badge queries and can genuinely be slow, so this is
    transient and worth the ordinary retry. Conflating it with an unusable
    browser is what made a slow first page look like a broken setup.
    """


@dataclass(frozen=True)
class Slide:
    """One carousel slide, with the two attributes `fetch.slide_urls` reads."""

    is_video: bool
    display_url: str


@dataclass(frozen=True)
class Post:
    """A post, wearing the attribute names `fetch.fetch_handle` expects.

    The names mirror instaloader's Post rather than the web payload's, so the
    walk, the watermark logic, and `PostRef` construction stay untouched. That
    is the whole point: this class absorbs the difference between the two
    sources so nothing downstream has to know which backend ran.
    """

    shortcode: str
    date_utc: datetime
    is_video: bool
    caption: str
    video_url: str
    url: str
    typename: str
    slides: Sequence[Slide] = field(default_factory=tuple)

    def get_sidecar_nodes(self) -> Sequence[Slide]:
        return self.slides


class ChromeClient:
    """The Chrome backend, with the same four methods as `InstaloaderClient`.

    The browser arrives as a parameter so the mapping can be tested against
    recorded payloads with no browser and no network, per `src/CLAUDE.md`.
    """

    def __init__(self, cfg: FetchConfig, browser: Any = None):
        self._browser = browser if browser is not None else CdpBrowser(cfg)

    def posts_for(self, handle: str) -> Iterator[Post]:
        """Newest first, paging only as far as the caller actually walks."""
        cursor: str | None = None

        while True:
            payload = self._browser.profile_posts(handle, cursor)
            connection = _connection(payload, handle)

            for edge in connection.get("edges") or []:
                node = edge.get("node")
                if node:
                    yield _post(node)

            info = connection.get("page_info") or {}
            cursor = info.get("end_cursor")
            # A promised next page with no cursor to ask for would loop forever.
            if not info.get("has_next_page") or not cursor:
                return

    def download(self, post: Any, dest: Path) -> None:
        _write(dest, self._browser.get_bytes(post.video_url))

    def download_image(self, url: str, dest: Path) -> None:
        _write(dest, self._browser.get_bytes(url))

    def slide_urls(self, post: Any) -> list[str]:
        """Image URLs for a post. Mirrors `InstaloaderClient.slide_urls`.

        Video slides inside a carousel are skipped for the same reason as there:
        only a thumbnail is available, and a thumbnail is not the post's
        content. ocr.py concatenates these in slide order.
        """
        if getattr(post, "typename", "") != "GraphSidecar":
            return [post.url]

        return [
            slide.display_url for slide in post.get_sidecar_nodes()
            if not slide.is_video
        ]


def probe_endpoint(url: str) -> dict | None:
    """What is answering at a CDP endpoint, or None if nothing is.

    `/json/version` identifies the browser, which matters because the port
    number alone does not: another browser can hold the same port on the other
    address family.
    """
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/json/version",
                                    timeout=3) as response:
            payload = json.load(response)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def ensure_endpoint(cfg: FetchConfig, probe=None, launcher=None,
                    sleep=None) -> None:
    """Make sure a Chrome CDP endpoint is answering at `cfg.cdp_url`.

    Starts one if nothing is. The scheduled run is unattended, so requiring a
    browser someone launched by hand makes a reboot into a silently missed
    digest -- which was the most likely cause of future failure once the
    chrome backend became the default.

    Headful on purpose. A headless browser would need stealth patches to pass
    for real, which is the arms race this backend exists to avoid.
    """
    probe = probe or probe_endpoint
    launcher = launcher or subprocess.Popen
    pause = sleep or time.sleep

    answering = probe(cfg.cdp_url)
    if answering is not None:
        _refuse_a_foreign_browser(answering, cfg)
        return

    port = urlparse(cfg.cdp_url).port or 9222
    profile = Path(cfg.chrome_profile_dir).expanduser()
    argv = [
        cfg.chrome_binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        # Playwright cannot attach to a browser with no targets at all, and a
        # freshly launched Chrome with no URL may have none.
        "about:blank",
    ]

    log.info("no browser at %s, starting one", cfg.cdp_url)
    try:
        launcher(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        raise ChromeUnavailable(
            f"cannot start a browser: {cfg.chrome_binary} is not there. Set "
            f"[fetch] chrome_binary to where Google Chrome actually is."
        ) from exc

    deadline = max(cfg.chrome_launch_timeout_seconds, 1)
    for _ in range(deadline):
        pause(1)
        answering = probe(cfg.cdp_url)
        if answering is not None:
            _refuse_a_foreign_browser(answering, cfg)
            log.info("browser is up at %s", cfg.cdp_url)
            return

    raise ChromeUnavailable(
        f"started {cfg.chrome_binary} but the endpoint at {cfg.cdp_url} did "
        f"not come up within {deadline}s"
    )


def _refuse_a_foreign_browser(answering: dict, cfg: FetchConfig) -> None:
    """Refuse a port held by something that is not Chrome.

    Verified live on 2026-09-02: Brave held 127.0.0.1:9222 while Chrome was on
    [::1]:9222, and the same port number reached a different browser depending
    on the address family. Launching another Chrome cannot bind an occupied
    port, so naming the squatter is the only useful move.
    """
    name = str(answering.get("Browser") or "unknown")
    if "chrome" in name.lower() or "chromium" in name.lower():
        return

    raise ChromeUnavailable(
        f"{cfg.cdp_url} is held by {name}, not Chrome. Another browser is "
        f"using that debugging port -- quit it, or point [fetch] cdp_url at a "
        f"different port."
    )


class CdpBrowser:
    """The real backend: an already-running Chrome, driven over CDP.

    Attaching to a browser the user launched -- rather than launching a headless
    one -- is the entire mechanism. A headless browser would need stealth
    patches to pass for real; a real browser needs none, because it is real.
    """

    def __init__(self, cfg: FetchConfig):
        # Imported here, not at module scope: playwright is only needed when
        # this backend actually runs, and the instaloader path and the whole
        # test suite must not require it to be installed.
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ChromeUnavailable(
                "the chrome backend needs playwright:\n"
                "  .venv/bin/pip install playwright"
            ) from exc

        self._cfg = cfg
        self._timeout_ms = max(cfg.page_timeout_seconds, 1) * 1000
        ensure_endpoint(cfg)
        self._play = sync_playwright().start()
        try:
            self._browser = self._play.chromium.connect_over_cdp(cfg.cdp_url)
        except Exception as exc:
            self._play.stop()
            raise ChromeUnavailable(
                f"could not drive the browser at {cfg.cdp_url}: {exc}\n"
                "If nothing is listening, start one with:\n"
                "  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \\\n"
                "    --remote-debugging-port=9222 \\\n"
                "    --user-data-dir=$HOME/.config/daily-news-chrome\n"
                "then log into Instagram in it once.\n"
                "If it connected and then failed on 'Browser context management "
                "is not supported', the browser has no tab open -- open one."
            ) from exc

        contexts = self._browser.contexts
        self._context = contexts[0] if contexts else self._browser.new_context()
        self._page = self._context.new_page()

    def profile_posts(self, handle: str, cursor: str | None = None) -> dict:
        """Load (or scroll) the profile and return the posts payload it fetched.

        The first call navigates. A cursor means the caller wants the next page,
        which the web app only requests when the grid is scrolled -- so scroll,
        rather than trying to issue the query ourselves.
        """
        url = f"https://www.instagram.com/{handle}/"
        log.debug("%s: %s", handle,
                  "loading profile" if cursor is None else "scrolling for more")

        seen: list[str] = []

        def note(request: Any) -> None:
            if "graphql" not in request.url:
                return
            try:
                name = request.headers.get("x-fb-friendly-name")
            except Exception:       # a diagnostic must never break the fetch
                return
            if name:
                seen.append(name)

        self._page.on("request", note)
        try:
            with self._page.expect_response(_is_posts_response,
                                            timeout=self._timeout_ms) as caught:
                if cursor is None:
                    self._page.goto(url, timeout=self._timeout_ms,
                                    wait_until="domcontentloaded")
                else:
                    self._page.mouse.wheel(0, 20000)
        except Exception as exc:
            if type(exc).__name__ != "TimeoutError":
                raise
            # Verified live on 2026-09-02: logged out, the profile page routes
            # to PolarisLoggedOut* and never issues PolarisProfilePostsQuery,
            # so the wait simply expires. A bare playwright timeout here reads
            # as a network fault and sends you looking in the wrong place.
            # Which of the two it is, is a question the browser can answer --
            # so ask, rather than assert a cause. Guessing "logged out" here
            # sent a real diagnosis down the wrong path once already.
            if not self.logged_in():
                raise ChromeUnavailable(
                    f"{handle}: the browser at {self._cfg.cdp_url} is not "
                    f"signed into Instagram (no sessionid cookie), so "
                    f"{POSTS_QUERY} is never issued. Sign in once in that "
                    f"browser window."
                ) from exc

            raise PostsQueryTimeout(
                f"{handle}: signed in, but the page issued none of "
                f"{'/'.join(POSTS_QUERIES)} within "
                f"{self._cfg.page_timeout_seconds}s. {_timeout_detail(seen)}"
            ) from exc
        finally:
            self._page.remove_listener("request", note)

        return caught.value.json()

    def logged_in(self) -> bool:
        """Whether this browser holds an Instagram session cookie."""
        try:
            names = {c["name"] for c in
                     self._context.cookies("https://www.instagram.com")}
        except Exception:
            return False
        return "sessionid" in names and "ds_user_id" in names

    def get_bytes(self, url: str) -> bytes:
        """Fetch media through the page, so the request carries its context.

        The signed `cdninstagram.com` URLs are short-lived, which is why the
        download happens during the run rather than being deferred.
        """
        response = self._context.request.get(url, timeout=self._timeout_ms)
        if not response.ok:
            raise RuntimeError(f"{url} -> HTTP {response.status}")
        return response.body()

    def close(self) -> None:
        """Disconnect, leaving the browser itself running.

        Two deliberate choices. `browser.close()` on a CDP *attachment*
        disconnects rather than quitting the browser, so a browser we started
        stays up and the next run reuses it instead of paying the launch again.

        And our own tab is closed only if another remains: leaving the browser
        with no targets at all is what makes the next `connect_over_cdp` fail
        with "Browser context management is not supported", which reads as a
        broken setup rather than an empty window.
        """
        try:
            if len(self._context.pages) > 1:
                self._page.close()
        except Exception:                # teardown must not mask a real failure
            pass

        for shut in (self._browser.close, self._play.stop):
            try:
                shut()
            except Exception:
                pass


# --- internals -------------------------------------------------------------


def _is_posts_response(response: Any) -> bool:
    if "/graphql/query" not in response.url:
        return False
    try:
        return is_posts_query(response.request.headers.get("x-fb-friendly-name"))
    except Exception:
        # A predicate that raises is a predicate that never matches, which
        # would present as an inexplicable timeout.
        return False


def _timeout_detail(seen: Sequence[str]) -> str:
    """What the page asked for instead, so the next step is not another guess."""
    if not seen:
        return ("It issued no graphql queries at all, which points at the "
                "navigation rather than the route.")

    return "It did issue: " + ", ".join(sorted(set(seen))) + "."


def _connection(payload: Any, handle: str) -> dict:
    """Pull the posts connection out of a payload, or explain what came back."""
    if not isinstance(payload, dict):
        raise ChromeUnavailable(f"{handle}: unreadable response from the browser")

    _refuse_if_blocked(payload, handle)

    connection = (payload.get("data") or {}).get(POSTS_ROOT_FIELD)
    if not isinstance(connection, dict):
        # Verified live: logged out, Instagram serves the login interstitial and
        # this field is simply absent. Being logged out is the overwhelmingly
        # likely cause, and it is not a per-handle problem.
        raise ChromeUnavailable(
            f"{handle}: the browser returned no posts. The likeliest cause by "
            f"far is that it is not logged into Instagram -- open "
            f"https://www.instagram.com/{handle}/ in it and check."
        )

    return connection


def _refuse_if_blocked(payload: dict, handle: str) -> None:
    """An action block reaching us through the browser is still an action block.

    Same rule as the instaloader path: never retried, because a retry is read
    as confirmation that a bot is driving the session.
    """
    messages = [str(payload.get("message") or "")]
    for error in payload.get("errors") or []:
        if isinstance(error, dict):
            messages.append(str(error.get("message") or ""))

    for message in messages:
        if fetch.is_action_block(message):
            raise fetch.ActionBlocked(
                f"Instagram refused the request pattern on {handle} through the "
                f"browser too ({message}). Leave it alone and let the next "
                f"scheduled run retry."
            )


def _post(node: dict) -> Post:
    media_type = node.get("media_type")
    is_carousel = media_type == CAROUSEL
    is_video = media_type == VIDEO

    return Post(
        shortcode=node.get("code") or "",
        date_utc=datetime.fromtimestamp(node.get("taken_at") or 0, timezone.utc),
        is_video=is_video,
        caption=((node.get("caption") or {}).get("text") or ""),
        video_url=_best(node.get("video_versions")),
        url=_best((node.get("image_versions2") or {}).get("candidates")),
        typename=("GraphSidecar" if is_carousel
                  else "GraphVideo" if is_video
                  else "GraphImage"),
        slides=tuple(_slide(s) for s in node.get("carousel_media") or ()),
    )


def _slide(node: dict) -> Slide:
    return Slide(
        is_video=node.get("media_type") == VIDEO,
        display_url=_best((node.get("image_versions2") or {}).get("candidates")),
    )


def _best(candidates: Any) -> str:
    """The largest rendition, falling back to the first.

    Instagram orders these widest-first, but resolution is load-bearing here --
    ocr.py reads text off these images, and a thumbnail silently produces worse
    text rather than an error. So prefer an explicit width when there is one.
    """
    if not candidates:
        return ""

    with_width = [c for c in candidates if isinstance(c.get("width"), int)]
    if with_width:
        return max(with_width, key=lambda c: c["width"]).get("url") or ""

    return candidates[0].get("url") or ""


def _write(dest: Path, blob: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(blob)
