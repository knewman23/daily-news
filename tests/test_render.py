from datetime import date, datetime, timezone

import pytest

from src import notes, render


GENERATED = datetime(2026, 7, 28, 11, 0, 4, tzinfo=timezone.utc)
DAY = date(2026, 7, 28)

TOPICS = [
    {
        "headline": "Senate passes the spending bill",
        "body": "The chamber cleared the measure after a weekend of negotiation.",
        "tags": ["politics"],
        "sources": ["aaronparnas", "total.hipocrisy"],
    },
    {
        "headline": "Nvidia earnings beat estimates",
        "body": "Revenue came in ahead of guidance.",
        "tags": ["markets", "tech"],
        "sources": ["carolinegleich"],
    },
]

STATS = {"post_count": 4, "transcribed_count": 4, "incomplete": False}


EXPECTED = """---
date: 2026-07-28
generated: 2026-07-28T11:00:04+00:00
tags: [politics, markets, tech]
sources: ["@aaronparnas", "@total.hipocrisy", "@carolinegleich"]
post_count: 4
transcribed_count: 4
incomplete: false
---

# July 28, 2026

## Senate passes the spending bill
tags: politics
sources: @aaronparnas, @total.hipocrisy

The chamber cleared the measure after a weekend of negotiation.

## Nvidia earnings beat estimates
tags: markets, tech
sources: @carolinegleich

Revenue came in ahead of guidance.

## My Notes
<!-- notes:start -->
<!-- notes:end -->
"""


def test_renders_the_documented_format_exactly():
    assert render.render_day(DAY, TOPICS, STATS, generated=GENERATED) == EXPECTED


def test_frontmatter_tags_are_the_deduped_union_in_first_seen_order():
    topics = [
        {"headline": "A", "body": "b", "tags": ["tech", "politics"], "sources": ["x"]},
        {"headline": "B", "body": "b", "tags": ["politics", "markets"], "sources": ["y"]},
    ]
    out = render.render_day(DAY, topics, STATS, generated=GENERATED)
    assert "tags: [tech, politics, markets]" in out


def test_frontmatter_sources_are_deduped_and_at_prefixed():
    topics = [
        {"headline": "A", "body": "b", "tags": ["tech"], "sources": ["@x", "y"]},
        {"headline": "B", "body": "b", "tags": ["tech"], "sources": ["x"]},
    ]
    out = render.render_day(DAY, topics, STATS, generated=GENERATED)
    assert 'sources: ["@x", "@y"]' in out


def test_multi_source_topic_lists_every_handle():
    topics = [{
        "headline": "Shared story",
        "body": "b",
        "tags": ["politics"],
        "sources": ["a", "b", "c"],
    }]
    out = render.render_day(DAY, topics, STATS, generated=GENERATED)
    assert "sources: @a, @b, @c" in out


def test_incomplete_flag_is_carried_through():
    out = render.render_day(
        DAY, TOPICS, {**STATS, "incomplete": True}, generated=GENERATED
    )
    assert "incomplete: true" in out


def test_zero_topics_still_produces_a_usable_file():
    out = render.render_day(DAY, [], {
        "post_count": 0, "transcribed_count": 0, "incomplete": False,
    }, generated=GENERATED)

    assert "tags: []" in out
    assert "sources: []" in out
    assert "post_count: 0" in out
    assert "No posts found" in out
    assert out.count(notes.START) == 1
    assert out.count(notes.END) == 1


def test_output_is_writable_by_notes_without_losing_the_summary(tmp_path):
    p = tmp_path / "2026-07-28.md"
    p.write_text(render.render_day(DAY, TOPICS, STATS, generated=GENERATED),
                 encoding="utf-8")

    notes.write_notes(p, "my reaction")

    text = p.read_text(encoding="utf-8")
    assert notes.read_notes(p) == "my reaction"
    assert "Senate passes the spending bill" in text
    assert "Revenue came in ahead of guidance." in text


