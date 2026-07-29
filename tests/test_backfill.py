import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest
from instaloader import exceptions as ig

import backfill
from src import config


TODAY = date(2026, 7, 29)
# run() backfills up to yesterday; today belongs to the daily run.
END = TODAY - timedelta(days=1)

CONFIG = """\
[paths]
news = "news"
raw = "data/raw"
transcripts = "data/transcripts"
logs = "logs"
sources = "config/sources.json"

[fetch]
session_user = "krys.newman"
"""

SOURCES = {
    "version": 2,
    "sources": [
        {"handle": "aaronparnas", "enabled": True, "added": "2026-06-01",
         "last_pull_at": None, "last_seen": None},
        {"handle": "oafnation_actual", "enabled": True, "added": "2026-06-01",
         "last_pull_at": None, "last_seen": None},
        {"handle": "sleeping", "enabled": False, "added": "2026-06-01",
         "last_pull_at": None, "last_seen": None},
    ],
}


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "config.toml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "sources.json").write_text(
        json.dumps(SOURCES, indent=2), encoding="utf-8")
    return config.load(tmp_path / "config.toml")


class FakePost:
    def __init__(self, shortcode, days_ago, is_video=True, caption="c", slides=0):
        self.shortcode = shortcode
        # Noon local, so the date bucket is unambiguous either side of midnight.
        local = datetime(TODAY.year, TODAY.month, TODAY.day, 12) - timedelta(days=days_ago)
        self.date_utc = local.astimezone(timezone.utc).replace(tzinfo=None)
        self.is_video = is_video
        self.caption = caption
        self.video_url = f"https://cdn.test/{shortcode}.mp4"
        self.url = f"https://cdn.test/{shortcode}.jpg"
        self.typename = "GraphSidecar" if slides else (
            "GraphVideo" if is_video else "GraphImage")
        self.slides = slides


class FakeLoader:
    def __init__(self, posts=None, raise_on_list=None, raise_on_download=None):
        self.posts = posts or {}
        self.raise_on_list = raise_on_list
        self.raise_on_download = raise_on_download
        self.walks = []
        self.downloads = []

    def posts_for(self, handle):
        self.walks.append(handle)
        if self.raise_on_list == handle:
            raise ig.TooManyRequestsException("429")
        yield from self.posts.get(handle, [])

    def download(self, post, dest):
        if self.raise_on_download == post.shortcode:
            raise ig.TooManyRequestsException("429")
        self.downloads.append(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"mp4")

    def download_image(self, url, dest):
        self.downloads.append(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"jpg")

    def slide_urls(self, post):
        if post.typename != "GraphSidecar":
            return [post.url]
        return [f"https://cdn.test/{post.shortcode}_{i}.jpg"
                for i in range(1, post.slides + 1)]


def no_sleep(_seconds):
    pass


def go(cfg, loader, **kwargs):
    kwargs.setdefault("days", 7)
    kwargs.setdefault("today", TODAY)
    kwargs.setdefault("sleep", no_sleep)
    return backfill.run(cfg, loader=loader, **kwargs)


# --- one walk per handle, not per day --------------------------------------


def test_each_profile_is_walked_exactly_once(cfg):
    """The point of the whole script. run_daily --date per day would walk each
    profile once per day — 210 walks for a month across seven handles."""
    loader = FakeLoader({
        "aaronparnas": [FakePost(f"A{i}", days_ago=i) for i in range(1, 8)],
        "oafnation_actual": [FakePost(f"O{i}", days_ago=i) for i in range(1, 8)],
    })

    go(cfg, loader, days=7, execute=True)

    assert loader.walks == ["aaronparnas", "oafnation_actual"]


def test_posts_are_filed_into_the_day_they_belong_to(cfg):
    loader = FakeLoader({"aaronparnas": [
        FakePost("DAY1", days_ago=1), FakePost("DAY3", days_ago=3),
    ]})

    go(cfg, loader, days=7, execute=True, handles=["aaronparnas"])

    raw = cfg.paths.raw
    assert (raw / (END).isoformat() / "aaronparnas_DAY1.mp4").is_file()
    assert (raw / (TODAY - timedelta(days=3)).isoformat() / "aaronparnas_DAY3.mp4").is_file()


def test_a_sidecar_is_written_with_the_kind(cfg):
    loader = FakeLoader({"aaronparnas": [FakePost("VID", days_ago=1)]})

    go(cfg, loader, days=7, execute=True, handles=["aaronparnas"])

    meta = json.loads(
        (cfg.paths.raw / END.isoformat() / "aaronparnas_VID.json").read_text())
    assert meta["kind"] == "video"
    assert meta["handle"] == "aaronparnas"
    assert meta["permalink"].endswith("/VID/")


