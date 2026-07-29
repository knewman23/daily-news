import json
import subprocess
from datetime import date, datetime, timezone

import pytest

from src import digest, notes, summarize
from src.records import Stats, Transcript


DAY = date(2026, 7, 28)
GENERATED = datetime(2026, 7, 28, 11, 0, 4, tzinfo=timezone.utc)

TRANSCRIPTS = [
    Transcript(
        handle="aaronparnas",
        shortcode="AAA",
        text="The Senate passed the spending bill today after weekend negotiation.",
        caption="Breaking: spending bill",
        permalink="https://www.instagram.com/p/AAA/",
    ),
    Transcript(
        handle="total.hipocrisy",
        shortcode="BBB",
        text="Big news this morning: the spending bill cleared the Senate.",
        caption="",
        permalink="https://www.instagram.com/p/BBB/",
    ),
    Transcript(
        handle="carolinegleich",
        shortcode="CCC",
        text="Nvidia reported earnings above its own guidance this quarter.",
        caption="markets",
        permalink="https://www.instagram.com/p/CCC/",
    ),
]

TOPICS_JSON = {
    "topics": [
        {
            "headline": "Senate passes the spending bill",
            "body": "The chamber cleared the measure after a weekend of negotiation.",
            "tags": ["politics"],
            "sources": ["@aaronparnas", "@total.hipocrisy"],
        },
        {
            "headline": "Nvidia earnings beat estimates",
            "body": "Revenue came in ahead of guidance.",
            "tags": ["markets"],
            "sources": ["@carolinegleich"],
        },
    ]
}


class FakeRunner:
    """Stands in for subprocess.run. Returns queued results, records the calls."""

    def __init__(self, *results):
        self.queue = list(results)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, **kwargs})
        if not self.queue:
            raise AssertionError("runner called more times than results were queued")
        return self.queue.pop(0)


def wrapper(result_text, *, is_error=False, returncode=0):
    payload = json.dumps({
        "is_error": is_error,
        "result": result_text,
        "type": "result",
    })
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=payload, stderr=""
    )


def ok_runner():
    return FakeRunner(wrapper(json.dumps(TOPICS_JSON)))


# --- build_prompt ----------------------------------------------------------


def test_prompt_includes_every_transcript_with_attribution():
    prompt = summarize.build_prompt(TRANSCRIPTS)
    for t in TRANSCRIPTS:
        assert t.handle in prompt
        assert t.text in prompt
    assert "Breaking: spending bill" in prompt
    assert "https://www.instagram.com/p/AAA/" in prompt


def test_prompt_states_the_same_day_dedup_rule_and_the_schema():
    prompt = summarize.build_prompt(TRANSCRIPTS)
    lowered = prompt.lower()
    assert "same story" in lowered
    assert "headline" in prompt
    assert "sources" in prompt
    assert "tags" in prompt
    assert "topics" in prompt


def test_prompt_survives_an_empty_transcript_list():
    assert summarize.build_prompt([]).strip()


# --- call_claude -----------------------------------------------------------


def test_parses_a_well_formed_response():
    topics = summarize.call_claude("prompt", runner=ok_runner())
    assert [t["headline"] for t in topics] == [
        "Senate passes the spending bill", "Nvidia earnings beat estimates",
    ]


def test_prompt_goes_on_stdin_not_argv():
    runner = ok_runner()
    summarize.call_claude("PROMPT-SENTINEL", runner=runner)

    call = runner.calls[0]
    assert call["input"] == "PROMPT-SENTINEL"
    assert "PROMPT-SENTINEL" not in call["cmd"]


def test_invocation_pins_the_model_and_skips_mcp():
    runner = ok_runner()
    summarize.call_claude("prompt", runner=runner)

    cmd = runner.calls[0]["cmd"]
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "--output-format" in cmd and "json" in cmd
    assert "--strict-mcp-config" in cmd
    assert "--model" in cmd
    assert summarize.DEFAULT_MODEL in cmd


def test_tolerates_a_fenced_json_payload():
    fenced = "```json\n" + json.dumps(TOPICS_JSON) + "\n```"
    topics = summarize.call_claude("prompt", runner=FakeRunner(wrapper(fenced)))
    assert len(topics) == 2


def test_tolerates_prose_around_the_json_object():
    noisy = "Here you go:\n" + json.dumps(TOPICS_JSON) + "\nHope that helps."
    topics = summarize.call_claude("prompt", runner=FakeRunner(wrapper(noisy)))
    assert len(topics) == 2


def test_retries_exactly_once_then_raises_on_unparseable_output():
    runner = FakeRunner(wrapper("no json at all"), wrapper("still nothing"))
    with pytest.raises(summarize.SummarizeError):
        summarize.call_claude("prompt", runner=runner)
    assert len(runner.calls) == 2


