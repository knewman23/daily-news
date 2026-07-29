"""Reading text off image posts, via the OCR built into macOS.

Some followed accounts publish news as text-on-image rather than video, and
frequently with an empty caption — a real post from oafnation_actual carried a
full CENTCOM statement in the image and nothing at all in the caption. Without
reading the image, those posts contribute nothing to the digest.

Apple's Vision framework does this locally: no model download, no API key, no
per-image cost, measured at 0.1-0.4s per image with accurate results on real
posts. Tesseract would need a Homebrew dependency and reads stylised headline
text less reliably.

Output is deliberately the same Transcript record that transcribe.py produces,
so everything downstream treats spoken and on-image text identically. Only
`kind` differs, which the summarizer uses to label the two apart.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from src import posts
from src.config import TranscribeConfig
from src.records import Stats, Transcript

log = logging.getLogger(__name__)

# 0 = accurate, 1 = fast. Accurate is the point of doing this at all, and at a
# few tenths of a second per image the difference does not matter here.
RECOGNITION_ACCURATE = 0


def extract_text(image: str | Path) -> str:
    """Run Apple Vision text recognition over one image file.

    Vision is imported here rather than at module scope so the web app and the
    unit tests never pay the framework import, which is slow and only relevant
    on macOS.
    """
    import Vision
    from Foundation import NSURL

    url = NSURL.fileURLWithPath_(str(image))
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(RECOGNITION_ACCURATE)
    request.setUsesLanguageCorrection_(True)

    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"Vision failed on {Path(image).name}: {error}")

    lines = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if candidates:
            lines.append(str(candidates[0].string()))

    return "\n".join(lines).strip()


def ocr_day(
    raw_dir: str | Path,
    out_dir: str | Path,
    cfg: TranscribeConfig,
    recognizer: Callable[[Path], str] = extract_text,
) -> tuple[list[Transcript], Stats]:
    """Extract text from every image post that has no extraction yet.

    One Transcript per *post*, not per file: a carousel's slides are concatenated
    in slide order, because the slides are one continuous story.
    """
    raw = Path(raw_dir)
    out = Path(out_dir)
    stats = Stats()
    transcripts: list[Transcript] = []

    for post in posts.of_kind(raw, posts.IMAGE):
        stats.post_count += 1
        existing = post.transcript(out)
        meta = post.meta

        if existing.exists():
            transcripts.append(_transcript(meta, existing.read_text(encoding="utf-8").strip()))
            stats.transcribed_count += 1
            continue

        slides = post.images(raw)
        if not slides:
            # Pruned before it was read. The text is unrecoverable, so say so
            # rather than reporting a quietly thinner day.
            log.warning("%s has no extraction and its images are gone", post.stem)
            stats.fail(f"ocr {post.stem}: images pruned before extraction")
            continue

        pieces = []
        for slide in slides:
            try:
                text = recognizer(slide)
            except Exception as exc:
                log.warning("OCR failed for %s: %s", slide.name, exc)
                stats.fail(f"ocr {slide.name}: {exc}")
                continue
            if text.strip():
                pieces.append(text.strip())

        combined = "\n".join(pieces).strip()

        # The floor counts the caption too. A headline graphic often OCRs to a
        # handful of stylised words while the caption carries the actual story —
        # judging the image alone would discard the post and its caption with it.
        caption = post.caption
        if len(f"{combined} {caption}".split()) < cfg.min_words:
            log.info("%s yielded too little text, skipping", post.stem)
            continue

        out.mkdir(parents=True, exist_ok=True)
        existing.write_text(combined + "\n", encoding="utf-8")
        transcripts.append(_transcript(meta, combined))
        stats.transcribed_count += 1

    return transcripts, stats


# --- internals -------------------------------------------------------------


def _transcript(meta: dict, text: str) -> Transcript:
    return Transcript(
        handle=str(meta.get("handle", "")),
        shortcode=str(meta.get("shortcode", "")),
        text=text,
        caption=str(meta.get("caption") or ""),
        permalink=str(meta.get("permalink") or ""),
        posted_at=meta.get("posted_at"),
        kind="image",
    )
