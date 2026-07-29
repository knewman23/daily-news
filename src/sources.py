"""The curated handle list.

Owns config/sources.json and is its only writer. Two per-handle timestamps
matter and are easy to confuse:

    last_pull_at  the fetch watermark. Everything posted after this is still
                  owed to us. Advanced only when that handle's fetch succeeds,
                  so a rate-limited account retries the same window rather
                  than losing those posts.

    last_seen     the date a post was last actually found. Purely informational,
                  but it is what makes a silently dead account visible in the UI.

A corrupt or missing file raises rather than degrading to an empty list. An
empty list would produce a digest with no news in it, which is indistinguishable
from a quiet news day.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src import atomic

HANDLE_RE = re.compile(r"^[a-z0-9._]{1,30}$")

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_HOST_RE = re.compile(r"^(www\.)?instagram\.com/", re.IGNORECASE)


class SourcesError(Exception):
    """Base class for every failure this module raises."""


class SourcesFileError(SourcesError):
    """The file is missing, unreadable, or not the shape we expect."""


class DuplicateHandle(SourcesError):
    """The handle is already in the list."""


class UnknownHandle(SourcesError):
    """No such handle in the list."""


class LookupFailed(SourcesError):
    """The profile could not be reached, so the handle was not added."""


@dataclass(frozen=True)
class Source:
    handle: str
    enabled: bool = True
    added: str | None = None
    last_pull_at: str | None = None
    last_seen: str | None = None


def normalize(raw: object) -> str:
    """Reduce any way of writing a handle to its canonical form.

    Accepts a bare handle, an @-prefixed handle, or a profile URL with or
    without a scheme, host, query string, or trailing slash.
    """
    if not isinstance(raw, str):
        raise ValueError(f"handle must be a string, got {type(raw).__name__}")

    s = raw.strip()
    s = s.split("?", 1)[0].split("#", 1)[0]
    s = _SCHEME_RE.sub("", s)
    s = _HOST_RE.sub("", s)
    s = s.strip().lstrip("@").rstrip("/").lower()

    if not HANDLE_RE.match(s):
        raise ValueError(f"not a valid Instagram handle: {raw!r}")
    return s


def load(path: str | Path) -> list[Source]:
    return [_to_source(rec, path) for rec in _read(path)["sources"]]


def enabled_sources(path: str | Path) -> list[Source]:
    """Whole records, not bare handles — fetch needs each one's watermark."""
    return [s for s in load(path) if s.enabled]


def add(
    path: str | Path,
    handle: str,
    lookup: Callable[[str], Any],
    today: str | date | None = None,
) -> Source:
    """Validate, confirm the account is reachable, then append it.

    The lookup runs before anything is written, so a handle that cannot be
    reached leaves the file untouched.
    """
    normalized = normalize(handle)

    if any(s.handle == normalized for s in load(path)):
        raise DuplicateHandle(f"{normalized} is already in the source list")

    try:
        lookup(normalized)
    except Exception as exc:
        raise LookupFailed(f"{normalized}: {exc}") from exc

    record = {
        "handle": normalized,
        "enabled": True,
        "added": _as_date(today) if today else _today(),
        "last_pull_at": None,
        "last_seen": None,
    }

    data = _read(path)
    data["sources"].append(record)
    atomic.write_json(path, data)
    return _to_source(record, path)


def set_enabled(path: str | Path, handle: str, flag: bool) -> None:
    def apply(record: dict[str, Any]) -> bool:
        record["enabled"] = bool(flag)
        return True

    _mutate(path, handle, apply)


def remove(path: str | Path, handle: str) -> None:
    normalized = normalize(handle)
    data = _read(path)
    kept = [r for r in data["sources"] if r.get("handle") != normalized]
    if len(kept) == len(data["sources"]):
        raise UnknownHandle(f"{normalized} is not in the source list")
    data["sources"] = kept
    atomic.write_json(path, data)


def advance_watermark(path: str | Path, handle: str, when: str | datetime) -> None:
    """Move a handle's fetch watermark forward. Never backward.

    A backward move would re-open a window that was already closed, so an
    out-of-order or clock-skewed run would re-fetch and re-summarize posts
    already covered. Going backwards is silently a no-op.
    """
    stamp = _as_utc_iso(when)

    def apply(record: dict[str, Any]) -> bool:
        current = record.get("last_pull_at")
        if current is not None and _parse_utc(current) >= _parse_utc(stamp):
            return False
        record["last_pull_at"] = stamp
        return True

    _mutate(path, handle, apply)


def stamp_last_seen(path: str | Path, handle: str, when: str | date) -> None:
    stamp = _as_date(when)

    def apply(record: dict[str, Any]) -> bool:
        record["last_seen"] = stamp
        return True

    _mutate(path, handle, apply)


# --- internals -------------------------------------------------------------


def _read(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourcesFileError(f"cannot read source list at {p}: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SourcesFileError(f"malformed JSON in {p}: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
        raise SourcesFileError(f"{p} does not contain a 'sources' list")
    return data


def _to_source(record: Any, path: str | Path) -> Source:
    if not isinstance(record, dict) or not isinstance(record.get("handle"), str):
        raise SourcesFileError(f"{path} contains a source with no handle: {record!r}")
    return Source(
        handle=record["handle"],
        enabled=bool(record.get("enabled", True)),
        added=record.get("added"),
        last_pull_at=record.get("last_pull_at"),
        last_seen=record.get("last_seen"),
    )


def _mutate(
    path: str | Path,
    handle: str,
    apply: Callable[[dict[str, Any]], bool],
) -> None:
    """Read, apply to one record in place, write only if something changed.

    Mutating the raw dict rather than rebuilding it from Source objects keeps
    any unrecognized keys in the file intact.
    """
    normalized = normalize(handle)
    data = _read(path)

    for record in data["sources"]:
        if record.get("handle") == normalized:
            if apply(record):
                atomic.write_json(path, data)
            return

    raise UnknownHandle(f"{normalized} is not in the source list")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _as_date(value: str | date) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(value).isoformat()


def _as_utc_iso(value: str | datetime) -> str:
    moment = value if isinstance(value, datetime) else _parse_utc(value)
    if moment.tzinfo is None:
        raise ValueError(
            f"watermark must be timezone-aware, got naive {moment.isoformat()}"
        )
    return moment.astimezone(timezone.utc).isoformat()


def _parse_utc(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError(f"watermark must be timezone-aware: {value!r}")
    return moment.astimezone(timezone.utc)
