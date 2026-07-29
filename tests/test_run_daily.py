import json
from datetime import date, datetime, timezone

import pytest

import run_daily
from src import config, digest, fetch, notes, prune, publish
from src.records import PostRef, Stats, Transcript


DAY = date(2026, 7, 28)
GENERATED = datetime(2026, 7, 28, 11, 0, 4, tzinfo=timezone.utc)

CONFIG_TOML = """\
[paths]
news = "news"
raw = "data/raw"
transcripts = "data/transcripts"
logs = "logs"
sources = "config/sources.json"

[fetch]
session_user = "krys.newman"

[transcribe]
model = "small"

[serve]
port = 8420
"""

SOURCES = {
    "version": 2,
    "sources": [
        {"handle": "aaronparnas", "enabled": True, "added": "2026-07-28",
         "last_pull_at": None, "last_seen": None},
        {"handle": "oafnation_actual", "enabled": True, "added": "2026-07-28",
         "last_pull_at": None, "last_seen": None},
    ],
}

TOPICS = [{
    "headline": "Senate passes the spending bill",
    "body": "The chamber cleared the measure after a weekend of negotiation.",
    "tags": ["politics"],
    "sources": ["@aaronparnas"],
}]

AUDIO = Transcript(handle="aaronparnas", shortcode="AAA",
                   text="The Senate passed the spending bill today.", kind="audio")
