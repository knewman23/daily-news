"""Flipping a topic's skipped state must never damage anything else in the file.

The digest and the journal share one file, and the journal cannot be regenerated.
So the tests that matter here are the ones asserting on what did *not* change.
"""

import pytest

from src import digest, notes, topics


DAY = """\
---
date: 2026-07-28
generated: 2026-07-28T11:00:04+00:00
tags: [politics, markets]
sources: ["@aaronparnas"]
post_count: 3
transcribed_count: 2
incomplete: false
---

# July 28, 2026

## Senate passes the spending bill
tags: politics
sources: [@aaronparnas](https://www.instagram.com/p/AAA/)

The Senate passed the bill after weekend negotiation.

## Nvidia beats its own guidance
tags: markets

Nvidia reported earnings above guidance.

## Merch drop announcement
tags: media
skipped: promotional, reports no news

A new hoodie went on sale.

## My Notes
<!-- notes:start -->
I care about the spending bill. Note the ## marks here.
<!-- notes:end -->
"""


@pytest.fixture
def day(tmp_path):
    path = tmp_path / "2026-07-28.md"
    path.write_text(DAY, encoding="utf-8")
    return path


def reasons(path):
    return {t.headline: t.skipped for t in digest.topics_of(path)}


# --- marking ---------------------------------------------------------------


def test_marks_a_topic_as_skipped(day):
    topics.set_skipped(day, "Nvidia beats its own guidance", "not interested")
    assert reasons(day)["Nvidia beats its own guidance"] == "not interested"


def test_marking_defaults_the_reason(day):
    stored = topics.set_skipped(day, "Nvidia beats its own guidance")
    assert stored == topics.DEFAULT_REASON
    assert reasons(day)["Nvidia beats its own guidance"] == topics.DEFAULT_REASON


def test_marking_puts_the_line_after_the_existing_meta_lines(day):
    topics.set_skipped(day, "Senate passes the spending bill", "changed my mind")
    section = day.read_text(encoding="utf-8").split("## ")[1]
    assert section.splitlines()[:4] == [
        "Senate passes the spending bill",
        "tags: politics",
        "sources: [@aaronparnas](https://www.instagram.com/p/AAA/)",
        "skipped: changed my mind",
    ]


def test_marking_leaves_the_body_intact(day):
    topics.set_skipped(day, "Nvidia beats its own guidance")
    body = {t.headline: t.body for t in digest.topics_of(day)}
    assert body["Nvidia beats its own guidance"] == "Nvidia reported earnings above guidance."


def test_remarking_replaces_the_reason_rather_than_stacking(day):
    topics.set_skipped(day, "Merch drop announcement", "still promotional")
    text = day.read_text(encoding="utf-8")
    assert text.count("skipped:") == 1
    assert reasons(day)["Merch drop announcement"] == "still promotional"


# --- restoring -------------------------------------------------------------


def test_restores_a_skipped_topic(day):
    topics.clear_skipped(day, "Merch drop announcement")
    assert reasons(day)["Merch drop announcement"] == ""
    assert "skipped:" not in day.read_text(encoding="utf-8")


def test_restoring_keeps_the_body_and_tags(day):
    topics.clear_skipped(day, "Merch drop announcement")
    restored = next(t for t in digest.topics_of(day)
                    if t.headline == "Merch drop announcement")
    assert restored.body == "A new hoodie went on sale."
    assert restored.tags == ["media"]
    assert restored.kept


def test_restoring_makes_it_searchable(day, tmp_path):
    assert digest.search(tmp_path, "hoodie") == []
    topics.clear_skipped(day, "Merch drop announcement")
    assert [h.headline for h in digest.search(tmp_path, "hoodie")] == [
        "Merch drop announcement"
    ]


def test_restoring_puts_it_in_the_feed(day):
    assert "hoodie" not in digest.render_html(day)
    topics.clear_skipped(day, "Merch drop announcement")
    assert "hoodie" in digest.render_html(day)