def test_title_uses_no_zero_padded_day():
    out = render.render_day(date(2026, 8, 5), TOPICS, STATS, generated=GENERATED)
    assert "# August 5, 2026" in out


def test_tags_are_lowercased_and_headline_whitespace_collapsed():
    topics = [{
        "headline": "  Spread  across\nlines  ",
        "body": "  body text  ",
        "tags": ["Politics", "MARKETS"],
        "sources": ["A"],
    }]
    out = render.render_day(DAY, topics, STATS, generated=GENERATED)
    assert "## Spread across lines" in out
    assert "tags: politics, markets" in out
    assert "sources: @a" in out


def test_a_topic_with_no_tags_omits_the_tags_line():
    topics = [{"headline": "A", "body": "b", "tags": [], "sources": ["x"]}]
    out = render.render_day(DAY, topics, STATS, generated=GENERATED)
    assert "## A\nsources: @x\n" in out


@pytest.mark.parametrize("topic", [
    {"body": "b", "tags": [], "sources": []},
    {"headline": "", "body": "b", "tags": [], "sources": []},
    {"headline": "A", "tags": [], "sources": []},
])
def test_malformed_topic_raises(topic):
    with pytest.raises(ValueError):
        render.render_day(DAY, [topic], STATS, generated=GENERATED)


def test_generated_timestamp_must_be_aware():
    with pytest.raises(ValueError):
        render.render_day(DAY, TOPICS, STATS, generated=datetime(2026, 7, 28, 11, 0))


# --- source links ----------------------------------------------------------

TOPICS_WITH_POSTS = [{
    "headline": "Senate passes the spending bill",
    "body": "The chamber cleared the measure.",
    "tags": ["politics"],
    "sources": ["@aaronparnas", "@total.hypocrisy"],
    "posts": ["AAA", "BBB"],
}]

PERMALINKS = {
    "AAA": ("aaronparnas", "https://www.instagram.com/p/AAA/"),
    "BBB": ("total.hypocrisy", "https://www.instagram.com/p/BBB/"),
}


def test_the_handle_itself_becomes_the_link():
    """Nobody recognises a shortcode, so a line of bare post ids reads as noise."""
    out = render.render_day(
        DAY, TOPICS_WITH_POSTS, STATS, generated=GENERATED, permalinks=PERMALINKS,
    )
    assert ("sources: [@aaronparnas](https://www.instagram.com/p/AAA/), "
            "[@total.hypocrisy](https://www.instagram.com/p/BBB/)") in out
    assert "posts:" not in out


def test_an_invented_post_id_is_dropped_rather_than_linked():
    """A link to a post that does not exist is worse than no link: it looks
    checkable and is not."""
    topics = [{**TOPICS_WITH_POSTS[0], "posts": ["AAA", "MADE-UP"]}]
    out = render.render_day(DAY, topics, STATS, generated=GENERATED, permalinks=PERMALINKS)

    assert "MADE-UP" not in out
    assert "[@aaronparnas](https://www.instagram.com/p/AAA/)" in out


def test_duplicate_post_ids_are_listed_once():
    topics = [{**TOPICS_WITH_POSTS[0], "posts": ["AAA", "AAA"]}]
    out = render.render_day(DAY, topics, STATS, generated=GENERATED, permalinks=PERMALINKS)
    assert out.count("[@aaronparnas]") == 1


def test_a_handle_the_model_named_without_a_post_id_is_still_credited():
    topics = [{**TOPICS_WITH_POSTS[0], "posts": ["AAA"]}]
    out = render.render_day(DAY, topics, STATS, generated=GENERATED, permalinks=PERMALINKS)

    assert "[@aaronparnas](https://www.instagram.com/p/AAA/)" in out
    assert "@total.hypocrisy" in out          # named by the model, no id, still listed


def test_sources_are_plain_when_no_permalinks_are_supplied():
    out = render.render_day(DAY, TOPICS_WITH_POSTS, STATS, generated=GENERATED)
    assert "sources: @aaronparnas, @total.hypocrisy" in out
