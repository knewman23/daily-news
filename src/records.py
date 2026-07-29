"""Records passed between pipeline stages.

Kept in a module of its own with no heavy imports: transcribe.py pulls in
faster-whisper and fetch.py pulls in instaloader, so anything that needs to
name a Transcript or a PostRef would otherwise drag a model loader or an HTTP
client into a unit test that has no use for either.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PostRef:
    handle: str
    shortcode: str
    posted_at: str
    permalink: str
    caption: str = ""


@dataclass(frozen=True)
class Transcript:
    handle: str
    shortcode: str
    text: str
    caption: str = ""
    permalink: str = ""
    posted_at: str | None = None


@dataclass
class Stats:
    """Counts for one day's run, and whether it can be trusted as complete."""

    post_count: int = 0
    transcribed_count: int = 0
    incomplete: bool = False
    notes: list[str] = field(default_factory=list)

    def fail(self, note: str) -> None:
        """Record a partial failure. Anything that calls this makes the day incomplete."""
        self.incomplete = True
        self.notes.append(note)

    def as_dict(self) -> dict[str, object]:
        return {
            "post_count": self.post_count,
            "transcribed_count": self.transcribed_count,
            "incomplete": self.incomplete,
        }
