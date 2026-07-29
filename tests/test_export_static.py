import json

import pytest

import export_static
from src import notes


DAY_ONE = """---
date: 2026-07-27
tags: [politics]
sources: ["@aaronparnas"]
post_count: 2
transcribed_count: 2
incomplete: false
---

# July 27, 2026

## Court hears the tariff case
tags: politics
sources: [@aaronparnas](https://www.instagram.com/p/AAA/)

Oral arguments ran long on Monday morning.

## My Notes
<!-- notes:start -->
this is private
<!-- notes:end -->
"""

DAY_TWO = """---
date: 2026-07-28
tags: [politics, markets]
sources: ["@aaronparnas", "@carolinegleich"]
post_count: 4
transcribed_count: 3
incomplete: true
---

# July 28, 2026

## Senate passes the spending bill
tags: politics
sources: [@aaronparnas](https://www.instagram.com/p/BBB/)

The chamber cleared the measure after a weekend of negotiation.

## Nvidia earnings beat estimates
tags: markets
sources: @carolinegleich

Revenue came in ahead of guidance.

## My Notes
<!-- notes:start -->
<!-- notes:end -->
"""


@pytest.fixture
def project(tmp_path):
    news = tmp_path / "news"
    news.mkdir()
    (news / "2026-07-27.md").write_text(DAY_ONE, encoding="utf-8")
    (news / "2026-07-28.md").write_text(DAY_TWO, encoding="utf-8")

    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text(
        '<head>\n'
        '<link rel="icon" href="header.PNG">\n'
        '<link rel="stylesheet" href="style.css">\n'
        "</head>\n"
        '<body data-mode="live">\n'
        '<img class="crest" src="header.PNG" alt="crest">\n'
        '<details class="panel" id="sources-panel" data-live-only open>x</details>\n'
        '<details class="panel" id="runs-panel" data-live-only open>y</details>\n'
        '<script src="app.js"></script>\n'
        "</body>\n",
        encoding="utf-8",
    )
    (web / "style.css").write_text("body { color: red }", encoding="utf-8")
    (web / "app.js").write_text("console.log('hi')", encoding="utf-8")
    (web / "header.PNG").write_bytes(b"\x89PNG fake")

    return tmp_path


def export(project, **kwargs):
    out = kwargs.pop("out", project / "site")
    export_static.export(project / "news", project / "web", out, **kwargs)
    return out


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


# --- day data --------------------------------------------------------------


def test_every_day_becomes_a_json_file(project):
    site = export(project)

    assert (site / "data" / "day" / "2026-07-27.json").is_file()
    assert (site / "data" / "day" / "2026-07-28.json").is_file()


def test_a_day_matches_the_shape_the_live_api_returns(project):
    site = export(project)
    day = read(site / "data" / "day" / "2026-07-28.json")

    assert day["date"] == "2026-07-28"
    assert "<h2>Senate passes the spending bill</h2>" in day["html"]
    assert day["post_count"] == 4
    assert day["transcribed_count"] == 3
    assert day["incomplete"] is True
    assert day["tags"] == ["politics", "markets"]


def test_source_links_survive_into_the_exported_html(project):
    site = export(project)
    day = read(site / "data" / "day" / "2026-07-28.json")
    assert 'href="https://www.instagram.com/p/BBB/"' in day["html"]


def test_the_days_index_is_newest_first(project):
    site = export(project)
    assert [d["date"] for d in read(site / "data" / "days.json")["days"]] == [
        "2026-07-28", "2026-07-27",
    ]


def test_tags_are_exported_for_the_chips(project):
    site = export(project)
    assert sorted(read(site / "data" / "tags.json")["tags"]) == ["markets", "politics"]


# --- notes must not be published ------------------------------------------


def test_journal_notes_are_never_exported(project):
    """The whole reason only site/ is published. Notes are private."""
    site = export(project)

    day = read(site / "data" / "day" / "2026-07-27.json")
    assert "this is private" not in json.dumps(day)
    assert "notes" not in day

    everything = "".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in site.rglob("*") if p.is_file()
    )
    assert "this is private" not in everything


