import json
import urllib.error
import urllib.request
from datetime import date

import pytest

from src import digest, notes, serve, sources


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
sources: @aaronparnas

Oral arguments ran long on Monday morning.

## My Notes
<!-- notes:start -->
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
sources: @aaronparnas

The chamber cleared the measure after a weekend of negotiation.

## Nvidia earnings beat estimates
tags: markets
sources: @carolinegleich

Revenue came in ahead of guidance.

## My Notes
<!-- notes:start -->
<!-- notes:end -->
"""

SOURCES = {
    "version": 2,
    "sources": [
        {"handle": "aaronparnas", "enabled": True, "added": "2026-07-01",
         "last_pull_at": "2026-07-28T11:00:00+00:00", "last_seen": "2026-07-28"},
        {"handle": "carolinegleich", "enabled": False, "added": "2026-07-01",
         "last_pull_at": None, "last_seen": None},
    ],
}


@pytest.fixture
def server(tmp_path):
    news = tmp_path / "news"
    news.mkdir()
    (news / "2026-07-27.md").write_text(DAY_ONE, encoding="utf-8")
    (news / "2026-07-28.md").write_text(DAY_TWO, encoding="utf-8")

    sources_path = tmp_path / "sources.json"
    sources_path.write_text(json.dumps(SOURCES, indent=2), encoding="utf-8")

    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<h1>Daily News</h1>", encoding="utf-8")
    (web / "header.PNG").write_bytes(b"\x89PNG fake")

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "2026-07-28.log").write_text(
        "2026-07-28 11:00:00 INFO starting\n"
        "2026-07-28 11:04:00 WARNING partial failure: fetch total.hipocrisy\n",
        encoding="utf-8",
    )
    from src import runlog
    runlog.append(logs, runlog.RunRecord(
        started_at="2026-07-28T11:00:00+00:00", finished_at="2026-07-28T11:04:30+00:00",
        date="2026-07-28", ok=True, post_count=28, transcribed_count=28,
        topic_count=26, incomplete=True,
        failures=["fetch total.hipocrisy: Profile does not exist."],
    ))

    httpd = serve.build_server(
        news_dir=news, sources_path=sources_path, web_dir=web,
        host="127.0.0.1", port=0, lookup=lambda handle: None, logs_dir=logs,
    )
    thread = serve.serve_in_thread(httpd)
    try:
        yield Client(httpd, news, sources_path)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class Client:
    def __init__(self, httpd, news, sources_path):
        self.base = f"http://127.0.0.1:{httpd.server_address[1]}"
        self.news = news
        self.sources_path = sources_path

    def request(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                raw = response.read()
                return response.status, raw
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def json(self, path, method="GET", body=None):
        status, raw = self.request(path, method, body)
        return status, (json.loads(raw) if raw else None)


# --- days ------------------------------------------------------------------


def test_days_index_is_newest_first(server):
    status, payload = server.json("/api/days")

    assert status == 200
    assert [d["date"] for d in payload["days"]] == ["2026-07-28", "2026-07-27"]
    assert payload["days"][0]["tags"] == ["politics", "markets"]
    assert payload["days"][0]["post_count"] == 4
    assert payload["days"][0]["incomplete"] is True


def test_a_day_returns_rendered_html_and_its_notes(server):
    status, payload = server.json("/api/day/2026-07-28")

    assert status == 200
    assert "<h2>Senate passes the spending bill</h2>" in payload["html"]
    assert payload["notes"] == ""
    assert payload["date"] == "2026-07-28"
    assert payload["incomplete"] is True
    assert "My Notes" not in payload["html"]


def test_an_unknown_day_is_404(server):
    status, _ = server.request("/api/day/2020-01-01")
    assert status == 404


@pytest.mark.parametrize("path", [
    "/api/day/2026-7-28",
    "/api/day/not-a-date",
    "/api/day/../../etc/passwd",
    "/api/day/..%2f..%2fetc%2fpasswd",
    "/api/day/2026-07-28.md",
])
def test_a_malformed_or_traversing_date_is_rejected(server, path):
    status, _ = server.request(path)
    assert status in (400, 404)


def test_traversal_cannot_read_a_file_outside_the_news_directory(server, tmp_path):
    (tmp_path / "secret.md").write_text("do not serve me", encoding="utf-8")

    for attempt in ("/api/day/../secret", "/api/day/%2e%2e%2fsecret"):
        status, raw = server.request(attempt)
        assert status in (400, 404)
        assert b"do not serve me" not in raw


# --- search ----------------------------------------------------------------


def test_search_matches_across_days(server):
    status, payload = server.json("/api/search?q=spending")

    assert status == 200
    assert [h["headline"] for h in payload["hits"]] == ["Senate passes the spending bill"]
    assert payload["hits"][0]["date"] == "2026-07-28"


def test_an_empty_query_returns_no_hits(server):
    status, payload = server.json("/api/search?q=")
    assert status == 200
    assert payload["hits"] == []


def test_search_can_filter_by_tag(server):
    _, payload = server.json("/api/search?tag=markets")
    assert [h["headline"] for h in payload["hits"]] == ["Nvidia earnings beat estimates"]


def test_the_tag_list_is_exposed_for_the_chips(server):
    _, payload = server.json("/api/tags")
    assert sorted(payload["tags"]) == ["markets", "politics"]


# --- notes -----------------------------------------------------------------


def test_saving_notes_persists_them_and_leaves_the_summary_intact(server):
    path = server.news / "2026-07-28.md"
    before = path.read_text(encoding="utf-8")

    status, _ = server.json("/api/notes/2026-07-28", "POST", {"notes": "This worries me."})

    assert status == 200
    assert notes.read_notes(path) == "This worries me."

    after = path.read_text(encoding="utf-8")
    assert after.split(notes.START)[0] == before.split(notes.START)[0]
    assert digest.topics_of(path)[0].headline == "Senate passes the spending bill"


def test_saved_notes_come_back_on_the_next_read(server):
    server.json("/api/notes/2026-07-28", "POST", {"notes": "a note"})
    _, payload = server.json("/api/day/2026-07-28")
    assert payload["notes"] == "a note"


def test_notes_can_be_cleared(server):
    server.json("/api/notes/2026-07-28", "POST", {"notes": "temporary"})
    server.json("/api/notes/2026-07-28", "POST", {"notes": ""})
    _, payload = server.json("/api/day/2026-07-28")
    assert payload["notes"] == ""


def test_a_broken_marker_block_is_409_not_a_silent_append(server):
    path = server.news / "2026-07-28.md"
    path.write_text(path.read_text(encoding="utf-8").replace(notes.END, ""), encoding="utf-8")

    status, _ = server.request("/api/notes/2026-07-28", "POST", {"notes": "hello"})
    assert status == 409


def test_notes_may_not_forge_the_markers(server):
    status, _ = server.request(
        "/api/notes/2026-07-28", "POST", {"notes": f"sneaky {notes.START}"},
    )
    assert status == 409


def test_saving_notes_for_an_unknown_day_is_404(server):
    status, _ = server.request("/api/notes/2020-01-01", "POST", {"notes": "x"})
    assert status == 404


def test_a_malformed_notes_body_is_400(server):
    status, raw = server.request("/api/notes/2026-07-28", "POST")
    assert status == 400


# --- sources ---------------------------------------------------------------


def test_sources_are_listed_with_their_state(server):
    status, payload = server.json("/api/sources")

    assert status == 200
    by_handle = {s["handle"]: s for s in payload["sources"]}
    assert by_handle["aaronparnas"]["enabled"] is True
    assert by_handle["aaronparnas"]["last_seen"] == "2026-07-28"
    assert by_handle["carolinegleich"]["enabled"] is False


def test_adding_a_source(server):
    status, payload = server.json("/api/sources", "POST", {"handle": "@NewHandle"})

    assert status == 200
    assert payload["source"]["handle"] == "newhandle"
    assert "newhandle" in [s.handle for s in sources.load(server.sources_path)]


def test_adding_a_duplicate_is_409_with_a_reason(server):
    status, payload = server.json("/api/sources", "POST", {"handle": "aaronparnas"})

    assert status == 409
    assert "already" in payload["error"].lower()


def test_an_invalid_handle_is_400_with_a_reason(server):
    status, payload = server.json("/api/sources", "POST", {"handle": "two words"})

    assert status == 400
    assert payload["error"]


def test_an_unreachable_handle_is_400_and_is_not_added(server, tmp_path):
    """The lookup failure reason must reach the UI, not just the log."""
    def failing_lookup(handle):
        raise RuntimeError("profile not found")

    httpd = serve.build_server(
        news_dir=server.news, sources_path=server.sources_path,
        web_dir=tmp_path / "web", host="127.0.0.1", port=0, lookup=failing_lookup,
    )
    thread = serve.serve_in_thread(httpd)
    try:
        client = Client(httpd, server.news, server.sources_path)
        status, payload = client.json("/api/sources", "POST", {"handle": "ghostaccount"})
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    assert status == 400
    assert "profile not found" in payload["error"]
    assert "ghostaccount" not in [s.handle for s in sources.load(server.sources_path)]


def test_toggling_a_source(server):
    status, _ = server.json("/api/sources/aaronparnas", "PATCH", {"enabled": False})

    assert status == 200
    assert [s.enabled for s in sources.load(server.sources_path)
            if s.handle == "aaronparnas"] == [False]


def test_deleting_a_source(server):
    status, _ = server.json("/api/sources/carolinegleich", "DELETE")

    assert status == 200
    assert "carolinegleich" not in [s.handle for s in sources.load(server.sources_path)]


def test_toggling_an_unknown_source_is_404(server):
    status, _ = server.request("/api/sources/nobody", "PATCH", {"enabled": True})
    assert status == 404


def test_deleting_an_unknown_source_is_404(server):
    status, _ = server.request("/api/sources/nobody", "DELETE")
    assert status == 404


# --- static files and binding ---------------------------------------------


def test_the_index_page_is_served_at_the_root(server):
    status, raw = server.request("/")
    assert status == 200
    assert b"Daily News" in raw


def test_the_header_image_is_served(server):
    status, raw = server.request("/header.PNG")
    assert status == 200
    assert raw.startswith(b"\x89PNG")


def test_the_server_binds_loopback_only(server):
    assert server.base.startswith("http://127.0.0.1:")


def test_an_unknown_path_is_404(server):
    status, _ = server.request("/nope.html")
    assert status == 404


# --- runs ------------------------------------------------------------------


def test_run_history_is_exposed(server):
    status, payload = server.json("/api/runs")

    assert status == 200
    assert len(payload["runs"]) == 1
    run = payload["runs"][0]
    assert run["date"] == "2026-07-28"
    assert run["topic_count"] == 26
    assert run["duration_seconds"] == 270.0


def test_the_problem_count_lets_the_ui_badge_the_panel(server):
    """An incomplete run counts as a problem even though it exited zero."""
    _, payload = server.json("/api/runs")
    assert payload["problems"] == 1


def test_failure_notes_reach_the_ui(server):
    _, payload = server.json("/api/runs")
    assert "total.hipocrisy" in payload["runs"][0]["failures"][0]


def test_a_run_log_is_readable(server):
    status, payload = server.json("/api/log/2026-07-28")

    assert status == 200
    assert "partial failure" in payload["log"]


def test_a_missing_run_log_is_404(server):
    status, _ = server.request("/api/log/2020-01-01")
    assert status == 404


@pytest.mark.parametrize("path", [
    "/api/log/not-a-date",
    "/api/log/../../etc/passwd",
    "/api/log/..%2f..%2fetc%2fpasswd",
])
def test_a_traversing_log_path_is_rejected(server, path):
    status, raw = server.request(path)
    assert status in (400, 404)
    assert b"root:" not in raw
