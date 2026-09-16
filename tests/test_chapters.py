"""Classifying a day's topics into book threads.

The seam is `runner`: every test passes a fake subprocess, so nothing here
reaches `claude -p`. Outline and day files are written under `tmp_path`.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date

import pytest

from src import chapters
from src.digest import Topic


OUTLINE = {
    "version": 3,
    "revised_at": "2026-09-15",
    "parts": [
        {"id": "unauthorized-war", "title": "The War Nobody Authorized",
         "thread_ids": ["iran-authorization", "force-readiness"]},
    ],
    "threads": [
        {"id": "iran-authorization", "title": "Iran war authorization fight",
         "description": "Attempts to constrain hostilities", "created": "2026-09-15"},
        {"id": "force-readiness", "title": "Force readiness",
         "description": "Whether the force can sustain operations",
         "created": "2026-09-15"},
    ],
}


def topic(headline: str, sources=("@aaronparnas",), skipped: str = "") -> Topic:
    return Topic(headline=headline, tags=["politics"], sources=list(sources),
                 body="Body text.", links=[], skipped=skipped)


def book_dir(tmp_path, outline=None):
    book = tmp_path / "book"
    book.mkdir()
    (book / "days").mkdir()
    chapters.save_outline(book, chapters.outline_from_dict(outline or OUTLINE))
    return book


def fake_runner(payload):
    """A runner that answers with the CLI's real wrapper shape."""
    def run(command, input=None, capture_output=False, text=False):
        wrapper = {"is_error": False, "result": json.dumps(payload),
                   "type": "result"}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(wrapper),
                                           stderr="")
    return run


# --- outline ---------------------------------------------------------------


def test_load_outline_round_trips(tmp_path):
    book = book_dir(tmp_path)
    outline = chapters.load_outline(book)

    assert outline.version == 3
    assert [p.title for p in outline.parts] == ["The War Nobody Authorized"]
    assert outline.thread("iran-authorization").title == "Iran war authorization fight"


def test_malformed_outline_raises_rather_than_rebuilding(tmp_path):
    book = tmp_path / "book"
    (book / "days").mkdir(parents=True)
    (book / "outline.json").write_text("{not json", encoding="utf-8")

    # Silently rebuilding would discard a structure the user approved.
    with pytest.raises(chapters.OutlineError):
        chapters.load_outline(book)


def test_outline_missing_raises_with_a_usable_message(tmp_path):
    book = tmp_path / "book"
    book.mkdir()

    with pytest.raises(chapters.OutlineError) as caught:
        chapters.load_outline(book)

    assert "bootstrap" in str(caught.value)


# --- classification --------------------------------------------------------


def test_classify_files_each_topic_and_stamps_the_version(tmp_path):
    book = book_dir(tmp_path)
    topics = [topic("Massie files impeachment articles"),
              topic("Pilots flying once a month")]
    runner = fake_runner({"assignments": [
        {"headline": "Massie files impeachment articles",
         "thread_id": "iran-authorization"},
        {"headline": "Pilots flying once a month", "thread_id": "force-readiness"},
    ]})

    result = chapters.classify_day(book, date(2026, 9, 15), topics, runner=runner)

    assert result["outline_version"] == 3
    assert result["unfiled"] == []
    assert {a["headline"]: a["thread_id"] for a in result["assignments"]} == {
        "Massie files impeachment articles": "iran-authorization",
        "Pilots flying once a month": "force-readiness",
    }

    on_disk = json.loads((book / "days" / "2026-09-15.json").read_text())
    assert on_disk == result


def test_a_thread_id_the_outline_does_not_have_becomes_unfiled(tmp_path):
    """The model's word is not taken for the thread list.

    An invented id would otherwise land in a day file and produce a thread that
    exists only in assignments, which the review page could not resolve.
    """
    book = book_dir(tmp_path)
    runner = fake_runner({"assignments": [
        {"headline": "Massie files impeachment articles", "thread_id": "invented"},
    ]})

    result = chapters.classify_day(
        book, date(2026, 9, 15), [topic("Massie files impeachment articles")],
        runner=runner,
    )

    assert result["assignments"] == []
    assert result["unfiled"][0]["headline"] == "Massie files impeachment articles"
    assert "invented" in result["unfiled"][0]["why"]