def test_no_markdown_source_is_copied_into_the_export(project):
    site = export(project)
    assert list(site.rglob("*.md")) == []


# --- search index ----------------------------------------------------------


def test_the_search_index_covers_every_topic(project):
    site = export(project)
    topics = read(site / "data" / "search.json")["topics"]

    assert len(topics) == 3
    assert {t["headline"] for t in topics} == {
        "Court hears the tariff case",
        "Senate passes the spending bill",
        "Nvidia earnings beat estimates",
    }


def test_search_entries_carry_what_the_browser_filters_on(project):
    site = export(project)
    entry = next(t for t in read(site / "data" / "search.json")["topics"]
                 if t["headline"] == "Nvidia earnings beat estimates")

    assert entry["date"] == "2026-07-28"
    assert entry["tags"] == ["markets"]
    assert entry["snippet"]
    assert entry["text"] == entry["text"].lower()      # pre-lowered for matching
    assert "revenue" in entry["text"]
    assert "nvidia" in entry["text"]                   # headline is searchable too


def test_the_search_index_is_newest_first(project):
    site = export(project)
    dates = [t["date"] for t in read(site / "data" / "search.json")["topics"]]
    assert dates == sorted(dates, reverse=True)


def test_notes_are_not_searchable_in_the_export(project):
    site = export(project)
    assert "private" not in json.dumps(read(site / "data" / "search.json"))


# --- assets and markup -----------------------------------------------------


@pytest.mark.parametrize("name", ["index.html", "style.css", "app.js", "header.PNG"])
def test_assets_are_copied(project, name):
    site = export(project)
    assert (site / name).is_file()


def test_the_exported_page_is_marked_static(project):
    site = export(project)
    assert 'data-mode="static"' in (site / "index.html").read_text(encoding="utf-8")
    assert 'data-mode="live"' not in (site / "index.html").read_text(encoding="utf-8")


def test_live_only_panels_are_stripped_from_the_markup(project):
    """Removed, not hidden: a display:none form is still a working form."""
    html = (export(project) / "index.html").read_text(encoding="utf-8")

    assert "sources-panel" not in html
    assert "runs-panel" not in html
    assert "data-live-only" not in html


def test_nojekyll_is_written(project):
    """Without it GitHub Pages drops any path starting with an underscore."""
    assert (export(project) / ".nojekyll").is_file()


# --- re-export safety ------------------------------------------------------


def test_re_export_is_idempotent(project):
    first = export(project)
    listing = sorted(p.relative_to(first).as_posix() for p in first.rglob("*"))

    export(project)
    assert sorted(p.relative_to(first).as_posix() for p in first.rglob("*")) == listing


def test_a_deleted_day_disappears_from_the_export(project):
    site = export(project)
    assert (site / "data" / "day" / "2026-07-27.json").is_file()

    (project / "news" / "2026-07-27.md").unlink()
    export(project)

    assert not (site / "data" / "day" / "2026-07-27.json").exists()
    assert [d["date"] for d in read(site / "data" / "days.json")["days"]] == ["2026-07-28"]


def test_a_marker_file_identifies_the_export(project):
    site = export(project)
    assert (site / export_static.MARKER).is_file()


def test_it_refuses_to_clean_a_directory_it_did_not_create(project):
    """The target is wiped on every export, so pointing it at the wrong
    directory must not delete someone's work."""
    victim = project / "important"
    victim.mkdir()
    (victim / "thesis.txt").write_text("years of work", encoding="utf-8")

    with pytest.raises(export_static.UnsafeTarget):
        export(project, out=victim)

    assert (victim / "thesis.txt").read_text(encoding="utf-8") == "years of work"


def test_an_empty_directory_is_an_acceptable_target(project):
    empty = project / "fresh"
    empty.mkdir()
    export(project, out=empty)
    assert (empty / "index.html").is_file()


