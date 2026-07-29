"""Audio to text, locally.

faster-whisper runs on CPU via CTranslate2 — there is no Metal/MPS backend, so
device="cpu" with int8 is the correct configuration on Apple Silicon rather than
a fallback from something faster.

Nothing here knows about Instagram. It reads mp4s from a directory, writes .txt
files next to nothing, and reports counts. The sidecar JSON that fetch.py leaves
beside each mp4 supplies the attribution, because a filename cannot be parsed
back into a handle reliably: both handles and Instagram shortcodes may contain
underscores, so `oafnation_actual_AB_cd.mp4` has no unambiguous split point.

A single post failing never aborts the day. A day that loses one post is worth
publishing; the loss is recorded so the digest can be marked incomplete.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from src.config import TranscribeConfig
from src.records import Stats, Transcript

log = logging.getLogger(__name__)

FFPROBE = "ffprobe"
FFMPEG = "ffmpeg"


class ToolMissing(Exception):
    """A required external binary is not on PATH."""


def require_tools(which: Callable[[str], str | None] = shutil.which) -> None:
    """Fail before any work is attempted, not once per file mid-run."""
    missing = [name for name in (FFMPEG, FFPROBE) if which(name) is None]
    if missing:
        raise ToolMissing(
            f"{', '.join(missing)} not found on PATH. Install with: brew install ffmpeg"
        )


def has_audio(mp4: Path, runner: Callable[..., subprocess.CompletedProcess]) -> bool:
    """True when the file carries at least one audio stream.

    Empty stdout means no audio track — reels are frequently music-only or
    silent, and running whisper on those wastes a model pass to produce nothing.
    """
    completed = runner(
        [FFPROBE, "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(mp4)],
        capture_output=True, text=True,
    )
    return bool((completed.stdout or "").strip())


def extract_wav(
    mp4: Path,
    wav: Path,
    runner: Callable[..., subprocess.CompletedProcess],
) -> None:
    """Decode to 16 kHz mono PCM, which is what whisper expects.

    -nostdin matters: without it ffmpeg can consume the parent process's stdin
    and hang an unattended launchd run with no output and no error.
    """
    completed = runner(
        [FFMPEG, "-nostdin", "-v", "error", "-y", "-i", str(mp4),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
        capture_output=True, text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed on {mp4.name}: {(completed.stderr or '').strip()[:300]}"
        )


def transcribe_file(wav: Path, model) -> str:
    """Run whisper over one wav and return the joined text.

    vad_filter drops non-speech stretches. Without it whisper hallucinates
    filler over music-only passages, which reels have a great deal of.
    """
    segments, _info = model.transcribe(str(wav), vad_filter=True)
    return " ".join(segment.text.strip() for segment in segments).strip()


def transcribe_day(
    raw_dir: str | Path,
    out_dir: str | Path,
    cfg: TranscribeConfig,
    model_factory: Callable[[TranscribeConfig], object] = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> tuple[list[Transcript], Stats]:
    """Transcribe every mp4 without a transcript. Returns the day's transcripts.

    Already-transcribed posts are read back off disk rather than skipped, so a
    re-run produces a complete digest without re-doing any whisper work.
    """
    raw = Path(raw_dir)
    out = Path(out_dir)
    stats = Stats()
    transcripts: list[Transcript] = []

    if not raw.is_dir():
        return transcripts, stats

    factory = model_factory or _load_model
    model = None  # built on first real transcription, never if there is none

    for mp4 in sorted(raw.glob("*.mp4")):
        stats.post_count += 1
        meta = _sidecar(mp4, stats)
        existing = out / f"{mp4.stem}.txt"

        if existing.exists():
            transcripts.append(_transcript(meta, existing.read_text(encoding="utf-8").strip()))
            stats.transcribed_count += 1
            continue

        try:
            if not has_audio(mp4, runner):
                log.info("%s has no audio track, skipping", mp4.name)
                continue

            if model is None:
                model = factory(cfg)

            text = _run(mp4, out, model, runner)
        except Exception as exc:
            log.warning("transcription failed for %s: %s", mp4.name, exc)
            stats.fail(f"transcribe {mp4.name}: {exc}")
            continue

        if len(text.split()) < cfg.min_words:
            log.info("%s produced %d words, below the floor, skipping",
                     mp4.name, len(text.split()))
            continue

        out.mkdir(parents=True, exist_ok=True)
        existing.write_text(text + "\n", encoding="utf-8")
        transcripts.append(_transcript(meta, text))
        stats.transcribed_count += 1

    return transcripts, stats


# --- internals -------------------------------------------------------------


def _run(mp4: Path, out: Path, model, runner) -> str:
    """Extract, transcribe, and always remove the intermediate wav."""
    out.mkdir(parents=True, exist_ok=True)
    wav = out / f"{mp4.stem}.wav"
    try:
        extract_wav(mp4, wav, runner)
        return transcribe_file(wav, model)
    finally:
        wav.unlink(missing_ok=True)


def _sidecar(mp4: Path, stats: Stats) -> dict:
    """Attribution for one post, from the JSON fetch.py wrote beside the mp4.

    Falling back to the filename is a last resort: the split is ambiguous when a
    handle or shortcode contains an underscore, so the day is flagged rather than
    silently mis-attributed.
    """
    path = mp4.with_suffix(".json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("handle"):
            return data
    except (OSError, json.JSONDecodeError):
        pass

    handle, _, shortcode = mp4.stem.rpartition("_")
    stats.fail(f"missing caption sidecar for {mp4.name}")
    return {"handle": handle or mp4.stem, "shortcode": shortcode}


def _transcript(meta: dict, text: str) -> Transcript:
    return Transcript(
        handle=str(meta.get("handle", "")),
        shortcode=str(meta.get("shortcode", "")),
        text=text,
        caption=str(meta.get("caption") or ""),
        permalink=str(meta.get("permalink") or ""),
        posted_at=meta.get("posted_at"),
    )


def _load_model(cfg: TranscribeConfig):
    """Imported lazily so tests and the web app never pay the import cost."""
    from faster_whisper import WhisperModel

    log.info("loading whisper model %s (%s)", cfg.model, cfg.compute_type)
    return WhisperModel(cfg.model, device="cpu", compute_type=cfg.compute_type)
