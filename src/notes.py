"""The journal block inside a generated digest.

This is the only code permitted to modify a file in news/, and the only place
where user input and generated news share a file. A bug here destroys news the
user cannot recover, so the contract is deliberately narrow:

  * Exactly one start marker and one end marker, in that order, or nothing is
    read or written at all.
  * Writes rebuild the file as prefix + marker + body + marker + suffix, where
    prefix and suffix are copied verbatim. Nothing outside the markers can move.
  * Notes may not contain the markers themselves. Allowing it would let a note
    forge a block boundary, and the next read would slice the file in the wrong
    place. Rejecting is cheaper than escaping and has no realistic cost.

Malformed markers raise NotesMarkerError, which serve.py maps to HTTP 409. The
file is left exactly as it was.
"""

from __future__ import annotations

from pathlib import Path

from src import atomic

START = "<!-- notes:start -->"
END = "<!-- notes:end -->"


class NotesMarkerError(Exception):
    """The marker block is missing, duplicated, out of order, or forged."""


def read_notes(path: str | Path) -> str:
    text = Path(path).read_text(encoding="utf-8")
    _, body, _ = _split(text, path)
    return body.strip()


def write_notes(path: str | Path, note: str) -> None:
    if START in note or END in note:
        raise NotesMarkerError("notes may not contain the notes block markers")

    p = Path(path)
    text = p.read_text(encoding="utf-8")
    prefix, _, suffix = _split(text, p)

    body = note.strip()
    middle = f"\n{body}\n" if body else "\n"

    atomic.write_text(p, f"{prefix}{START}{middle}{END}{suffix}")


def _split(text: str, path: str | Path) -> tuple[str, str, str]:
    """Return (text before the start marker, body between them, text after the end marker).

    Neither marker is included in the returned parts; write_notes re-emits them.
    Counting before slicing is what makes a duplicated marker an error rather
    than a silent choice of the first one.
    """
    if text.count(START) != 1 or text.count(END) != 1:
        raise NotesMarkerError(
            f"{path} must contain exactly one {START} and one {END} "
            f"(found {text.count(START)} and {text.count(END)})"
        )

    start = text.index(START)
    end = text.index(END)
    if start > end:
        raise NotesMarkerError(f"{path} has its notes markers in the wrong order")

    return text[:start], text[start + len(START):end], text[end + len(END):]