IMAGE = Transcript(handle="oafnation_actual", shortcode="BBB",
                   text="PROSECUTORS SAY BERLIN PRIDE ATTACK", kind="image")


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(CONFIG_TOML, encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "sources.json").write_text(
        json.dumps(SOURCES, indent=2), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


class Spy:
    """Records that a stage ran, and what it was asked to do."""

    def __init__(self, result, raises=None):
        self.result = result
        self.raises = raises
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.result


def cli_result(topics):
    """A fake claude -p runner returning the given topics in the real wrapper shape."""
    import subprocess as sp

    payload = json.dumps({
        "is_error": False,
        "result": json.dumps({"topics": topics}),
        "type": "result",
    })
    return lambda cmd, **kwargs: sp.CompletedProcess(cmd, 0, stdout=payload, stderr="")


class SummarizerSpy:
    """Stands in for summarize_day, honouring its contract: it writes the file.

    A spy that only returned topics would let the orchestrator's notes-carrying
    and re-run behaviour pass untested, since there would be no file to carry
    notes into.
    """

    def __init__(self, topics, raises=None):
        self.topics = topics
        self.raises = raises
        self.calls = 0
        self.transcripts = None
        self.stats = None

    def __call__(self, day, transcripts, stats, news_dir, **kwargs):
        self.calls += 1
        self.transcripts = list(transcripts)
        self.stats = stats
        if self.raises:
            raise self.raises
        from src import summarize
        return summarize.summarize_day(
            day, transcripts, stats, news_dir,
            runner=cli_result(self.topics), **kwargs,
        )


def stages(
    posts=None, fetch_stats=None,
    audio=None, audio_stats=None,
    images=None, image_stats=None,
    topics=None, fetch_raises=None, summarize_raises=None,
):
    """Build a full set of injected stages with sensible defaults."""
    return {
        "fetcher": Spy(
            (posts if posts is not None else [PostRef("aaronparnas", "AAA", "t", "l")],
             fetch_stats or Stats(post_count=1)),
            raises=fetch_raises,
        ),
        "transcriber": Spy(
            (audio if audio is not None else [AUDIO],
             audio_stats or Stats(transcribed_count=1)),
        ),
        "ocr_runner": Spy(
            (images if images is not None else [],
             image_stats or Stats()),
        ),
        "summarizer": SummarizerSpy(topics if topics is not None else TOPICS,
                                    raises=summarize_raises),
    }


def run(project, **overrides):
    kit = stages(**overrides.pop("stage_kwargs", {}))
    kit.update(overrides)
    notified = []
    code = run_daily.run_day(
        DAY, config.load(project / "config.toml"),
        notifier=notified.append, generated=GENERATED, **kit,
    )
    return code, kit, notified


# --- the happy path --------------------------------------------------------


def test_a_successful_run_writes_the_digest_and_exits_zero(project):
    code, kit, notified = run(project)

    assert code == 0
    assert notified == []
    assert all(spy.calls == 1 for spy in kit.values())

    path = project / "news" / "2026-07-28.md"
    assert path.exists()
    assert digest.topics_of(path)[0].headline == "Senate passes the spending bill"
    assert digest.list_days(project / "news")[0].incomplete is False


def test_stages_run_in_pipeline_order(project):
    order = []

    def spy(name, result):
        def call(*args, **kwargs):
            order.append(name)
            return result
        return call

    run_daily.run_day(
        DAY, config.load(project / "config.toml"),
        fetcher=spy("fetch", ([], Stats())),
        transcriber=spy("transcribe", ([AUDIO], Stats(transcribed_count=1))),
        ocr_runner=spy("ocr", ([], Stats())),
        summarizer=spy("summarize", TOPICS),
        notifier=lambda _m: None, generated=GENERATED,
    )

    assert order == ["fetch", "transcribe", "ocr", "summarize"]


def test_audio_and_image_text_are_both_handed_to_the_summarizer(project):
    _, kit, _ = run(project, stage_kwargs={
        "audio": [AUDIO],
        "images": [IMAGE], "image_stats": Stats(transcribed_count=1),
    })

    passed = kit["summarizer"].transcripts
    assert [t.shortcode for t in passed] == ["AAA", "BBB"]
    assert {t.kind for t in passed} == {"audio", "image"}


def test_transcribed_count_sums_audio_and_image(project):
    run(project, stage_kwargs={
        "audio": [AUDIO], "audio_stats": Stats(post_count=1, transcribed_count=1),
        "images": [IMAGE], "image_stats": Stats(post_count=1, transcribed_count=1),
        "fetch_stats": Stats(post_count=2),
    })

    meta = digest.list_days(project / "news")[0]
    assert meta.post_count == 2
    assert meta.transcribed_count == 2


# --- empty and disabled runs ----------------------------------------------


def test_zero_posts_still_writes_a_file(project):
    """A silent pipeline failure must be distinguishable from a quiet news day."""
    code, _, _ = run(project, stage_kwargs={
        "posts": [], "fetch_stats": Stats(), "audio": [], "topics": [],
    })

    assert code == 0
    path = project / "news" / "2026-07-28.md"
    assert "No posts found" in path.read_text(encoding="utf-8")


def test_all_handles_disabled_writes_nothing_and_exits_zero(project):
    payload = json.loads((project / "config" / "sources.json").read_text())
    for source in payload["sources"]:
        source["enabled"] = False
    (project / "config" / "sources.json").write_text(json.dumps(payload), encoding="utf-8")

    code, kit, notified = run(project)

    assert code == 0
    assert notified == []
    assert kit["fetcher"].calls == 0
    assert not (project / "news").exists() or not list((project / "news").glob("*.md"))


# --- partial failures ------------------------------------------------------


@pytest.mark.parametrize("field", ["fetch_stats", "audio_stats", "image_stats"])
def test_a_failure_in_any_stage_marks_the_day_incomplete(project, field):
    failed = Stats()
    failed.fail("something broke")

    run(project, stage_kwargs={field: failed})

    assert digest.list_days(project / "news")[0].incomplete is True


def test_stage_failure_notes_reach_the_frontmatter_count_not_the_body(project):
    failed = Stats(post_count=3)
    failed.fail("rate limited: aaronparnas")

    run(project, stage_kwargs={"fetch_stats": failed})

    text = (project / "news" / "2026-07-28.md").read_text(encoding="utf-8")
    assert "incomplete: true" in text
    assert "rate limited" not in text     # operational detail belongs in the log


# --- hard failures ---------------------------------------------------------


def test_expired_session_exits_non_zero_and_notifies(project):
    code, _, notified = run(project, stage_kwargs={
        "fetch_raises": fetch.SessionExpired("session rejected"),
    })

    assert code != 0
    assert notified
    assert not list((project / "news").glob("*.md"))


def test_expired_session_logs_the_relogin_command(project):
    run(project, stage_kwargs={
        "fetch_raises": fetch.SessionExpired("session rejected"),
    })

    log = (project / "logs" / "2026-07-28.log").read_text(encoding="utf-8")
    assert "--load-cookies" in log


def test_a_summarizer_failure_exits_non_zero_and_notifies(project):
    from src.summarize import SummarizeError

    code, _, notified = run(project, stage_kwargs={
        "summarize_raises": SummarizeError("claude -p failed"),
    })

    assert code != 0
    assert notified


def test_a_summarizer_failure_leaves_transcripts_alone(project):
    """Re-running after a fix must cost seconds, not a full re-transcription."""
    from src.summarize import SummarizeError

    _, kit, _ = run(project, stage_kwargs={
        "summarize_raises": SummarizeError("boom"),
    })

    assert kit["transcriber"].calls == 1
    assert kit["fetcher"].calls == 1


# --- re-runs ---------------------------------------------------------------


def test_a_rerun_preserves_the_journal_notes(project):
    """Re-running a day must never cost the user their journal entry."""
    run(project)
    path = project / "news" / "2026-07-28.md"
    notes.write_notes(path, "This one worries me.")

    run(project)

    assert notes.read_notes(path) == "This one worries me."
    assert "Senate passes the spending bill" in path.read_text(encoding="utf-8")


def test_a_rerun_does_not_duplicate_topics(project):
    run(project)
    run(project)

    assert len(digest.topics_of(project / "news" / "2026-07-28.md")) == 1


def test_a_rerun_after_a_broken_notes_block_does_not_lose_the_summary(project):
    run(project)
    path = project / "news" / "2026-07-28.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(notes.END, ""), encoding="utf-8"
    )

    code, _, _ = run(project)

    assert code == 0
    assert digest.topics_of(path)[0].headline == "Senate passes the spending bill"


