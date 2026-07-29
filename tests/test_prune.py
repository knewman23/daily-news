import json
from datetime import date

import pytest

from src import prune


TODAY = date(2026, 7, 29)


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "news").mkdir()
    (tmp_path / "data" / "raw").mkdir(parents=True)
    (tmp_path / "data" / "transcripts").mkdir(parents=True)
    return tmp_path


def day(tree, stamp, *, digest=True):
    raw = tree / "data" / "raw" / stamp
    raw.mkdir(parents=True, exist_ok=True)
    (tree / "data" / "transcripts" / stamp).mkdir(parents=True, exist_ok=True)
    if digest:
        (tree / "news" / f"{stamp}.md").write_text("# a digest\n", encoding="utf-8")
    return raw


def video(tree, stamp, handle="aaronparnas", code="AAA", *, transcript=True, size=1024):
    raw = day(tree, stamp)
    stem = f"{handle}_{code}"
    (raw / f"{stem}.mp4").write_bytes(b"x" * size)
    (raw / f"{stem}.json").write_text(json.dumps({
        "handle": handle, "shortcode": code, "kind": "video", "caption": "c",
    }), encoding="utf-8")
    if transcript:
        (tree / "data" / "transcripts" / stamp / f"{stem}.txt").write_text(
            "words words words", encoding="utf-8")
    return stem


def carousel(tree, stamp, handle="oafnation_actual", code="CAR", slides=3, *, transcript=True):
    raw = day(tree, stamp)
    stem = f"{handle}_{code}"
    for i in range(1, slides + 1):
        (raw / f"{stem}_{i}.jpg").write_bytes(b"y" * 512)
    (raw / f"{stem}.json").write_text(json.dumps({
        "handle": handle, "shortcode": code, "kind": "image", "caption": "c",
    }), encoding="utf-8")
    if transcript:
        (tree / "data" / "transcripts" / stamp / f"{stem}.txt").write_text(
            "headline text", encoding="utf-8")
    return stem


def run(tree, keep_days=3, today=TODAY):
    return prune.prune(
        tree / "data" / "raw", tree / "data" / "transcripts", tree / "news",
        keep_days=keep_days, today=today,
    )


def media(tree, stamp):
    raw = tree / "data" / "raw" / stamp
    return sorted(p.name for p in raw.iterdir()) if raw.is_dir() else []


# --- the happy path --------------------------------------------------------


def test_transcribed_media_older_than_the_window_is_deleted(tree):
    video(tree, "2026-07-20")

    result = run(tree, keep_days=3)

    assert result.files == 1
    assert result.days == 1
    assert media(tree, "2026-07-20") == ["aaronparnas_AAA.json"]


def test_the_sidecar_and_transcript_always_survive(tree):
    """They are what lets a pruned day be re-summarized offline."""
    stem = video(tree, "2026-07-20")

    run(tree)

    assert (tree / "data" / "raw" / "2026-07-20" / f"{stem}.json").is_file()
    assert (tree / "data" / "transcripts" / "2026-07-20" / f"{stem}.txt").is_file()


def test_carousel_slides_are_all_removed(tree):
    carousel(tree, "2026-07-20", slides=4)

    result = run(tree)

    assert result.files == 4
    assert media(tree, "2026-07-20") == ["oafnation_actual_CAR.json"]


def test_a_stray_wav_is_cleaned_up(tree):
    """An interrupted transcription can leave one behind."""
    day(tree, "2026-07-20")
    (tree / "data" / "raw" / "2026-07-20" / "leftover.wav").write_bytes(b"riff")

    assert run(tree).files == 1


def test_the_freed_size_is_reported(tree):
    video(tree, "2026-07-20", size=2 * 1_048_576)
    result = run(tree)
    assert result.megabytes_freed == pytest.approx(2.0, abs=0.1)


# --- the retention window --------------------------------------------------


def test_days_inside_the_window_are_untouched(tree):
    video(tree, "2026-07-28")          # yesterday, keep_days=3

    assert run(tree, keep_days=3).files == 0
    assert "aaronparnas_AAA.mp4" in media(tree, "2026-07-28")


