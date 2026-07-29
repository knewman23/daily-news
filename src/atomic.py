"""Crash-safe file writes.

Both the source list and the journal notes are rewritten in place while the
user may be reading them. A plain truncate-and-write leaves a partial file if
the process dies mid-write; writing to a sibling temp file and renaming does
not, because os.replace is atomic within a filesystem.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def write_text(path: str | os.PathLike[str], text: str) -> None:
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def write_json(path: str | os.PathLike[str], data: Any) -> None:
    write_text(path, json.dumps(data, indent=2) + "\n")