def test_carousels_are_saved_slide_by_slide(cfg):
    loader = FakeLoader({"aaronparnas": [
        FakePost("CAR", days_ago=1, is_video=False, slides=3),
    ]})

    go(cfg, loader, days=7, execute=True, handles=["aaronparnas"])

    day = cfg.paths.raw / END.isoformat()
    assert (day / "aaronparnas_CAR_1.jpg").is_file()
    assert (day / "aaronparnas_CAR_3.jpg").is_file()
    assert json.loads((day / "aaronparnas_CAR.json").read_text())["kind"] == "image"


# --- the plan comes first --------------------------------------------------


def test_a_plan_run_downloads_nothing(cfg):
    """The cost has to be knowable before it is paid."""
    loader = FakeLoader({"aaronparnas": [FakePost("A", days_ago=1)]})

    code = go(cfg, loader, days=7, handles=["aaronparnas"])

    assert code == 0
    assert loader.downloads == []
    assert not cfg.paths.raw.exists() or not list(cfg.paths.raw.rglob("*.mp4"))


def test_the_plan_still_lists_posts(cfg, capsys):
    loader = FakeLoader({"aaronparnas": [
        FakePost("A", days_ago=1), FakePost("B", days_ago=2),
    ]})

    go(cfg, loader, days=7, handles=["aaronparnas"])

    out = capsys.readouterr().out
    assert "2 post(s)" in out
    assert "--execute" in out


# --- the range -------------------------------------------------------------


def test_today_is_left_to_the_daily_run(cfg):
    loader = FakeLoader({"aaronparnas": [
        FakePost("TODAY", days_ago=0), FakePost("YESTERDAY", days_ago=1),
    ]})

    go(cfg, loader, days=7, execute=True, handles=["aaronparnas"])

    filed = [p.name for p in cfg.paths.raw.rglob("*.mp4")]
    assert "aaronparnas_YESTERDAY.mp4" in filed
    assert "aaronparnas_TODAY.mp4" not in filed


def test_posts_older_than_the_range_are_not_collected(cfg):
    loader = FakeLoader({"aaronparnas": [
        FakePost("RECENT", days_ago=2), FakePost("ANCIENT", days_ago=40),
    ]})

    go(cfg, loader, days=7, execute=True, handles=["aaronparnas"])

    filed = [p.name for p in cfg.paths.raw.rglob("*.mp4")]
    assert filed == ["aaronparnas_RECENT.mp4"]


def test_a_pinned_old_post_does_not_end_the_walk(cfg):
    """Instagram returns pins first, and a pin can be years old."""
    loader = FakeLoader({"aaronparnas": [
        FakePost("PIN1", days_ago=400),
        FakePost("PIN2", days_ago=380),
        FakePost("PIN3", days_ago=370),
        FakePost("REAL1", days_ago=2),
        FakePost("REAL2", days_ago=4),
    ]})

    go(cfg, loader, days=7, execute=True, handles=["aaronparnas"])

    filed = sorted(p.name for p in cfg.paths.raw.rglob("*.mp4"))
    assert filed == ["aaronparnas_REAL1.mp4", "aaronparnas_REAL2.mp4"]


def test_disabled_handles_are_never_walked(cfg):
    loader = FakeLoader({"sleeping": [FakePost("NOPE", days_ago=1)]})
    go(cfg, loader, days=7, execute=True)
    assert "sleeping" not in loader.walks


def test_a_handle_subset_is_honoured(cfg):
    loader = FakeLoader({
        "aaronparnas": [FakePost("A", days_ago=1)],
        "oafnation_actual": [FakePost("O", days_ago=1)],
    })
    go(cfg, loader, days=7, execute=True, handles=["aaronparnas"])
    assert loader.walks == ["aaronparnas"]


# --- caps ------------------------------------------------------------------


def test_the_per_handle_cap_truncates_the_plan(cfg):
    loader = FakeLoader({
        "aaronparnas": [FakePost(f"A{i}", days_ago=1 + (i % 6)) for i in range(50)],
    })

    plan = backfill.enumerate_handle(
        loader, "aaronparnas", END - timedelta(days=6), END, max_posts=10)

    assert plan.total == 10
    assert "cap" in plan.truncated_reason


def test_the_walk_stops_soon_after_leaving_the_range(cfg):
    """Old posts end the walk once it is clear they are not pins."""
    loader = FakeLoader({
        "aaronparnas": [FakePost(f"A{i}", days_ago=500) for i in range(100)],
    })

    plan = backfill.enumerate_handle(
        loader, "aaronparnas", END - timedelta(days=6), END, max_posts=250)

    assert plan.total == 0
    # Far short of the 100 available: it did not crawl the profile.
    assert plan.scanned < 20