# --- CLI -------------------------------------------------------------------


def test_date_argument_selects_the_day(project):
    assert run_daily.parse_args(["--date", "2026-07-01"]).date == date(2026, 7, 1)


def test_date_defaults_to_today(project):
    assert run_daily.parse_args([]).date == date.today()


def test_a_malformed_date_is_rejected(project):
    with pytest.raises(SystemExit):
        run_daily.parse_args(["--date", "not-a-date"])


# --- counting across a re-run ---------------------------------------------


def test_post_count_comes_from_the_extract_stages_not_fetch(project):
    """On a re-run nothing is newly fetched, but the day still has its posts.

    Taking fetch's count would report a 28-post day as a 0-post day the moment
    the files were already on disk.
    """
    run(project, stage_kwargs={
        "posts": [],                                  # nothing newly downloaded
        "fetch_stats": Stats(post_count=0),
        "audio": [AUDIO], "audio_stats": Stats(post_count=18, transcribed_count=18),
        "images": [IMAGE], "image_stats": Stats(post_count=14, transcribed_count=14),
    })

    meta = digest.list_days(project / "news")[0]
    assert meta.post_count == 32
    assert meta.transcribed_count == 32


def test_a_fetch_failure_still_marks_a_rerun_incomplete(project):
    failed = Stats(post_count=0)
    failed.fail("fetch total.hipocrisy: Profile does not exist.")

    run(project, stage_kwargs={
        "posts": [], "fetch_stats": failed,
        "audio_stats": Stats(post_count=1, transcribed_count=1),
    })

    assert digest.list_days(project / "news")[0].incomplete is True


# --- publishing ------------------------------------------------------------


class PublisherSpy:
    def __init__(self, ok=True, message="published"):
        self.result = publish.PublishResult(ok, ok, message)
        self.calls = []

    def __call__(self, cfg, day, topic_count=0):
        self.calls.append({"day": day, "topic_count": topic_count})
        return self.result


def test_the_publisher_is_told_how_many_topics_were_written(project):
    spy = PublisherSpy()
    run(project, publisher=spy)

    assert spy.calls == [{"day": DAY, "topic_count": 1}]


def test_publishing_happens_after_the_digest_is_written(project):
    seen = {}

    def publisher(cfg, day, topic_count=0):
        seen["exists"] = (project / "news" / "2026-07-28.md").exists()
        return publish.PublishResult(True, True, "ok")

    run(project, publisher=publisher)
    assert seen["exists"] is True


def test_a_publish_failure_does_not_fail_the_run(project):
    """The digest is written and readable locally; losing GitHub is not worth
    discarding a successful run over."""
    code, _, notified = run(project, publisher=PublisherSpy(
        ok=False, message="fatal: could not read from remote",
    ))

    assert code == 0
    assert any("not pushed" in note for note in notified)


