"""The day's posts, as recorded on disk.

fetch.py writes one JSON sidecar per post. That sidecar — not the presence of an
mp4 or a jpg — is what defines the day, which is the whole reason the media can
be deleted once it has been transcribed: a re-run still sees every post, reuses
the transcripts, and produces the same digest without touching the network.

Globbing media instead would make a pruned day look empty.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from src import atomic

log = logging.getLogger(__name__)

VIDEO = "video"
IMAGE = "image"

SLIDE_SUFFIX = "_"

# Written when extraction ran and produced nothing usable — a silent video, a
# graphic with no readable text. Distinct from "not extracted yet", which is why
# it exists: without it those posts are retried by whisper on every run forever
# and their media can never be pruned.
SETTLED_SUFFIX = ".none"


@dataclass(frozen=True)
class Post:
    stem: str
    kind: str
    meta: dict
    sidecar: Path

    @property
    def handle(self) -> str:
        return str(self.meta.get("handle", ""))

    @property
    def shortcode(self) -> str:
        return str(self.meta.get("shortcode", ""))

    @property
    def caption(self) -> str:
        return str(self.meta.get("caption") or "")

    def video(self, raw: Path) -> Path:
        return raw / f"{self.stem}.mp4"

    def images(self, raw: Path) -> list[Path]:
        """Slide files in numeric order, or the single image.

        Sorting the paths as strings would put slide 10 before slide 2 and
        scramble a long carousel's narrative.
        """
        single = raw / f"{self.stem}.jpg"
        if single.is_file():
            return [single]

        numbered = []
        for path in raw.glob(f"{self.stem}{SLIDE_SUFFIX}*.jpg"):
            tail = path.stem[len(self.stem) + 1:]
            if tail.isdigit():
                numbered.append((int(tail), path))
        return [path for _, path in sorted(numbered)]

    def transcript(self, out: Path) -> Path:
        return out / f"{self.stem}.txt"

    def settled_marker(self, out: Path) -> Path:
        return out / f"{self.stem}{SETTLED_SUFFIX}"

    def is_settled(self, out: Path) -> bool:
        """True once extraction has reached a conclusion, usable text or not."""
        return self.transcript(out).is_file() or self.settled_marker(out).is_file()


def load(raw_dir: str | Path) -> list[Post]:
    """Every post recorded for a day, in a stable order."""
    raw = Path(raw_dir)
    if not raw.is_dir():
        return []

    posts = []
    for sidecar in sorted(raw.glob("*.json")):
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("skipping unreadable sidecar %s: %s", sidecar.name, exc)
            continue
        if not isinstance(meta, dict) or not meta.get("handle"):
            log.warning("skipping sidecar with no handle: %s", sidecar.name)
            continue

        posts.append(Post(
            stem=sidecar.stem,
            kind=_kind(sidecar.stem, meta, sidecar, raw),
            meta=meta,
            sidecar=sidecar,
        ))
    return posts


def of_kind(raw_dir: str | Path, kind: str) -> list[Post]:
    return [p for p in load(raw_dir) if p.kind == kind]


def orphans(raw_dir: str | Path) -> list[Path]:
    """Media files that belong to no recorded post.

    fetch.py writes the media then the sidecar, so a process killed between the
    two leaves media that nothing will ever transcribe. Since the sidecar defines
    the day, such a file would otherwise vanish from the digest silently — the
    old code guessed a handle out of the filename instead, which risked
    attributing a post to the wrong account.
    """
    raw = Path(raw_dir)
    if not raw.is_dir():
        return []

    known = {post.stem for post in load(raw)}
    stray = []

    for path in sorted(raw.iterdir()):
        if not path.is_file() or path.suffix.lower() not in (".mp4", ".jpg"):
            continue
        if path.stem in known:
            continue
        head, sep, tail = path.stem.rpartition(SLIDE_SUFFIX)
        if sep and tail.isdigit() and head in known:
            continue           # a carousel slide of a known post
        stray.append(path)

    return stray


# --- internals -------------------------------------------------------------


def _kind(stem: str, meta: dict, sidecar: Path, raw: Path) -> str:
    """Video or image.

    Sidecars written before `kind` existed are classified by whichever media is
    on disk — and the answer is written back, because that inference stops being
    possible the moment the media is pruned. Healing it here rather than in a
    migration script means it cannot be forgotten: every read repairs what it
    touches, and prune.py runs after the extract stages have already read.
    """
    recorded = meta.get("kind")
    if recorded in (VIDEO, IMAGE):
        return recorded

    if (raw / f"{stem}.mp4").is_file():
        inferred = VIDEO
    elif (raw / f"{stem}.jpg").is_file() or any(raw.glob(f"{stem}{SLIDE_SUFFIX}*.jpg")):
        inferred = IMAGE
    else:
        return ""

    try:
        meta["kind"] = inferred
        atomic.write_json(sidecar, meta)
        log.info("backfilled kind=%s into %s", inferred, sidecar.name)
    except OSError as exc:
        log.warning("could not backfill kind into %s: %s", sidecar.name, exc)

    return inferred
