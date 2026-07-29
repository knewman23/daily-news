import json
from datetime import date, datetime, timedelta, timezone

import pytest

from src import fetch, sources
from src.config import FetchConfig
from src.records import Stats


NOW = datetime(2026, 7, 28, 11, 0, tzinfo=timezone.utc)
CFG = FetchConfig(
    session_user="krys.newman",
    first_run_lookback_hours=48,
    max_lookback_days=14,
    max_retries=3,
    backoff_seconds=30,
)

SEED = {
    "version": 2,
    "sources": [
        {"handle": "aaronparnas", "enabled": True, "added": "2026-07-28",
         "last_pull_at": None, "last_seen": None},
        {"handle": "carolinegleich", "enabled": True, "added": "2026-07-28",
         "last_pull_at": None, "last_seen": None},
        {"handle": "oafnation_actual", "enabled": False, "added": "2026-07-28",
         "last_pull_at": None, "last_seen": None},
    ],
}


class FakeNode:
    def __init__(self, index, is_video=False):
        self.is_video = is_video
        self.display_url = f"https://cdn.test/slide{index}.jpg"


class FakePost:
    def __init__(self, shortcode, hours_ago, is_video=True, caption="a caption",
                 typename=None, slides=None):
        self.shortcode = shortcode
        self.date_utc = (NOW - timedelta(hours=hours_ago)).replace(tzinfo=None)
        self.is_video = is_video
        self.caption = caption
        self.video_url = f"https://cdn.test/{shortcode}.mp4"
        self.url = f"https://cdn.test/{shortcode}.jpg"
        self.typename = typename or ("GraphVideo" if is_video else "GraphImage")
        self._slides = slides or []

    def get_sidecar_nodes(self):
        return self._slides