def test_a_publish_failure_is_recorded_against_the_run(project):
    run(project, publisher=PublisherSpy(ok=False, message="no upstream"))

    entry = json.loads((project / "logs" / "runs.json").read_text())["runs"][0]
    assert entry["incomplete"] is True
    assert any("publish" in note for note in entry["failures"])


def test_a_summarize_failure_skips_publishing_entirely(project):
    from src.summarize import SummarizeError

    spy = PublisherSpy()
    run(project, publisher=spy, stage_kwargs={
        "summarize_raises": SummarizeError("boom"),
    })

    assert spy.calls == []


# --- email -----------------------------------------------------------------


class EmailerSpy:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def __call__(self, cfg, subject, body):
        self.calls.append({"subject": subject, "body": body})
        from src.mailer import MailResult
        return MailResult(self.ok, self.ok, "sent" if self.ok else "smtp refused")


def test_the_email_carries_the_days_headlines(project):
    spy = EmailerSpy()
    run(project, emailer=spy)

    assert len(spy.calls) == 1
    assert "1 topic for July 28" in spy.calls[0]["subject"]
    assert "Senate passes the spending bill" in spy.calls[0]["body"]


def test_the_email_is_sent_after_publishing_so_the_link_is_live(project):
    order = []

    def publisher(cfg, day, topic_count=0):
        order.append("publish")
        return publish.PublishResult(True, True, "ok")

    def emailer(cfg, subject, body):
        order.append("email")
        from src.mailer import MailResult
        return MailResult(True, True, "sent")

    run(project, publisher=publisher, emailer=emailer)
    assert order == ["publish", "email"]


def test_an_email_failure_does_not_fail_the_run(project):
    code, _, _ = run(project, emailer=EmailerSpy(ok=False))
    assert code == 0


def test_a_publish_failure_reaches_the_email_body(project):
    """The email is the one place a failure is guaranteed to be seen."""
    spy = EmailerSpy()
    run(project, publisher=PublisherSpy(ok=False, message="no upstream"), emailer=spy)

    assert "(incomplete)" in spy.calls[0]["subject"]
    assert "no upstream" in spy.calls[0]["body"]


def test_a_quiet_day_still_sends(project):
    spy = EmailerSpy()
    run(project, emailer=spy, stage_kwargs={
        "posts": [], "fetch_stats": Stats(), "audio": [], "topics": [],
    })

    assert "no news" in spy.calls[0]["subject"]


# --- disk reclamation ------------------------------------------------------


class PrunerSpy:
    def __init__(self, files=0, mb=0.0):
        self.files = files
        self.mb = mb
        self.calls = []

    def __call__(self, raw, transcripts, news, keep_days):
        self.calls.append({"raw": raw, "keep_days": keep_days})
        result = prune.PruneResult(days=1 if self.files else 0, files=self.files)
        result.bytes_freed = int(self.mb * 1_048_576)
        return result


def test_pruning_runs_last_with_the_configured_window(project):
    spy = PrunerSpy(files=12, mb=260.0)
    run(project, pruner=spy)

    assert len(spy.calls) == 1
    assert spy.calls[0]["keep_days"] == 3          # the default


def test_pruning_happens_after_the_email(project):
    order = []

    def emailer(cfg, subject, body):
        order.append("email")
        from src.mailer import MailResult
        return MailResult(True, True, "sent")

    def pruner(raw, transcripts, news, keep_days):
        order.append("prune")
        return prune.PruneResult()

    run(project, emailer=emailer, pruner=pruner)
    assert order == ["email", "prune"]


def test_a_summarize_failure_skips_pruning(project):
    """Nothing is deleted for a day that never got a digest."""
    from src.summarize import SummarizeError

    spy = PrunerSpy()
    run(project, pruner=spy, stage_kwargs={"summarize_raises": SummarizeError("boom")})

    assert spy.calls == []


def test_orphan_media_is_flagged_against_the_day(project):
    """Media with no sidecar belongs to no post, so nothing would transcribe it
    and it would vanish from the digest without a word."""
    raw = project / "data" / "raw" / "2026-07-28"
    raw.mkdir(parents=True)
    (raw / "aaronparnas_LOST.mp4").write_bytes(b"orphan")

    run(project, pruner=PrunerSpy())

    assert digest.list_days(project / "news")[0].incomplete is True
    entry = json.loads((project / "logs" / "runs.json").read_text())["runs"][0]
    assert any("no caption sidecar" in note for note in entry["failures"])
