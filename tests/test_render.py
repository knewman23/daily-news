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


def test_a_handle_that_covered_a_story_twice_is_listed_once():
    """Two posts from one account rendered as "@handle, @handle", which reads
    as a mistake."""
    permalinks = {
        "AAA": ("aaronparnas", "https://www.instagram.com/p/AAA/"),
        "CCC": ("aaronparnas", "https://www.instagram.com/p/CCC/"),
        "BBB": ("total.hypocrisy", "https://www.instagram.com/p/BBB/"),
    }
    topics = [{
        "headline": "Covered twice by one account",
        "body": "b",
        "tags": ["politics"],
        "sources": ["@aaronparnas", "@total.hypocrisy"],
        "posts": ["AAA", "CCC", "BBB"],
    }]

    out = render.render_day(DAY, topics, STATS, generated=GENERATED,
                            permalinks=permalinks)

    assert out.count("@aaronparnas") == 2      # once in frontmatter, once in the topic
    # The first post it was drawn from is the one linked.
    assert "[@aaronparnas](https://www.instagram.com/p/AAA/)" in out
    assert "CCC" not in out


# --- skipped topics --------------------------------------------------------


SKIPPED = [{
    "headline": "My trip to Moab",
    "body": "A weekend of climbing outside town.",
    "tags": ["travel"],
    # Deliberately a handle no kept topic uses, so the frontmatter test below is
    # actually about the skipped topic rather than about a shared source.
    "sources": ["moabclimber"],
    "reason": "personal vlog",
}]


def test_skipped_topics_are_written_after_the_kept_ones():
    out = render.render_day(DAY, TOPICS, STATS, generated=GENERATED, skipped=SKIPPED)
    assert out.index("My trip to Moab") > out.index("Nvidia earnings beat estimates")


def test_a_skipped_topic_carries_its_reason_and_its_body():
    out = render.render_day(DAY, TOPICS, STATS, generated=GENERATED, skipped=SKIPPED)
    section = out.split("## My trip to Moab\n")[1]
    assert section.splitlines()[:4] == [
        "tags: travel",
        "sources: @moabclimber",
        "skipped: personal vlog",
        "",
    ]
    assert "A weekend of climbing outside town." in section


def test_the_skipped_line_comes_after_the_attribution():
    """Presentational only — digest parses the meta run unordered — but the file
    is read by humans, so: what it is, who covered it, why it was dropped."""
    out = render.render_day(DAY, TOPICS, STATS, generated=GENERATED, skipped=SKIPPED)
    section = out.split("## My trip to Moab\n")[1]
    assert section.index("sources:") < section.index("skipped:")


def test_a_skipped_topic_stays_out_of_the_frontmatter():
    out = render.render_day(DAY, TOPICS, STATS, generated=GENERATED, skipped=SKIPPED)
    header = out.split("---")[1]
    assert "travel" not in header
    assert "moabclimber" not in header


def test_a_reason_is_collapsed_to_one_line():
    """A newline would end the meta line and turn its tail into body text."""
    out = render.render_day(DAY, TOPICS, STATS, generated=GENERATED, skipped=[
        {**SKIPPED[0], "reason": "personal\nvlog\nnot news"},
    ])
    assert "skipped: personal vlog not news" in out


def test_a_skip_with_no_reason_still_says_something():
    out = render.render_day(DAY, TOPICS, STATS, generated=GENERATED, skipped=[
        {**SKIPPED[0], "reason": ""},
    ])
    assert "skipped: off topic" in out


def test_a_skipped_topic_with_no_body_gets_a_placeholder():
    out = render.render_day(DAY, TOPICS, STATS, generated=GENERATED, skipped=[
        {"headline": "Thin one", "reason": "off topic"},
    ])
    assert render.NO_BODY in out


def test_a_kept_topic_with_no_body_is_still_an_error():
    """Tolerated for a skip explanation, never for the product."""
    with pytest.raises(ValueError, match="no body"):
        render.render_day(DAY, [{"headline": "Empty", "body": "  "}], STATS,
                          generated=GENERATED)


def test_a_day_of_nothing_but_skips_is_not_reported_as_empty():
    out = render.render_day(DAY, [], STATS, generated=GENERATED, skipped=SKIPPED)
    assert render.EMPTY_BODY not in out
    assert "My trip to Moab" in out