class FakeLoader:
    """Stands in for instaloader.Instaloader plus Profile.from_username."""

    def __init__(self, posts=None, error=None, error_times=0):
        self.posts = posts or {}
        self.error = error
        self.error_times = error_times
        self.attempts = []
        self.downloaded = []
        self.walked = {}

    def posts_for(self, handle):
        self.attempts.append(handle)
        if self.error and len(self.attempts) <= self.error_times:
            raise self.error
        walked = []
        self.walked[handle] = walked
        for post in self.posts.get(handle, []):
            walked.append(post.shortcode)
            yield post

    def download(self, post, dest):
        self.downloaded.append((post.shortcode, dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake-mp4")

    def download_image(self, url, dest):
        self.downloaded.append((url, dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake-jpg")

    def slide_urls(self, post):
        if post.typename != "GraphSidecar":
            return [post.url]
        return [n.display_url for n in post.get_sidecar_nodes() if not n.is_video]


@pytest.fixture
def sources_path(tmp_path):
    p = tmp_path / "sources.json"
    p.write_text(json.dumps(SEED, indent=2) + "\n", encoding="utf-8")
    return p


@pytest.fixture
def dest(tmp_path):
    return tmp_path / "raw" / "2026-07-28"


def no_sleep(_seconds):
    pass


def run(sources_path, dest, loader, cfg=CFG, now=NOW):
    return fetch.fetch_day(
        sources_path, dest, cfg, loader=loader, now=now, sleep=no_sleep,
    )


def watermarks(path):
    return {s.handle: s.last_pull_at for s in sources.load(path)}


# --- cutoff_for ------------------------------------------------------------


def test_cutoff_is_the_watermark_when_one_exists():
    source = sources.Source(handle="a", last_pull_at="2026-07-27T11:00:00+00:00")
    assert fetch.cutoff_for(source, NOW, CFG) == datetime(
        2026, 7, 27, 11, 0, tzinfo=timezone.utc
    )


def test_cutoff_falls_back_to_the_first_run_window():
    source = sources.Source(handle="a", last_pull_at=None)
    assert fetch.cutoff_for(source, NOW, CFG) == NOW - timedelta(hours=48)


def test_a_stale_watermark_is_clamped_to_max_lookback():
    source = sources.Source(handle="a", last_pull_at="2025-01-01T00:00:00+00:00")
    assert fetch.cutoff_for(source, NOW, CFG) == NOW - timedelta(days=14)


def test_cutoff_is_always_utc_aware():
    for stamp in (None, "2026-07-27T11:00:00+00:00", "2026-07-27T04:00:00-07:00"):
        cutoff = fetch.cutoff_for(sources.Source(handle="a", last_pull_at=stamp), NOW, CFG)
        assert cutoff.tzinfo is not None
        assert cutoff.utcoffset() == timedelta(0)


# --- selecting posts -------------------------------------------------------


def test_videos_download_as_mp4_and_images_as_jpg(sources_path, dest):
    loader = FakeLoader({"aaronparnas": [
        FakePost("VID", hours_ago=2),
        FakePost("IMG", hours_ago=3, is_video=False),
    ]})

    posts, stats = run(sources_path, dest, loader)

    assert [p.shortcode for p in posts] == ["VID", "IMG"]
    assert (dest / "aaronparnas_VID.mp4").exists()
    assert (dest / "aaronparnas_IMG.jpg").exists()
    assert stats.post_count == 2


def test_a_carousel_saves_one_file_per_image_slide_in_order(sources_path, dest):
    loader = FakeLoader({"aaronparnas": [
        FakePost("CAR", hours_ago=2, is_video=False, typename="GraphSidecar",
                 slides=[FakeNode(1), FakeNode(2), FakeNode(3, is_video=True)]),
    ]})

    posts, _ = run(sources_path, dest, loader)

    assert [p.shortcode for p in posts] == ["CAR"]
    assert (dest / "aaronparnas_CAR_1.jpg").exists()
    assert (dest / "aaronparnas_CAR_2.jpg").exists()
    # The video slide is skipped: only its thumbnail is reachable, and a
    # thumbnail is not the post's content.
    assert not (dest / "aaronparnas_CAR_3.jpg").exists()


def test_a_carousel_of_only_video_slides_is_skipped(sources_path, dest):
    loader = FakeLoader({"aaronparnas": [
        FakePost("VONLY", hours_ago=2, is_video=False, typename="GraphSidecar",
                 slides=[FakeNode(1, is_video=True)]),
    ]})

    posts, stats = run(sources_path, dest, loader)

    assert posts == []
    assert stats.post_count == 0
    assert not (dest / "aaronparnas_VONLY.json").exists()


def test_an_already_downloaded_image_is_not_fetched_again(sources_path, dest):
    dest.mkdir(parents=True)
    (dest / "aaronparnas_IMG.jpg").write_bytes(b"already here")
    loader = FakeLoader({"aaronparnas": [FakePost("IMG", hours_ago=2, is_video=False)]})

    posts, _ = run(sources_path, dest, loader)

    assert loader.downloaded == []
    assert [p.shortcode for p in posts] == ["IMG"]


def test_pinned_old_posts_at_the_head_do_not_hide_recent_ones(sources_path, dest):
    """Instagram returns pinned posts first, and a pinned post can be months old.

    Verified against a live account: the first three results were 3347h, 5723h,
    and 6337h old, with the actually-recent posts starting at position four.
    Treating the first old post as the end of the window collects nothing at all,
    every day, with no error.
    """
    loader = FakeLoader({"aaronparnas": [
        FakePost("PIN1", hours_ago=3347),
        FakePost("PIN2", hours_ago=5723),
        FakePost("PIN3", hours_ago=6337),
        FakePost("RECENT1", hours_ago=1),
        FakePost("RECENT2", hours_ago=3),
        FakePost("TAIL", hours_ago=100),
    ]})

    posts, _ = run(sources_path, dest, loader)

    assert [p.shortcode for p in posts] == ["RECENT1", "RECENT2"]


def test_the_walk_stops_once_past_the_pin_tolerance(sources_path, dest):
    """It must still stop — a 5000-post history would exhaust the rate limit."""
    loader = FakeLoader({"aaronparnas": [
        FakePost("NEW", hours_ago=2),
        *[FakePost(f"OLD{i}", hours_ago=100 + i) for i in range(10)],
    ]})

    posts, _ = run(sources_path, dest, loader)

    assert [p.shortcode for p in posts] == ["NEW"]
    # One recent post, then PINNED_TOLERANCE + 1 old ones before breaking out.
    assert loader.walked["aaronparnas"] == ["NEW", "OLD0", "OLD1", "OLD2", "OLD3"]


def test_the_walk_is_bounded_by_max_scan(sources_path, dest):
    loader = FakeLoader({"aaronparnas": [
        FakePost(f"P{i}", hours_ago=1) for i in range(fetch.MAX_SCAN + 20)
    ]})

    posts, _ = run(sources_path, dest, loader)

    assert len(posts) == fetch.MAX_SCAN
    assert len(loader.walked["aaronparnas"]) == fetch.MAX_SCAN + 1


def test_a_post_exactly_at_the_cutoff_is_excluded(sources_path, dest):
    """The boundary is strictly greater-than.

    Otherwise the newest post of the previous run reappears in today's digest
    every single day, because its timestamp equals the stored watermark.
    """
    sources.advance_watermark(sources_path, "aaronparnas", NOW - timedelta(hours=24))
    loader = FakeLoader({"aaronparnas": [FakePost("EDGE", hours_ago=24)]})

    posts, _ = run(sources_path, dest, loader)
    assert posts == []


def test_two_consecutive_runs_with_no_new_posts_download_nothing_the_second_time(
    sources_path, dest
):
    loader = FakeLoader({"aaronparnas": [FakePost("AAA", hours_ago=2)]})
    run(sources_path, dest, loader)
    assert len(loader.downloaded) == 1

    later = NOW + timedelta(days=1)
    second = FakeLoader({"aaronparnas": [FakePost("AAA", hours_ago=2)]})
    posts, _ = fetch.fetch_day(
        sources_path, dest, CFG, loader=second, now=later, sleep=no_sleep,
    )

    assert posts == []
    assert second.downloaded == []


def test_an_already_downloaded_post_is_not_fetched_again(sources_path, dest):
    dest.mkdir(parents=True)
    (dest / "aaronparnas_AAA.mp4").write_bytes(b"already here")
    loader = FakeLoader({"aaronparnas": [FakePost("AAA", hours_ago=2)]})

    posts, stats = run(sources_path, dest, loader)

    assert loader.downloaded == []
    assert [p.shortcode for p in posts] == ["AAA"]
    assert stats.post_count == 1


def test_disabled_handles_are_never_requested(sources_path, dest):
    loader = FakeLoader({"oafnation_actual": [FakePost("NOPE", hours_ago=1)]})
    run(sources_path, dest, loader)
    assert "oafnation_actual" not in loader.attempts


def test_a_caption_sidecar_is_written_beside_each_mp4(sources_path, dest):
    loader = FakeLoader({"aaronparnas": [FakePost("AAA", hours_ago=2, caption="Breaking")]})

    run(sources_path, dest, loader)

    sidecar = json.loads((dest / "aaronparnas_AAA.json").read_text(encoding="utf-8"))
    assert sidecar["handle"] == "aaronparnas"
    assert sidecar["shortcode"] == "AAA"
    assert sidecar["caption"] == "Breaking"
    assert sidecar["permalink"] == "https://www.instagram.com/p/AAA/"
    assert sidecar["posted_at"].endswith("+00:00")


# --- watermarks ------------------------------------------------------------


def test_watermark_advances_on_success_even_with_zero_posts(sources_path, dest):
    loader = FakeLoader({"aaronparnas": [], "carolinegleich": []})

    run(sources_path, dest, loader)

    marks = watermarks(sources_path)
    assert marks["aaronparnas"] == NOW.isoformat()
    assert marks["carolinegleich"] == NOW.isoformat()
    assert marks["oafnation_actual"] is None


def test_last_seen_is_stamped_only_for_handles_that_yielded_posts(sources_path, dest):
    loader = FakeLoader({"aaronparnas": [FakePost("AAA", hours_ago=2)], "carolinegleich": []})

    run(sources_path, dest, loader)

    seen = {s.handle: s.last_seen for s in sources.load(sources_path)}
    assert seen["aaronparnas"] == "2026-07-28"
    assert seen["carolinegleich"] is None


def test_the_run_uses_a_snapshot_of_the_handle_list(sources_path, dest):
    """A handle added mid-run takes effect tomorrow, not half-way through today."""
    loader = FakeLoader({"aaronparnas": [], "carolinegleich": []})

    original = fetch.fetch_handle

    def add_midway(loader_, source, cutoff, dest_):
        if source.handle == "aaronparnas":
            sources.add(sources_path, "latecomer", lookup=lambda h: None, today="2026-07-28")
        return original(loader_, source, cutoff, dest_)

    fetch.fetch_handle = add_midway
    try:
        run(sources_path, dest, loader)
    finally:
        fetch.fetch_handle = original

    assert "latecomer" not in loader.attempts
    assert watermarks(sources_path)["latecomer"] is None


# --- failure paths ---------------------------------------------------------


def test_expired_session_raises_a_distinct_error(sources_path, dest):
    import instaloader

    loader = FakeLoader(error=instaloader.exceptions.LoginRequiredException("nope"),
                        error_times=99)

    with pytest.raises(fetch.SessionExpired):
        run(sources_path, dest, loader)


def test_rate_limit_is_retried_with_backoff_then_the_handle_is_abandoned(sources_path, dest):
    import instaloader

    loader = FakeLoader(error=instaloader.exceptions.TooManyRequestsException("429"),
                        error_times=99)
    slept = []

    posts, stats = fetch.fetch_day(
        sources_path, dest, CFG, loader=loader, now=NOW, sleep=slept.append,
    )

    # 3 attempts per handle, two enabled handles.
    assert len(loader.attempts) == 6
    assert len(slept) == 4          # two backoffs per handle, none after the last try
    assert slept == [30, 60, 30, 60]
    assert stats.incomplete is True
    assert posts == []


def test_a_failed_handle_keeps_its_old_watermark(sources_path, dest):
    """The whole point of per-handle watermarks.

    Advancing a watermark past a window that was never actually read loses those
    posts permanently and silently — there is no later signal that they existed.
    """
    import instaloader

    sources.advance_watermark(sources_path, "aaronparnas", NOW - timedelta(days=2))
    before = watermarks(sources_path)["aaronparnas"]

    loader = FakeLoader(error=instaloader.exceptions.TooManyRequestsException("429"),
                        error_times=99)
    fetch.fetch_day(sources_path, dest, CFG, loader=loader, now=NOW, sleep=no_sleep)

    assert watermarks(sources_path)["aaronparnas"] == before


def test_a_retry_that_succeeds_advances_the_watermark(sources_path, dest):
    import instaloader

    loader = FakeLoader(
        {"aaronparnas": [FakePost("AAA", hours_ago=2)], "carolinegleich": []},
        error=instaloader.exceptions.TooManyRequestsException("429"), error_times=1,
    )

    posts, stats = fetch.fetch_day(
        sources_path, dest, CFG, loader=loader, now=NOW, sleep=no_sleep,
    )

    assert [p.shortcode for p in posts] == ["AAA"]
    assert watermarks(sources_path)["aaronparnas"] == NOW.isoformat()
    assert stats.incomplete is False


def test_a_private_or_missing_profile_is_skipped_and_flagged(sources_path, dest):
    import instaloader

    loader = FakeLoader(error=instaloader.exceptions.ProfileNotExistsException("gone"),
                        error_times=1)
    loader.posts = {"carolinegleich": [FakePost("BBB", hours_ago=1)]}

    posts, stats = fetch.fetch_day(
        sources_path, dest, CFG, loader=loader, now=NOW, sleep=no_sleep,
    )

    assert [p.shortcode for p in posts] == ["BBB"]
    assert stats.incomplete is True
    assert watermarks(sources_path)["aaronparnas"] is None
    assert watermarks(sources_path)["carolinegleich"] == NOW.isoformat()


def test_a_profile_error_is_not_retried(sources_path, dest):
    """Retrying a deleted account just burns requests against the rate limit."""
    import instaloader

    loader = FakeLoader(error=instaloader.exceptions.ProfileNotExistsException("gone"),
                        error_times=99)
    fetch.fetch_day(sources_path, dest, CFG, loader=loader, now=NOW, sleep=no_sleep)

    assert loader.attempts.count("aaronparnas") == 1


def test_one_handle_failing_does_not_stop_later_handles(sources_path, dest):
    loader = FakeLoader(
        {"carolinegleich": [FakePost("BBB", hours_ago=1)]},
        error=RuntimeError("network hiccup"), error_times=3,
    )

    posts, stats = fetch.fetch_day(
        sources_path, dest, CFG, loader=loader, now=NOW, sleep=no_sleep,
    )

    assert [p.shortcode for p in posts] == ["BBB"]
    assert stats.incomplete is True


def test_all_handles_disabled_is_a_clean_empty_run(tmp_path, dest):
    path = tmp_path / "sources.json"
    payload = {"version": 2, "sources": [
        {"handle": "aaronparnas", "enabled": False, "added": "2026-07-28",
         "last_pull_at": None, "last_seen": None},
    ]}
    path.write_text(json.dumps(payload), encoding="utf-8")

    posts, stats = run(path, dest, FakeLoader())

    assert posts == []
    assert stats.incomplete is False
    assert stats.post_count == 0


# --- lookup_profile (used by sources.add) ----------------------------------


def test_lookup_profile_raises_for_an_unreachable_handle():
    import instaloader

    loader = FakeLoader(error=instaloader.exceptions.ProfileNotExistsException("gone"),
                        error_times=99)

    with pytest.raises(Exception):
        fetch.lookup_profile(loader, "nosuchaccount")


# --- a full pull -----------------------------------------------------------


def test_start_of_day_is_local_midnight_as_utc():
    """Local, not UTC: the digest is dated by the local day, so 'today' has to
    mean the same day the file is named after."""
    from datetime import datetime as dt

    start = fetch.start_of_day(date(2026, 7, 29))

    assert start.tzinfo is not None
    assert start.utcoffset() == timedelta(0)
    assert start.astimezone().replace(tzinfo=None) == dt(2026, 7, 29, 0, 0)


def test_an_override_replaces_the_watermark():
    source = sources.Source(handle="a", last_pull_at="2026-07-29T16:33:00+00:00")
    override = datetime(2026, 7, 29, 6, 0, tzinfo=timezone.utc)

    assert fetch.cutoff_for(source, NOW, CFG, override=override) == override


def test_an_override_is_still_clamped_to_max_lookback():
    """--full on an ancient date must not crawl a whole profile."""
    source = sources.Source(handle="a", last_pull_at=None)
    ancient = datetime(2020, 1, 1, tzinfo=timezone.utc)

    assert fetch.cutoff_for(source, NOW, CFG, override=ancient) == NOW - timedelta(days=14)


def test_a_full_pull_re_scans_posts_the_watermark_had_passed(sources_path, dest):
    """The point of --full: this morning's run moved the watermark past posts an
    earlier failure missed."""
    sources.advance_watermark(sources_path, "aaronparnas", NOW - timedelta(hours=1))
    loader = FakeLoader({"aaronparnas": [FakePost("EARLIER", hours_ago=5)]})

    # A normal run sees nothing: the post predates the watermark.
    posts, _ = run(sources_path, dest, loader)
    assert posts == []

    second = FakeLoader({"aaronparnas": [FakePost("EARLIER", hours_ago=5)]})
    posts, _ = fetch.fetch_day(
        sources_path, dest, CFG, loader=second, now=NOW, sleep=no_sleep,
        since=NOW - timedelta(hours=12),
    )
    assert [p.shortcode for p in posts] == ["EARLIER"]


def test_a_full_pull_still_advances_the_watermark(sources_path, dest):
    loader = FakeLoader({"aaronparnas": [], "carolinegleich": []})

    fetch.fetch_day(
        sources_path, dest, CFG, loader=loader, now=NOW, sleep=no_sleep,
        since=NOW - timedelta(hours=12),
    )

    assert watermarks(sources_path)["aaronparnas"] == NOW.isoformat()


def test_a_full_pull_does_not_download_what_is_already_on_disk(sources_path, dest):
    dest.mkdir(parents=True)
    (dest / "aaronparnas_AAA.mp4").write_bytes(b"already here")
    loader = FakeLoader({"aaronparnas": [FakePost("AAA", hours_ago=2)]})

    posts, _ = fetch.fetch_day(
        sources_path, dest, CFG, loader=loader, now=NOW, sleep=no_sleep,
        since=NOW - timedelta(hours=12),
    )

    assert loader.downloaded == []
    assert [p.shortcode for p in posts] == ["AAA"]
