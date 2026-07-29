import json

import pytest

from src import ocr
from src.config import TranscribeConfig


CFG = TranscribeConfig(min_words=10)

HEADLINE = (
    "PROSECUTORS SAY BERLIN PRIDE ATTACK SUSPECT TIED TO IS "
    "AND FACES CHARGES IN COURT NEXT MONTH"
)


class FakeRecognizer:
    """Stands in for the Apple Vision call. Maps filename fragments to text."""

    def __init__(self, text=HEADLINE, per_file=None, raises_on=None):
        self.text = text
        self.per_file = per_file or {}
        self.raises_on = raises_on or ()
        self.calls = []

    def __call__(self, path):
        name = str(path)
        self.calls.append(name)
        # Match on the full filename: a bare "BIG_1" fragment would also match
        # "BIG_10.jpg" and silently return the wrong slide's text.
        for fragment in self.raises_on:
            if f"{fragment}.jpg" in name:
                raise RuntimeError("Vision failed")
        for fragment, value in self.per_file.items():
            if f"{fragment}.jpg" in name:
                return value
        return self.text


def make_image(raw_dir, handle="oafnation_actual", shortcode="AAA",
               slide=None, caption="", sidecar=True):
    raw_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{handle}_{shortcode}"
    name = stem if slide is None else f"{stem}_{slide}"
    (raw_dir / f"{name}.jpg").write_bytes(b"fake-jpg")
    if sidecar:
        (raw_dir / f"{stem}.json").write_text(json.dumps({
            "handle": handle,
            "shortcode": shortcode,
            "caption": caption,
            "permalink": f"https://www.instagram.com/p/{shortcode}/",
            "posted_at": "2026-07-28T09:00:00+00:00",
        }), encoding="utf-8")
    return raw_dir / f"{name}.jpg"


@pytest.fixture
def dirs(tmp_path):
    return tmp_path / "raw", tmp_path / "out"


# --- the happy path --------------------------------------------------------


def test_reads_text_off_an_image_and_writes_it(dirs):
    raw, out = dirs
    make_image(raw, caption="")
    recognizer = FakeRecognizer()

    transcripts, stats = ocr.ocr_day(raw, out, CFG, recognizer=recognizer)

    assert len(transcripts) == 1
    assert transcripts[0].handle == "oafnation_actual"
    assert transcripts[0].shortcode == "AAA"
    assert transcripts[0].text == HEADLINE
    assert transcripts[0].kind == "image"
    assert (out / "oafnation_actual_AAA.txt").read_text(encoding="utf-8").strip() == HEADLINE
    assert stats.post_count == 1
    assert stats.transcribed_count == 1
    assert stats.incomplete is False


def test_a_post_with_an_empty_caption_still_yields_content(dirs):
    """The case that motivated OCR at all.

    Verified live: a real oafnation_actual post had no caption whatsoever, so
    without reading the image the post contributes nothing to the digest.
    """
    raw, out = dirs
    make_image(raw, caption="")

    transcripts, _ = ocr.ocr_day(raw, out, CFG, recognizer=FakeRecognizer())

    assert transcripts[0].caption == ""
    assert transcripts[0].text


def test_carousel_slides_are_concatenated_in_slide_order(dirs):
    raw, out = dirs
    for slide in (1, 2, 3):
        make_image(raw, shortcode="CAR", slide=slide)
    recognizer = FakeRecognizer(per_file={
        "CAR_1": "First slide has quite a lot of words on it right here now",
        "CAR_2": "Second slide continues the story with more words again",
        "CAR_3": "Third slide finishes the story off with the last words",
    })

    transcripts, stats = ocr.ocr_day(raw, out, CFG, recognizer=recognizer)

    assert len(transcripts) == 1
    text = transcripts[0].text
    assert text.index("First slide") < text.index("Second slide") < text.index("Third slide")
    assert stats.post_count == 1
    assert stats.transcribed_count == 1


def test_slide_order_is_numeric_not_lexicographic(dirs):
    raw, out = dirs
    for slide in (1, 2, 10):
        make_image(raw, shortcode="BIG", slide=slide)
    recognizer = FakeRecognizer(per_file={
        "BIG_1": "one one one one one one one one one one one",
        "BIG_2": "two two two two two two two two two two two",
        "BIG_10": "ten ten ten ten ten ten ten ten ten ten ten",
    })

    transcripts, _ = ocr.ocr_day(raw, out, CFG, recognizer=recognizer)

    text = transcripts[0].text
    assert text.index("one") < text.index("two") < text.index("ten")


# --- resumability ----------------------------------------------------------


def test_an_existing_extraction_is_reused_without_calling_vision(dirs):
    raw, out = dirs
    make_image(raw)
    out.mkdir(parents=True)
    (out / "oafnation_actual_AAA.txt").write_text(
        "already extracted with plenty of words here to pass the floor",
        encoding="utf-8",
    )
    recognizer = FakeRecognizer()

    transcripts, stats = ocr.ocr_day(raw, out, CFG, recognizer=recognizer)

    assert recognizer.calls == []
    assert transcripts[0].text.startswith("already extracted")
    assert stats.transcribed_count == 1


