from datetime import date

import pytest

from src import digest, notes


DAY_ONE = """---
date: 2026-07-27
generated: 2026-07-27T11:00:00+00:00
tags: [politics]
sources: ["@aaronparnas"]
post_count: 2
transcribed_count: 2
incomplete: false
---

# July 27, 2026

## Court hears the tariff case
tags: politics
sources: @aaronparnas

Oral arguments ran long on Monday morning.

## My Notes
<!-- notes:start -->
<!-- notes:end -->
"""

DAY_TWO = """---
date: 2026-07-28
generated: 2026-07-28T11:00:00+00:00
tags: [politics, markets]
sources: ["@aaronparnas", "@carolinegleich"]
post_count: 4
transcribed_count: 3
incomplete: true
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


@pytest.fixture
def news(tmp_path):
    (tmp_path / "2026-07-27.md").write_text(DAY_ONE, encoding="utf-8")
    (tmp_path / "2026-07-28.md").write_text(DAY_TWO, encoding="utf-8")
    return tmp_path


# --- list_days -------------------------------------------------------------


def test_list_days_is_newest_first(news):
    assert [d.date for d in digest.list_days(news)] == [
        date(2026, 7, 28), date(2026, 7, 27),
    ]


def test_list_days_carries_frontmatter(news):
    latest = digest.list_days(news)[0]
    assert latest.tags == ["politics", "markets"]
    assert latest.sources == ["@aaronparnas", "@carolinegleich"]
    assert latest.post_count == 4
    assert latest.transcribed_count == 3
    assert latest.incomplete is True


@pytest.mark.parametrize("name", [
    "README.md", "2026-7-28.md", "notes.txt", "2026-07-28.md.bak", "draft.md",
])
def test_list_days_ignores_files_that_are_not_dated_digests(news, name):
    (news / name).write_text("whatever\n", encoding="utf-8")
    assert len(digest.list_days(news)) == 2


def test_list_days_on_missing_directory_is_empty(tmp_path):
    assert digest.list_days(tmp_path / "absent") == []


# --- degrading on bad frontmatter -----------------------------------------


def test_missing_frontmatter_fields_fall_back_to_defaults(news):
    (news / "2026-07-26.md").write_text(
        "---\ndate: 2026-07-26\n---\n\n# July 26, 2026\n", encoding="utf-8"
    )
    day = [d for d in digest.list_days(news) if d.date == date(2026, 7, 26)][0]
    assert day.tags == []
    assert day.sources == []
    assert day.post_count == 0
    assert day.incomplete is False


def test_absent_frontmatter_still_lists_using_the_filename(news):
    (news / "2026-07-25.md").write_text("# July 25, 2026\n\nplain\n", encoding="utf-8")
    assert date(2026, 7, 25) in [d.date for d in digest.list_days(news)]


def test_malformed_yaml_degrades_rather_than_raising(news):
    (news / "2026-07-24.md").write_text(
        "---\ntags: [unclosed\n  ::: bad\n---\n\n# July 24, 2026\n", encoding="utf-8"
    )
    day = [d for d in digest.list_days(news) if d.date == date(2026, 7, 24)][0]
    assert day.tags == []


# --- topics_of -------------------------------------------------------------


def test_topics_of_finds_each_section(news):
    topics = digest.topics_of(news / "2026-07-28.md")
    assert [t.headline for t in topics] == [
        "Senate passes the spending bill", "Nvidia earnings beat estimates",
    ]
    assert topics[0].tags == ["politics"]
    assert topics[0].sources == ["@aaronparnas", "@total.hipocrisy"]
    assert topics[0].body == "The chamber cleared the measure after a weekend of negotiation."


def test_topics_of_excludes_the_notes_section(news):
    assert "My Notes" not in [t.headline for t in digest.topics_of(news / "2026-07-28.md")]


def test_a_heading_inside_a_note_does_not_become_a_topic(news):
    path = news / "2026-07-28.md"
    notes.write_notes(path, "## Fake topic\nthis is just how I feel")

    topics = digest.topics_of(path)
    assert len(topics) == 2
    assert "Fake topic" not in [t.headline for t in topics]


def test_notes_content_is_not_searchable_as_news(news):
    notes.write_notes(news / "2026-07-28.md", "zebra")
    assert digest.search(news, "zebra") == []


# --- render_html -----------------------------------------------------------


def test_render_html_renders_the_summary_without_the_notes_block(news):
    html = digest.render_html(news / "2026-07-28.md")
    assert "<h2>Senate passes the spending bill</h2>" in html
    assert "My Notes" not in html
    assert notes.START not in html


def test_render_html_omits_the_frontmatter(news):
    html = digest.render_html(news / "2026-07-28.md")
    assert "post_count" not in html


# --- search ----------------------------------------------------------------


def test_search_matches_body_case_insensitively(news):
    hits = digest.search(news, "SPENDING")
    assert [(h.date, h.headline) for h in hits] == [
        (date(2026, 7, 28), "Senate passes the spending bill"),
    ]


def test_search_matches_headline(news):
    assert [h.date for h in digest.search(news, "nvidia")] == [date(2026, 7, 28)]


def test_search_spans_days_and_is_newest_first(news):
    hits = digest.search(news, "the")
    assert [h.date for h in hits] == [date(2026, 7, 28), date(2026, 7, 27)]


def test_search_returns_every_matching_topic_on_a_day(news):
    hits = digest.search(news, "e", tag=None)
    assert len(hits) == 3
    assert [h.date for h in hits] == [
        date(2026, 7, 28), date(2026, 7, 28), date(2026, 7, 27),
    ]


@pytest.mark.parametrize("query", ["", "   ", None])
def test_empty_query_returns_nothing_rather_than_everything(news, query):
    assert digest.search(news, query) == []


def test_search_returns_no_hits_for_an_absent_word(news):
    assert digest.search(news, "kangaroo") == []


def test_tag_filter_uses_the_topic_tags_not_the_day_union(news):
    hits = digest.search(news, "", tag="markets")
    assert [h.headline for h in hits] == ["Nvidia earnings beat estimates"]


def test_tag_filter_combines_with_a_query(news):
    assert digest.search(news, "revenue", tag="markets")
    assert digest.search(news, "revenue", tag="politics") == []


def test_all_tags_lists_every_topic_tag_once(news):
    assert sorted(digest.all_tags(news)) == ["markets", "politics", "tech"]


# --- source links ----------------------------------------------------------

DAY_WITH_LINKS = """---
date: 2026-07-29
tags: [politics]
---