def test_no_digests_still_produces_a_usable_site(tmp_path, project):
    for md in (project / "news").glob("*.md"):
        md.unlink()

    site = export(project)

    assert (site / "index.html").is_file()
    assert read(site / "data" / "days.json")["days"] == []
    assert read(site / "data" / "search.json")["topics"] == []


# --- cli -------------------------------------------------------------------


def test_the_cli_reports_what_it_wrote(project, capsys):
    export_static.main([
        "--news", str(project / "news"),
        "--web", str(project / "web"),
        "--out", str(project / "site"),
    ])
    assert "2 day" in capsys.readouterr().out


# --- cache busting ---------------------------------------------------------


def test_asset_urls_carry_a_content_hash(project):
    """GitHub Pages serves with max-age=600, so a fresh deploy is invisible for
    ten minutes unless the URL changes."""
    import re

    html = (export(project) / "index.html").read_text(encoding="utf-8")

    assert re.search(r'"style\.css\?v=[0-9a-f]{10}"', html)
    assert re.search(r'"app\.js\?v=[0-9a-f]{10}"', html)
    assert re.search(r'"header\.PNG\?v=[0-9a-f]{10}"', html)


def test_the_hash_changes_when_the_asset_changes(project):
    import re

    def stamp(html):
        return re.search(r'"app\.js\?v=([0-9a-f]{10})"', html).group(1)

    first = stamp((export(project) / "index.html").read_text(encoding="utf-8"))
    (project / "web" / "app.js").write_text("console.log('changed')", encoding="utf-8")
    second = stamp((export(project) / "index.html").read_text(encoding="utf-8"))

    assert first != second


def test_the_hash_is_stable_when_nothing_changes(project):
    import re

    def stamp(html):
        return re.search(r'"style\.css\?v=([0-9a-f]{10})"', html).group(1)

    first = stamp((export(project) / "index.html").read_text(encoding="utf-8"))
    second = stamp((export(project) / "index.html").read_text(encoding="utf-8"))
    assert first == second


def test_the_assets_themselves_keep_their_plain_names(project):
    """Only the URLs are fingerprinted; the files must stay where Pages looks."""
    site = export(project)
    assert (site / "app.js").is_file()
    assert (site / "style.css").is_file()


# --- skipped topics in the published build --------------------------------


def with_runs(project, skipped, date="2026-07-28"):
    from src import runlog
    logs = project / "logs"
    logs.mkdir(exist_ok=True)
    runlog.append(logs, runlog.RunRecord(
        started_at="2026-07-28T11:00:00+00:00",
        finished_at="2026-07-28T11:04:30+00:00",
        date=date, ok=True, topic_count=2, skipped=skipped,
    ))
    return logs


def export_with_logs(project, logs):
    export_static.export(project / "news", project / "web", project / "site", logs)
    return project / "site"


def test_skipped_topics_reach_the_published_day(project):
    """Published because a filter nobody can see is indistinguishable from one
    throwing away news — the reasons are about the news, not the reader."""
    logs = with_runs(project, ["My trip to Moab: personal vlog"])
    site = export_with_logs(project, logs)

    assert read(site / "data" / "day" / "2026-07-28.json")["skipped"] == [
        "My trip to Moab: personal vlog",
    ]


def test_the_published_index_carries_skipped_and_last_updated(project):
    logs = with_runs(project, ["Something: off topic"])
    site = export_with_logs(project, logs)

    payload = read(site / "data" / "days.json")
    assert payload["last_updated"] == "2026-07-28T11:04:30+00:00"
    by_date = {d["date"]: d for d in payload["days"]}
    assert by_date["2026-07-28"]["skipped"] == ["Something: off topic"]
    assert by_date["2026-07-27"]["skipped"] == []


def test_no_run_history_still_exports_cleanly(project):
    site = export(project)
    assert read(site / "data" / "days.json")["last_updated"] == ""
    assert read(site / "data" / "day" / "2026-07-28.json")["skipped"] == []