def test_video_files_are_ignored(dirs):
    raw, out = dirs
    raw.mkdir(parents=True)
    (raw / "aaronparnas_VID.mp4").write_bytes(b"fake-mp4")
    recognizer = FakeRecognizer()

    transcripts, stats = ocr.ocr_day(raw, out, CFG, recognizer=recognizer)

    assert transcripts == []
    assert stats.post_count == 0
    assert recognizer.calls == []


def test_missing_raw_directory_is_an_empty_day(tmp_path):
    transcripts, stats = ocr.ocr_day(
        tmp_path / "absent", tmp_path / "out", CFG, recognizer=FakeRecognizer(),
    )
    assert transcripts == []
    assert stats.post_count == 0
    assert stats.incomplete is False


# --- skips and failures ---------------------------------------------------


def test_an_image_yielding_no_text_is_skipped(dirs):
    raw, out = dirs
    make_image(raw)

    transcripts, stats = ocr.ocr_day(
        raw, out, CFG, recognizer=FakeRecognizer(text=""),
    )

    assert transcripts == []
    assert stats.post_count == 1
    assert stats.transcribed_count == 0
    assert not list(out.glob("*.txt"))


def test_a_bare_watermark_is_below_the_floor_and_skipped(dirs):
    """Some images yield only the account's watermark, which is not news."""
    raw, out = dirs
    make_image(raw)

    transcripts, stats = ocr.ocr_day(
        raw, out, CFG, recognizer=FakeRecognizer(text="/ OAF NATION //"),
    )

    assert transcripts == []
    assert stats.transcribed_count == 0


def test_a_short_headline_image_survives_when_the_caption_carries_the_story(dirs):
    """Observed on a real post: the graphic OCRs to a few stylised words while the
    caption holds the actual reporting. Judging the image alone would discard the
    post and its caption together."""
    raw, out = dirs
    make_image(raw, caption=(
        "Polish Prime Minister Donald Tusk called for an end to hate crimes "
        "following a series of attacks reported this week."
    ))

    transcripts, stats = ocr.ocr_day(
        raw, out, CFG, recognizer=FakeRecognizer(text="TUSK URGES END / OAF NATION //"),
    )

    assert len(transcripts) == 1
    assert "TUSK URGES END" in transcripts[0].text
    assert "Donald Tusk" in transcripts[0].caption
    assert stats.transcribed_count == 1


def test_a_substantial_caption_survives_even_when_ocr_finds_nothing(dirs):
    raw, out = dirs
    make_image(raw, caption=(
        "Prosecutors say the Berlin Pride attack suspect has been tied to IS "
        "and will face charges next month."
    ))

    transcripts, _ = ocr.ocr_day(raw, out, CFG, recognizer=FakeRecognizer(text=""))

    assert len(transcripts) == 1
    assert transcripts[0].text == ""
    assert "Berlin Pride" in transcripts[0].caption


def test_one_failing_image_does_not_stop_the_others(dirs):
    raw, out = dirs
    make_image(raw, shortcode="BAD")
    make_image(raw, shortcode="GOOD")

    transcripts, stats = ocr.ocr_day(
        raw, out, CFG, recognizer=FakeRecognizer(raises_on=["BAD"]),
    )

    assert [t.shortcode for t in transcripts] == ["GOOD"]
    assert stats.post_count == 2
    assert stats.transcribed_count == 1
    assert stats.incomplete is True


def test_one_failing_carousel_slide_does_not_lose_the_rest(dirs):
    raw, out = dirs
    for slide in (1, 2):
        make_image(raw, shortcode="CAR", slide=slide)
    recognizer = FakeRecognizer(
        per_file={"CAR_2": "the surviving slide still has plenty of words on it"},
        raises_on=["CAR_1"],
    )

    transcripts, stats = ocr.ocr_day(raw, out, CFG, recognizer=recognizer)

    assert len(transcripts) == 1
    assert "surviving slide" in transcripts[0].text
    assert stats.incomplete is True


def test_missing_sidecar_falls_back_to_the_filename_and_flags_the_day(dirs):
    raw, out = dirs
    make_image(raw, handle="oafnation_actual", shortcode="XYZ", sidecar=False)

    transcripts, stats = ocr.ocr_day(raw, out, CFG, recognizer=FakeRecognizer())

    assert transcripts[0].handle == "oafnation_actual"
    assert transcripts[0].shortcode == "XYZ"
    assert stats.incomplete is True


def test_posts_come_back_in_a_stable_order(dirs):
    raw, out = dirs
    for code in ("CCC", "AAA", "BBB"):
        make_image(raw, shortcode=code)

    transcripts, _ = ocr.ocr_day(raw, out, CFG, recognizer=FakeRecognizer())

    assert [t.shortcode for t in transcripts] == ["AAA", "BBB", "CCC"]