# July 29, 2026

## Senate passes the spending bill
tags: politics
sources: @aaronparnas, @total.hypocrisy
posts: [AAA](https://www.instagram.com/p/AAA/), [BBB](https://www.instagram.com/p/BBB/)

The chamber cleared the measure.

## My Notes
<!-- notes:start -->
<!-- notes:end -->
"""


def test_topic_links_are_parsed_back_out(news):
    (news / "2026-07-29.md").write_text(DAY_WITH_LINKS, encoding="utf-8")

    topic = digest.topics_of(news / "2026-07-29.md")[0]

    assert topic.links == [
        ("AAA", "https://www.instagram.com/p/AAA/"),
        ("BBB", "https://www.instagram.com/p/BBB/"),
    ]
    assert topic.sources == ["@aaronparnas", "@total.hypocrisy"]
    assert topic.body == "The chamber cleared the measure."


def test_the_posts_line_is_not_treated_as_body_text(news):
    (news / "2026-07-29.md").write_text(DAY_WITH_LINKS, encoding="utf-8")
    assert "instagram.com" not in digest.topics_of(news / "2026-07-29.md")[0].body


def test_links_render_as_anchors_in_html(news):
    (news / "2026-07-29.md").write_text(DAY_WITH_LINKS, encoding="utf-8")
    html = digest.render_html(news / "2026-07-29.md")
    assert 'href="https://www.instagram.com/p/AAA/"' in html


def test_a_topic_without_a_posts_line_has_no_links(news):
    assert digest.topics_of(news / "2026-07-28.md")[0].links == []
