"""Flipping one topic's `skipped:` line inside a generated digest.

This and notes.py are the only code permitted to modify a file in news/. The
stakes are the same — a bad write destroys news the user cannot recover — so the
contract is deliberately just as narrow:

  * The headline must match exactly one `## ` section. Zero or several is an
    error, never a guess: restoring or dropping the wrong topic is a silent
    wrong edit, and the file gives no way to notice it later.
  * Only the matched section's meta lines are touched. Everything before and
    after it is copied verbatim, byte for byte.
  * Everything from `## My Notes` onward is off limits. It is split off before
    the search so a journal entry that happens to contain `## <headline>` cannot
    be matched, and re-attached unchanged afterwards.
  * A reason may not contain a newline or a notes marker. A newline would end
    the meta line and silently turn its tail into body text; a marker would let
    a reason forge a journal boundary, which is what notes.write_notes refuses
    for the same reason.

TopicError maps to HTTP 409, like NotesMarkerError. The file is left exactly as
it was.
"""

from __future__ import annotations

import re
from pathlib import Path

from src import atomic, notes
from src.digest import NOTES_HEADING

SKIPPED_LINE = re.compile(r"^skipped:.*$\n?", re.IGNORECASE | re.MULTILINE)
_META_LINE = re.compile(r"^(tags|sources|posts|skipped):", re.IGNORECASE)

DEFAULT_REASON = "marked by hand"


class TopicError(Exception):
    """The headline matched no section, or more than one, or the reason is unusable."""


def set_skipped(path: str | Path, headline: str, reason: str = DEFAULT_REASON) -> str:
    """Mark one topic as skipped. Returns the reason as stored."""
    cleaned = " ".join((reason or "").split()) or DEFAULT_REASON
    if notes.START in cleaned or notes.END in cleaned:
        raise TopicError("a skip reason may not contain the notes block markers")

    _rewrite(path, headline, f"skipped: {cleaned}")
    return cleaned


def clear_skipped(path: str | Path, headline: str) -> None:
    """Restore one topic to the feed."""
    _rewrite(path, headline, None)


# --- internals -------------------------------------------------------------


def _rewrite(path: str | Path, headline: str, line: str | None) -> None:
    """Replace the section's `skipped:` line with `line`, or drop it if None."""
    wanted = " ".join((headline or "").split())
    if not wanted:
        raise TopicError("no headline given")

    target = Path(path)
    text = target.read_text(encoding="utf-8")

    # The journal is split off rather than searched-and-skipped: a note is free
    # text and may legitimately contain a line that looks like a heading.
    news, separator, journal = text.partition(NOTES_HEADING)

    start, end = _locate(news, wanted, target)
    section = news[start:end]
    rebuilt = _apply(section, line)

    if rebuilt == section:
        return                                  # already in the wanted state

    atomic.write_text(
        target, f"{news[:start]}{rebuilt}{news[end:]}{separator}{journal}",
    )


def _locate(news: str, headline: str, path: Path) -> tuple[int, int]:
    """Byte range of the one section whose heading is `headline`.

    Matching is on the collapsed heading text, so trailing whitespace in the file
    does not decide whether an edit is possible.
    """
    spans = []
    starts = [m.start() for m in re.finditer(r"^## ", news, re.MULTILINE)]
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(news)
        first = news[start:end].split("\n", 1)[0]
        if " ".join(first[len("## "):].split()) == headline:
            spans.append((start, end))

    if not spans:
        raise TopicError(f"{path.name} has no topic headed {headline!r}")
    if len(spans) > 1:
        raise TopicError(
            f"{path.name} has {len(spans)} topics headed {headline!r}, so which "
            f"one to edit is ambiguous"
        )
    return spans[0]


def _apply(section: str, line: str | None) -> str:
    """Set, replace, or remove the section's `skipped:` line."""
    existing = SKIPPED_LINE.search(section)

    if line is None:
        return SKIPPED_LINE.sub("", section, count=1) if existing else section
    if existing:
        return SKIPPED_LINE.sub(lambda _: line + "\n", section, count=1)

    return _insert(section, line)


def _insert(section: str, line: str) -> str:
    """Add the line at the end of the heading's run of meta lines.

    After the meta run rather than before the body, because the body may itself
    start with something that looks like a meta line, and appending there would
    put the marker inside the prose.
    """
    lines = section.split("\n")
    at = 1                                       # index 0 is the `## ` heading
    while at < len(lines) and _META_LINE.match(lines[at].strip()):
        at += 1

    lines.insert(at, line)
    return "\n".join(lines)