def test_restoring_something_already_kept_is_a_no_op(day):
    before = day.read_text(encoding="utf-8")
    topics.clear_skipped(day, "Nvidia beats its own guidance")
    assert day.read_text(encoding="utf-8") == before


# --- what must not change --------------------------------------------------


def test_the_journal_survives_a_round_trip(day):
    before = notes.read_notes(day)
    topics.set_skipped(day, "Senate passes the spending bill", "on reflection, no")
    topics.clear_skipped(day, "Senate passes the spending bill")
    assert notes.read_notes(day) == before


def test_a_round_trip_restores_the_file_byte_for_byte(day):
    before = day.read_text(encoding="utf-8")
    topics.set_skipped(day, "Nvidia beats its own guidance", "not interested")
    topics.clear_skipped(day, "Nvidia beats its own guidance")
    assert day.read_text(encoding="utf-8") == before


def test_editing_one_topic_leaves_the_others_untouched(day):
    topics.set_skipped(day, "Nvidia beats its own guidance", "not interested")
    after = {t.headline: (t.body, t.tags, t.sources, t.skipped)
             for t in digest.topics_of(day)}

    assert after["Senate passes the spending bill"] == (
        "The Senate passed the bill after weekend negotiation.",
        ["politics"],
        ["@aaronparnas"],       # the sources line keeps the @; links strip it
        "",
    )
    assert after["Merch drop announcement"][3] == "promotional, reports no news"


def test_the_frontmatter_is_not_rewritten(day):
    topics.set_skipped(day, "Senate passes the spending bill", "no")
    assert day.read_text(encoding="utf-8").startswith(DAY[:DAY.index("# July")])


def test_a_heading_inside_the_journal_is_never_matched(day):
    """The notes here contain `##`. Splitting the journal off first is what
    stops a note from being treated as a topic."""
    with pytest.raises(topics.TopicError):
        topics.set_skipped(day, "marks here.")


# --- refusals --------------------------------------------------------------


def test_an_unknown_headline_is_refused(day):
    before = day.read_text(encoding="utf-8")
    with pytest.raises(topics.TopicError, match="no topic headed"):
        topics.set_skipped(day, "A story that is not there")
    assert day.read_text(encoding="utf-8") == before


def test_an_ambiguous_headline_is_refused_rather_than_guessed(tmp_path):
    path = tmp_path / "2026-07-28.md"
    path.write_text(
        "# July 28, 2026\n\n## Same headline\n\nFirst.\n\n"
        "## Same headline\n\nSecond.\n\n"
        f"## My Notes\n{notes.START}\n{notes.END}\n",
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8")

    with pytest.raises(topics.TopicError, match="ambiguous"):
        topics.set_skipped(path, "Same headline")
    assert path.read_text(encoding="utf-8") == before


def test_an_empty_headline_is_refused(day):
    with pytest.raises(topics.TopicError, match="no headline"):
        topics.set_skipped(day, "   ")


@pytest.mark.parametrize("bad", [notes.START, notes.END])
def test_a_reason_may_not_forge_a_notes_marker(day, bad):
    before = day.read_text(encoding="utf-8")
    with pytest.raises(topics.TopicError, match="markers"):
        topics.set_skipped(day, "Nvidia beats its own guidance", f"why {bad} not")
    assert day.read_text(encoding="utf-8") == before


def test_a_newline_in_a_reason_cannot_leak_into_the_body(day):
    """Collapsed rather than rejected: the tail would otherwise become prose."""
    topics.set_skipped(day, "Nvidia beats its own guidance", "one\ntwo\nthree")
    assert reasons(day)["Nvidia beats its own guidance"] == "one two three"
    body = {t.headline: t.body for t in digest.topics_of(day)}
    assert body["Nvidia beats its own guidance"] == "Nvidia reported earnings above guidance."


def test_a_headline_is_matched_on_collapsed_whitespace(day):
    topics.set_skipped(day, "  Nvidia   beats its own guidance  ", "spacing")
    assert reasons(day)["Nvidia beats its own guidance"] == "spacing"
