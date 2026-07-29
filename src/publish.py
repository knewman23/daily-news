"""Publish the day's news after a successful run.

Exports the static site and pushes it, so the digest reaches the hosted page
without anyone touching a terminal.

Three rules keep this from being dangerous.

**Only `site/` is ever staged.** A bare `git add -A` from an unattended job would
sweep up whatever happened to be in the working tree — a half-finished edit, a
stray credential — and push it. The pathspec is explicit and nothing else is
committed. Notes are safe by construction too: they live in `news/`, which is
git-ignored, and the export never renders them.

**Nothing is committed when nothing changed.** Otherwise every run adds an empty
commit and the history stops meaning anything.

**A publish failure never fails the run.** By this point the digest is written and
readable locally; being unable to reach GitHub is worth reporting, not worth
throwing away a successful run over.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Sequence

log = logging.getLogger(__name__)

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass
class PublishResult:
    ok: bool
    published: bool          # False when there was simply nothing new
    message: str


class GitError(Exception):
    """A git command failed."""


def publish(
    cfg,
    day: date,
    topic_count: int = 0,
    runner: Runner = subprocess.run,
    exporter: Callable[..., int] | None = None,
) -> PublishResult:
    """Export the site and push it. Never raises."""
    if not cfg.publish.enabled:
        return PublishResult(True, False, "publishing is disabled in config.toml")

    root = cfg.paths.root
    site = (root / cfg.publish.site).resolve()

    try:
        build = exporter or _default_exporter
        days = build(cfg.paths.news, root / "web", site, cfg.paths.logs)
        log.info("exported %d day(s) to %s", days, site)
    except Exception as exc:
        log.exception("static export failed: %s", exc)
        return PublishResult(False, False, f"export failed: {exc}")

    try:
        relative = site.relative_to(root).as_posix()
    except ValueError:
        return PublishResult(
            False, False,
            f"the site directory {site} is outside the repository at {root}",
        )

    try:
        _git(runner, root, "add", "--", relative)

        if not _has_staged_changes(runner, root, relative):
            log.info("site is unchanged, nothing to publish")
            return PublishResult(True, False, "no change to publish")

        summary = f"news: {day.isoformat()}"
        if topic_count:
            summary += f" ({topic_count} topic{'s' if topic_count != 1 else ''})"

        _git(runner, root, "commit", "-m", summary, "--", relative)
        _git(runner, root, "push", cfg.publish.remote,
             f"HEAD:{cfg.publish.branch}")
    except GitError as exc:
        log.error("could not publish: %s", exc)
        return PublishResult(False, False, str(exc))

    log.info("published %s to %s/%s", relative, cfg.publish.remote, cfg.publish.branch)
    return PublishResult(True, True, f"published {day.isoformat()}")


# --- internals -------------------------------------------------------------


def _git(runner: Runner, root: Path, *args: str) -> str:
    completed = runner(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise GitError(f"git {args[0]} failed: {detail[:400]}")
    return completed.stdout or ""


def _has_staged_changes(runner: Runner, root: Path, pathspec: str) -> bool:
    """True when the staged tree differs from HEAD for this path.

    `git diff --cached --quiet` exits 1 when there are differences, so a
    non-zero code here is the signal, not a failure.
    """
    completed = runner(
        ["git", "-C", str(root), "diff", "--cached", "--quiet", "--", pathspec],
        capture_output=True, text=True,
    )
    return completed.returncode != 0


def _default_exporter(news: Path, web: Path, out: Path, logs: Path) -> int:
    """Imported lazily so importing publish does not pull in the exporter."""
    import export_static

    return export_static.export(news, web, out, logs)