def test_the_scan_ceiling_stops_a_runaway_walk(cfg):
    """A backstop for the case where posts keep matching but never run out."""
    loader = FakeLoader({
        "aaronparnas": [FakePost(f"A{i}", days_ago=1) for i in range(100)],
    })

    plan = backfill.enumerate_handle(
        loader, "aaronparnas", END - timedelta(days=6), END,
        max_posts=250, scan_ceiling=20)

    assert plan.scanned == 21
    assert "ceiling" in plan.truncated_reason


# --- stopping safely -------------------------------------------------------


def test_a_rate_limit_while_listing_aborts_the_run(cfg):
    """A 429 is Instagram asking for less; answering with more is how a slowdown
    becomes a suspension."""
    loader = FakeLoader(
        {"aaronparnas": [FakePost("A", days_ago=1)]},
        raise_on_list="aaronparnas",
    )

    code = go(cfg, loader, days=7, execute=True)

    assert code == 2
    assert loader.downloads == []
    # It stopped rather than moving on to the next handle.
    assert loader.walks == ["aaronparnas"]


def test_a_rate_limit_while_downloading_aborts_the_run(cfg):
    # Days are downloaded oldest first, so EARLIER is reached before LATER.
    loader = FakeLoader(
        {"aaronparnas": [FakePost("LATER", days_ago=1), FakePost("EARLIER", days_ago=3)],
         "oafnation_actual": [FakePost("O", days_ago=1)]},
        raise_on_download="LATER",
    )

    code = go(cfg, loader, days=7, execute=True)

    assert code == 2
    # What was already collected survives on disk, so a re-run resumes.
    earlier = cfg.paths.raw / (TODAY - timedelta(days=3)).isoformat()
    assert (earlier / "aaronparnas_EARLIER.mp4").is_file()
    # And the next handle was never started.
    assert not any("oafnation" in str(p) for p in loader.downloads)


def test_a_rate_limit_is_never_retried(cfg):
    loader = FakeLoader(
        {"aaronparnas": [FakePost("A", days_ago=1)]},
        raise_on_list="aaronparnas",
    )
    go(cfg, loader, days=7, execute=True, handles=["aaronparnas"])
    assert loader.walks.count("aaronparnas") == 1


def test_an_ordinary_error_skips_one_handle_and_continues(cfg):
    class Grumpy(FakeLoader):
        def posts_for(self, handle):
            self.walks.append(handle)
            if handle == "aaronparnas":
                raise RuntimeError("transient")
            yield from self.posts.get(handle, [])

    loader = Grumpy({"oafnation_actual": [FakePost("O", days_ago=1)]})

    code = go(cfg, loader, days=7, execute=True)

    assert code == 0
    assert loader.walks == ["aaronparnas", "oafnation_actual"]


# --- resumability and pacing ----------------------------------------------


def test_already_downloaded_media_is_not_fetched_again(cfg):
    day = cfg.paths.raw / END.isoformat()
    day.mkdir(parents=True)
    (day / "aaronparnas_A.mp4").write_bytes(b"already here")

    loader = FakeLoader({"aaronparnas": [FakePost("A", days_ago=1)]})
    go(cfg, loader, days=7, execute=True, handles=["aaronparnas"])

    assert loader.downloads == []


def test_it_waits_between_handles(cfg):
    slept = []
    loader = FakeLoader({
        "aaronparnas": [FakePost("A", days_ago=1)],
        "oafnation_actual": [FakePost("O", days_ago=1)],
    })

    backfill.run(cfg, days=7, execute=True, loader=loader, today=TODAY,
                 sleep=slept.append, delay_handles=90.0, delay_posts=2.0)

    assert 90.0 in slept
    assert 2.0 in slept


def test_no_wait_is_added_after_a_post_that_was_already_on_disk(cfg):
    day = cfg.paths.raw / END.isoformat()
    day.mkdir(parents=True)
    (day / "aaronparnas_A.mp4").write_bytes(b"already here")

    slept = []
    loader = FakeLoader({"aaronparnas": [FakePost("A", days_ago=1)]})
    backfill.run(cfg, days=7, execute=True, loader=loader, today=TODAY,
                 sleep=slept.append, handles=["aaronparnas"], delay_posts=2.0)

    assert 2.0 not in slept


def test_it_prints_the_commands_to_summarize_what_it_collected(cfg, capsys):
    loader = FakeLoader({"aaronparnas": [FakePost("A", days_ago=2)]})

    go(cfg, loader, days=7, execute=True, handles=["aaronparnas"])

    out = capsys.readouterr().out
    stamp = (TODAY - timedelta(days=2)).isoformat()
    assert f"--date {stamp} --no-fetch --quiet" in out


def test_no_matching_handles_is_an_error(cfg):
    loader = FakeLoader()
    assert go(cfg, loader, handles=["nobody"]) == 1