def test_a_retry_that_succeeds_is_used():
    runner = FakeRunner(wrapper("garbage"), wrapper(json.dumps(TOPICS_JSON)))
    assert len(summarize.call_claude("prompt", runner=runner)) == 2
    assert len(runner.calls) == 2


def test_non_zero_exit_is_retried_then_raises():
    runner = FakeRunner(
        wrapper("", returncode=1), wrapper("", returncode=1),
    )
    with pytest.raises(summarize.SummarizeError):
        summarize.call_claude("prompt", runner=runner)
    assert len(runner.calls) == 2


def test_is_error_true_is_a_failure_even_though_the_exit_code_is_zero():
    payload = json.dumps(TOPICS_JSON)
    runner = FakeRunner(
        wrapper(payload, is_error=True), wrapper(payload, is_error=True),
    )
    with pytest.raises(summarize.SummarizeError):
        summarize.call_claude("prompt", runner=runner)


def test_unparseable_wrapper_is_a_failure():
    broken = subprocess.CompletedProcess(
        args=["claude"], returncode=0, stdout="not json", stderr=""
    )
    with pytest.raises(summarize.SummarizeError):
        summarize.call_claude("prompt", runner=FakeRunner(broken, broken))


@pytest.mark.parametrize("payload", [
    {"topics": [{"body": "b", "tags": [], "sources": []}]},
    {"topics": [{"headline": "A", "tags": [], "sources": []}]},
    {"topics": "not-a-list"},
    {"no_topics_key": []},
    [{"headline": "A", "body": "b"}],
])
def test_wrong_shape_raises_with_a_clear_message(payload):
    text = json.dumps(payload)
    runner = FakeRunner(wrapper(text), wrapper(text))
    with pytest.raises(summarize.SummarizeError) as exc:
        summarize.call_claude("prompt", runner=runner)
    assert "topics" in str(exc.value) or "headline" in str(exc.value) or "body" in str(exc.value)


def test_zero_topics_is_valid_not_an_error():
    runner = FakeRunner(wrapper(json.dumps({"topics": []})))
    assert summarize.call_claude("prompt", runner=runner) == []
    assert len(runner.calls) == 1


# --- summarize_day (integration) ------------------------------------------


def test_summarize_day_collapses_a_shared_story_and_writes_a_readable_file(tmp_path):
    stats = Stats(post_count=3, transcribed_count=3)

    path = summarize.summarize_day(
        DAY, TRANSCRIPTS, stats, tmp_path,
        runner=ok_runner(), generated=GENERATED,
    )

    assert path == tmp_path / "2026-07-28.md"

    topics = digest.topics_of(path)
    assert len(topics) == 2
    assert topics[0].sources == ["@aaronparnas", "@total.hipocrisy"]

    meta = digest.list_days(tmp_path)[0]
    assert meta.date == DAY
    assert meta.post_count == 3
    assert meta.incomplete is False

    notes.write_notes(path, "my reaction")
    assert notes.read_notes(path) == "my reaction"
    assert "Senate passes the spending bill" in path.read_text(encoding="utf-8")


def test_summarize_day_marks_an_incomplete_run(tmp_path):
    stats = Stats(post_count=4, transcribed_count=3)
    stats.fail("rate limited: carolinegleich")

    path = summarize.summarize_day(
        DAY, TRANSCRIPTS, stats, tmp_path,
        runner=ok_runner(), generated=GENERATED,
    )
    assert digest.list_days(tmp_path)[0].incomplete is True


def test_summarize_day_with_no_transcripts_writes_a_file_without_calling_the_model(tmp_path):
    runner = FakeRunner()  # any call raises

    path = summarize.summarize_day(
        DAY, [], Stats(), tmp_path, runner=runner, generated=GENERATED,
    )

    assert runner.calls == []
    assert "No posts found" in path.read_text(encoding="utf-8")
    assert digest.topics_of(path) == []
    notes.write_notes(path, "quiet day")
    assert notes.read_notes(path) == "quiet day"


def test_summarize_day_creates_the_news_directory(tmp_path):
    target = tmp_path / "news"
    summarize.summarize_day(
        DAY, TRANSCRIPTS, Stats(), target, runner=ok_runner(), generated=GENERATED,
    )
    assert (target / "2026-07-28.md").exists()


def test_summarize_day_leaves_no_temp_file(tmp_path):
    summarize.summarize_day(
        DAY, TRANSCRIPTS, Stats(), tmp_path, runner=ok_runner(), generated=GENERATED,
    )
    assert list(tmp_path.glob("*.tmp")) == []
