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

import json
import logging
import re
from pathlib import Path
from typing import Callable

from src.config import TranscribeConfig
from src.records import Stats, Transcript

log = logging.getLogger(__name__)

# `<handle>_<shortcode>.jpg` for a single image, `<handle>_<shortcode>_<n>.jpg`
# for carousel slide n.
SLIDE_SUFFIX = re.compile(r"_(\d+)$")

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

    if not raw.is_dir():
        return transcripts, stats

    for stem, slides in sorted(_group_by_post(raw).items()):
        stats.post_count += 1
        existing = out / f"{stem}.txt"
        meta = _sidecar(raw, stem, stats)

        if existing.exists():
            transcripts.append(_transcript(meta, existing.read_text(encoding="utf-8").strip()))
            stats.transcribed_count += 1
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
        caption = str(meta.get("caption") or "")
        if len(f"{combined} {caption}".split()) < cfg.min_words:
            log.info("%s yielded too little text, skipping", stem)
            continue

        out.mkdir(parents=True, exist_ok=True)
        existing.write_text(combined + "\n", encoding="utf-8")
        transcripts.append(_transcript(meta, combined))
        stats.transcribed_count += 1

    return transcripts, stats


# --- internals -------------------------------------------------------------


def _group_by_post(raw: Path) -> dict[str, list[Path]]:
    """Map post stem to its image files, slides ordered numerically.

    Sorting slide paths as strings would put slide 10 before slide 2 and scramble
    a long carousel's narrative.
    """
    groups: dict[str, list[tuple[int, Path]]] = {}

    for image in raw.glob("*.jpg"):
        stem, index = _post_stem(image.stem)
        groups.setdefault(stem, []).append((index, image))

    return {
        stem: [path for _, path in sorted(entries)]
        for stem, entries in groups.items()
    }


def _post_stem(filename: str) -> tuple[str, int]:
    match = SLIDE_SUFFIX.search(filename)
    if match:
        return filename[: match.start()], int(match.group(1))
    return filename, 0


def _sidecar(raw: Path, stem: str, stats: Stats) -> dict:
    """Attribution written by fetch.py beside the images.

    Falling back to the filename is a last resort: both handles and Instagram
    shortcodes may contain underscores, so the split is ambiguous and the day is
    flagged rather than risk silently mis-attributing a post.
    """
    path = raw / f"{stem}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("handle"):
            return data
    except (OSError, json.JSONDecodeError):
        pass

    handle, _, shortcode = stem.rpartition("_")
    stats.fail(f"missing caption sidecar for {stem}")
    return {"handle": handle or stem, "shortcode": shortcode}


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