def test_today_is_never_pruned_even_with_zero_retention(tree):
    video(tree, TODAY.isoformat())

    assert run(tree, keep_days=0).files == 0
    assert "aaronparnas_AAA.mp4" in media(tree, TODAY.isoformat())


def test_the_boundary_day_is_kept(tree):
    """keep_days=3 on the 29th keeps the 26th and prunes the 25th."""
    video(tree, "2026-07-26", code="KEEP")
    video(tree, "2026-07-25", code="GONE")

    run(tree, keep_days=3)

    assert "aaronparnas_KEEP.mp4" in media(tree, "2026-07-26")
    assert "aaronparnas_GONE.mp4" not in media(tree, "2026-07-25")


def test_a_negative_retention_is_a_no_op(tree):
    video(tree, "2026-07-01")
    assert run(tree, keep_days=-1).files == 0


# --- safety ----------------------------------------------------------------


def test_a_day_with_no_digest_is_never_pruned(tree):
    """Without a digest the media is the only copy of that day's content."""
    raw = day(tree, "2026-07-20", digest=False)
    stem = "aaronparnas_AAA"
    (raw / f"{stem}.mp4").write_bytes(b"x" * 1024)
    (raw / f"{stem}.json").write_text(json.dumps({
        "handle": "aaronparnas", "shortcode": "AAA", "kind": "video",
    }), encoding="utf-8")
    (tree / "data" / "transcripts" / "2026-07-20" / f"{stem}.txt").write_text("w", encoding="utf-8")

    result = run(tree)

    assert result.files == 0
    assert f"{stem}.mp4" in media(tree, "2026-07-20")
    assert any("no digest" in note for note in result.skipped)


def test_an_untranscribed_post_keeps_its_own_media(tree):
    """One un-transcribed post holds back its media, not the whole day."""
    video(tree, "2026-07-20", code="DONE", transcript=True)
    video(tree, "2026-07-20", code="TODO", transcript=False)

    result = run(tree)

    remaining = media(tree, "2026-07-20")
    assert "aaronparnas_DONE.mp4" not in remaining
    assert "aaronparnas_TODO.mp4" in remaining
    assert any("not transcribed" in note for note in result.skipped)


def test_a_partly_transcribed_carousel_is_kept(tree):
    carousel(tree, "2026-07-20", code="CAR", slides=3, transcript=False)

    result = run(tree)

    assert result.files == 0
    assert len([n for n in media(tree, "2026-07-20") if n.endswith(".jpg")]) == 3


def test_non_media_files_are_left_alone(tree):
    day(tree, "2026-07-20")
    (tree / "data" / "raw" / "2026-07-20" / "notes.txt").write_text("keep", encoding="utf-8")

    run(tree)

    assert "notes.txt" in media(tree, "2026-07-20")


def test_a_directory_that_is_not_a_date_is_ignored(tree):
    odd = tree / "data" / "raw" / "scratch"
    odd.mkdir()
    (odd / "thing.mp4").write_bytes(b"x")

    assert run(tree).files == 0
    assert (odd / "thing.mp4").is_file()


def test_a_missing_raw_root_is_a_no_op(tmp_path):
    result = prune.prune(
        tmp_path / "absent", tmp_path / "t", tmp_path / "news", keep_days=3, today=TODAY,
    )
    assert result.files == 0


def test_media_with_no_sidecar_is_left_alone(tree):
    """A file this tool cannot attribute to a post is not its business."""
    day(tree, "2026-07-20")
    (tree / "data" / "raw" / "2026-07-20" / "mystery.mp4").write_bytes(b"x")

    run(tree)

    assert "mystery.mp4" in media(tree, "2026-07-20")


def test_several_days_are_pruned_in_one_pass(tree):
    video(tree, "2026-07-18", code="A")
    video(tree, "2026-07-19", code="B")
    carousel(tree, "2026-07-20", slides=2)

    result = run(tree)

    assert result.days == 3
    assert result.files == 4
