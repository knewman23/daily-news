"""Delete downloaded media once it has been transcribed.

A single day of posts is roughly 270 MB, almost all of it mp4 — about 98 GB a
year. Compressing it is pointless: mp4 and jpg are already compressed, and
gzip -9 on a real reel came back at 99.3% of the original. The only thing that
actually reclaims the space is deleting it.

What is kept is what has lasting value and costs nothing:

    data/transcripts/<date>/*.txt   the extracted text  (~150 KB/day)
    data/raw/<date>/*.json          the caption sidecars (~1 KB/post)
    news/<date>.md                  the digest itself

Those three are enough to re-summarize any day offline and get the same result,
which is why the extract stages find their posts from the sidecars rather than by
globbing media.

Three rules make this safe to run unattended:

  * A day is only pruned once its digest exists. No digest means the media is
    still the only copy of that day's content.
  * A post is only pruned once its transcript exists. One un-transcribed post
    holds back its own media, not the whole day.
  * Today is never pruned, whatever the retention setting says.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from src import posts

log = logging.getLogger(__name__)

SOURCE_SUFFIXES = (".mp4", ".jpg")

# Intermediates this project writes itself, never a source of truth, so they can
# go without being attributable to a post. transcribe.py removes its wav in a
# finally block; one only survives if a process was killed mid-transcription.
SCRATCH_SUFFIXES = (".wav",)

MEDIA_SUFFIXES = SOURCE_SUFFIXES + SCRATCH_SUFFIXES


@dataclass
class PruneResult:
    days: int = 0
    files: int = 0
    bytes_freed: int = 0
    skipped: list[str] = field(default_factory=list)

    @property
    def megabytes_freed(self) -> float:
        return round(self.bytes_freed / 1_048_576, 1)


def prune(
    raw_root: str | Path,
    transcripts_root: str | Path,
    news_dir: str | Path,
    keep_days: int,
    today: date | None = None,
) -> PruneResult:
    """Delete transcribed media older than keep_days. Never raises."""
    result = PruneResult()
    if keep_days < 0:
        return result

    root = Path(raw_root)
    if not root.is_dir():
        return result

    now = today or date.today()
    cutoff = now - timedelta(days=keep_days)

    for day_dir in sorted(root.iterdir()):
        if not day_dir.is_dir():
            continue

        try:
            day = date.fromisoformat(day_dir.name)
        except ValueError:
            continue                            # not a dated directory

        if day >= now:
            continue        # today is never pruned, whatever keep_days says

        if day >= cutoff:
            continue        # inside the retention window

        if not (Path(news_dir) / f"{day.isoformat()}.md").is_file():
            # Without a digest, this media is the only copy of the day.
            result.skipped.append(f"{day.isoformat()}: no digest yet")
            continue

        freed, count, held = _prune_day(day_dir, Path(transcripts_root) / day_dir.name)
        if count:
            result.days += 1
            result.files += count
            result.bytes_freed += freed
        result.skipped.extend(held)

    if result.files:
        log.info("pruned %d media file(s) across %d day(s), freeing %s MB",
                 result.files, result.days, result.megabytes_freed)
    for note in result.skipped:
        log.info("kept media: %s", note)

    return result


# --- internals -------------------------------------------------------------


def _prune_day(day_dir: Path, transcripts: Path) -> tuple[int, int, list[str]]:
    """Remove media for posts that have a transcript. Sidecars always stay."""
    freed = 0
    removed = 0
    held: list[str] = []

    recorded = posts.load(day_dir)
    transcribed = {
        post.stem for post in recorded
        if post.kind and (transcripts / f"{post.stem}.txt").is_file()
    }
    known = {post.stem for post in recorded}

    # A post with no recorded kind cannot be classified once its media is gone,
    # so its media stays. posts.load backfills the field whenever the media is
    # still there, which means this only ever holds back a genuinely broken post.
    unclassified = {post.stem for post in recorded if not post.kind}

    for path in sorted(day_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in MEDIA_SUFFIXES:
            continue

        if path.suffix.lower() in SOURCE_SUFFIXES:
            stem = _post_stem(path.stem, known)
            if not stem:
                # Not attributable to any recorded post. Deleting a file this
                # tool does not understand is not its business.
                held.append(f"{day_dir.name}/{path.name}: no matching post")
                continue
            if stem in unclassified:
                held.append(f"{day_dir.name}/{path.name}: sidecar has no kind")
                continue
            if stem not in transcribed:
                held.append(f"{day_dir.name}/{path.name}: not transcribed")
                continue

        try:
            size = path.stat().st_size
            path.unlink()
        except OSError as exc:
            log.warning("could not delete %s: %s", path, exc)
            continue

        freed += size
        removed += 1

    return freed, removed, held


def _post_stem(filename: str, known: set[str]) -> str:
    """Map a media filename back to its post.

    Carousel slides are `<stem>_<n>`, and both handles and shortcodes may contain
    underscores, so the split is resolved against the posts actually recorded for
    the day rather than guessed from the name.
    """
    if filename in known:
        return filename

    head, sep, tail = filename.rpartition("_")
    if sep and tail.isdigit() and head in known:
        return head
    return ""
