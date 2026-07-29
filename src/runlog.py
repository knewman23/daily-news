"""A record of every run, so failures are visible without opening a terminal.

The per-day log files hold the detail, but nothing points at them and nothing
says which runs went wrong. A run that fails at 11am is otherwise invisible until
someone notices a digest that never appeared — and the digest's `incomplete` flag
says only *that* something went wrong, never *what*.

Each run appends one record: when, for which date, whether it succeeded, the
counts, and the specific failure notes. Newest first, capped, so the file stays
readable and cannot grow without bound.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from src import atomic

log = logging.getLogger(__name__)

RUNS_FILE = "runs.json"
KEEP = 200
LOG_TAIL_BYTES = 256_000


@dataclass
class RunRecord:
    started_at: str
    finished_at: str
    date: str
    ok: bool
    post_count: int = 0
    transcribed_count: int = 0
    spoken_count: int = 0
    image_count: int = 0
    topic_count: int = 0
    incomplete: bool = False
    error: str | None = None
    failures: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        try:
            start = datetime.fromisoformat(self.started_at)
            end = datetime.fromisoformat(self.finished_at)
            return round((end - start).total_seconds(), 1)
        except ValueError:
            return 0.0

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["duration_seconds"] = self.duration_seconds
        return payload


def append(logs_dir: str | Path, record: RunRecord, keep: int = KEEP) -> None:
    """Add a run to the history. Never raises — a bookkeeping failure must not
    fail a run that otherwise produced a digest."""
    path = Path(logs_dir) / RUNS_FILE
    try:
        history = _read(path)
        history.insert(0, record.as_dict())
        atomic.write_json(path, {"runs": history[:keep]})
    except Exception as exc:                            # pragma: no cover
        log.warning("could not write the run history: %s", exc)


def load(logs_dir: str | Path) -> list[dict]:
    """Every recorded run, newest first. Missing or corrupt history is empty.

    Unlike the source list, a broken history degrades rather than raising: it is
    a diagnostic aid, and taking the whole page down because the diagnostics are
    unreadable would be backwards.
    """
    return _read(Path(logs_dir) / RUNS_FILE)


def read_log(logs_dir: str | Path, day: date | str, tail_bytes: int = LOG_TAIL_BYTES) -> str:
    """The log file for one date, truncated from the front if very large.

    Whisper logs three lines per video, so a busy day runs to thousands of lines.
    The end is the part that explains how the run finished.
    """
    stamp = day.isoformat() if isinstance(day, date) else str(day)
    path = Path(logs_dir) / f"{stamp}.log"
    if not path.is_file():
        return ""

    size = path.stat().st_size
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        if size > tail_bytes:
            handle.seek(size - tail_bytes)
            handle.readline()               # drop the partial line at the seek point
            return f"[…truncated {size - tail_bytes} earlier bytes…]\n" + handle.read()
        return handle.read()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    runs = data.get("runs") if isinstance(data, dict) else None
    return runs if isinstance(runs, list) else []