def test_a_topic_the_model_ignored_becomes_unfiled(tmp_path):
    book = book_dir(tmp_path)
    runner = fake_runner({"assignments": []})

    result = chapters.classify_day(
        book, date(2026, 9, 15), [topic("Some topic nobody filed")], runner=runner,
    )

    assert [u["headline"] for u in result["unfiled"]] == ["Some topic nobody filed"]


def test_model_may_decline_with_a_reason(tmp_path):
    book = book_dir(tmp_path)
    runner = fake_runner({"assignments": [
        {"headline": "Yosemite land exchange", "thread_id": None,
         "why": "no thread covers public lands"},
    ]})

    result = chapters.classify_day(
        book, date(2026, 9, 15), [topic("Yosemite land exchange")], runner=runner,
    )

    assert result["unfiled"][0]["why"] == "no thread covers public lands"


def test_skipped_topics_are_not_classified(tmp_path):
    """Off topic for the digest is off topic for the book."""
    book = book_dir(tmp_path)
    sent = {}

    def capturing(command, input=None, **kwargs):
        sent["prompt"] = input
        wrapper = {"is_error": False, "result": json.dumps({"assignments": []})}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(wrapper))

    topics = [topic("Kept story"), topic("Dropped story", skipped="off topic")]
    result = chapters.classify_day(book, date(2026, 9, 15), topics, runner=capturing)

    assert "Dropped story" not in sent["prompt"]
    assert [u["headline"] for u in result["unfiled"]] == ["Kept story"]


def test_no_topics_makes_no_call_at_all(tmp_path):
    book = book_dir(tmp_path)

    def explode(*args, **kwargs):
        raise AssertionError("claude was called for a day with nothing in it")

    result = chapters.classify_day(book, date(2026, 9, 15), [], runner=explode)

    assert result["assignments"] == [] and result["unfiled"] == []


def test_a_model_failure_raises_chapters_error(tmp_path):
    """Raising is right here; run_daily is what decides it is not fatal."""
    book = book_dir(tmp_path)

    def failing(command, input=None, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="boom")

    with pytest.raises(chapters.ChaptersError):
        chapters.classify_day(book, date(2026, 9, 15), [topic("A story")],
                              runner=failing)


def test_a_model_level_error_is_caught_despite_exit_zero(tmp_path):
    book = book_dir(tmp_path)

    def failing(command, input=None, **kwargs):
        wrapper = {"is_error": True, "result": "overloaded"}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(wrapper))

    with pytest.raises(chapters.ChaptersError):
        chapters.classify_day(book, date(2026, 9, 15), [topic("A story")],
                              runner=failing)


# --- bookkeeping -----------------------------------------------------------


def test_is_classified_tracks_the_day_file(tmp_path):
    book = book_dir(tmp_path)
    day = date(2026, 9, 15)

    assert not chapters.is_classified(book, day)
    chapters.classify_day(book, day, [], runner=fake_runner({"assignments": []}))
    assert chapters.is_classified(book, day)


def test_stale_days_are_the_ones_filed_under_an_older_version(tmp_path):
    book = book_dir(tmp_path)
    chapters.classify_day(book, date(2026, 9, 15), [],
                          runner=fake_runner({"assignments": []}))

    bumped = chapters.outline_from_dict({**OUTLINE, "version": 4})
    chapters.save_outline(book, bumped)

    assert chapters.stale_days(book) == [date(2026, 9, 15)]


def test_moving_a_thread_between_parts_leaves_no_day_stale(tmp_path):
    """A part-only edit must not trigger a re-file.

    Threads are the classification target; parts are a layer above them. Marking
    days stale for a change that cannot alter an assignment would cost one model
    call per day to recompute what is already on disk.
    """
    book = book_dir(tmp_path)
    chapters.classify_day(book, date(2026, 9, 15), [],
                          runner=fake_runner({"assignments": []}))

    moved = {
        **OUTLINE,
        "revised_at": "2026-09-16",
        "parts": [
            {"id": "unauthorized-war", "title": "The War Nobody Authorized",
             "thread_ids": ["iran-authorization"]},
            {"id": "readiness", "title": "Readiness",
             "thread_ids": ["force-readiness"]},
        ],
    }
    chapters.save_outline(book, chapters.outline_from_dict(moved))

    assert chapters.stale_days(book) == []
