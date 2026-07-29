import json

from src import runlog


def record(day="2026-07-28", ok=True, **kwargs):
    return runlog.RunRecord(
        started_at="2026-07-28T11:00:00+00:00",
        finished_at="2026-07-28T11:04:30+00:00",
        date=day, ok=ok, **kwargs,
    )


def test_a_run_is_appended_and_read_back(tmp_path):
    runlog.append(tmp_path, record(post_count=28, transcribed_count=28, topic_count=26))

    runs = runlog.load(tmp_path)
    assert len(runs) == 1
    assert runs[0]["date"] == "2026-07-28"
    assert runs[0]["ok"] is True
    assert runs[0]["topic_count"] == 26
    assert runs[0]["duration_seconds"] == 270.0


def test_runs_are_newest_first(tmp_path):
    runlog.append(tmp_path, record(day="2026-07-27"))
    runlog.append(tmp_path, record(day="2026-07-28"))

    assert [r["date"] for r in runlog.load(tmp_path)] == ["2026-07-28", "2026-07-27"]


def test_failure_notes_are_preserved(tmp_path):
    runlog.append(tmp_path, record(
        ok=True, incomplete=True,
        failures=["fetch total.hipocrisy: Profile does not exist."],
    ))

    run = runlog.load(tmp_path)[0]
    assert run["incomplete"] is True
    assert "total.hipocrisy" in run["failures"][0]


def test_a_hard_failure_records_the_error(tmp_path):
    runlog.append(tmp_path, record(ok=False, error="session expired"))

    run = runlog.load(tmp_path)[0]
    assert run["ok"] is False
    assert run["error"] == "session expired"


def test_history_is_capped(tmp_path):
    for i in range(10):
        runlog.append(tmp_path, record(day=f"2026-07-{i + 1:02d}"), keep=5)

    assert len(runlog.load(tmp_path)) == 5


def test_missing_history_is_empty_not_an_error(tmp_path):
    assert runlog.load(tmp_path) == []


def test_corrupt_history_degrades_rather_than_raising(tmp_path):
    """Diagnostics being unreadable must not take the page down."""
    (tmp_path / runlog.RUNS_FILE).write_text("{not json", encoding="utf-8")
    assert runlog.load(tmp_path) == []


def test_a_bookkeeping_failure_never_raises(tmp_path):
    # A directory where the file should be: writing cannot succeed.
    (tmp_path / runlog.RUNS_FILE).mkdir()
    runlog.append(tmp_path, record())
    assert runlog.load(tmp_path) == []


def test_no_temp_file_is_left_behind(tmp_path):
    runlog.append(tmp_path, record())
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_malformed_timestamp_gives_a_zero_duration(tmp_path):
    entry = runlog.RunRecord(
        started_at="not-a-time", finished_at="also-not", date="2026-07-28", ok=True,
    )
    assert entry.duration_seconds == 0.0


# --- log files -------------------------------------------------------------


def test_a_day_log_is_read_back(tmp_path):
    (tmp_path / "2026-07-28.log").write_text("line one\nline two\n", encoding="utf-8")
    assert runlog.read_log(tmp_path, "2026-07-28") == "line one\nline two\n"


def test_a_missing_log_is_empty(tmp_path):
    assert runlog.read_log(tmp_path, "2026-07-28") == ""


def test_a_large_log_is_truncated_from_the_front(tmp_path):
    """Whisper logs three lines per video, so a busy day runs to thousands of
    lines. The end is the part that explains how the run finished."""
    body = "".join(f"noise line {i}\n" for i in range(20_000))
    (tmp_path / "2026-07-28.log").write_text(body + "FINAL LINE\n", encoding="utf-8")

    text = runlog.read_log(tmp_path, "2026-07-28", tail_bytes=2_000)

    assert "FINAL LINE" in text
    assert "truncated" in text
    assert "noise line 0\n" not in text
    assert len(text) < 3_000


def test_a_small_log_is_returned_whole_without_a_truncation_notice(tmp_path):
    (tmp_path / "2026-07-28.log").write_text("short\n", encoding="utf-8")
    assert runlog.read_log(tmp_path, "2026-07-28", tail_bytes=1_000) == "short\n"


def test_a_date_object_is_accepted(tmp_path):
    from datetime import date

    (tmp_path / "2026-07-28.log").write_text("ok\n", encoding="utf-8")
    assert runlog.read_log(tmp_path, date(2026, 7, 28)) == "ok\n"
