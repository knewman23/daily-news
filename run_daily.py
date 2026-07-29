#!/usr/bin/env python3
"""The 11am run. Entry point for launchd.

fetch -> transcribe -> ocr -> summarize, in that order. Every stage is keyed on
output existence, so re-running a day redoes only what is missing: a failure at
the summarize step costs seconds to retry rather than a fresh round of downloads
and whisper passes.

Two invariants matter more than the happy path.

A day always gets a file when there is anything to say about it, even if that
file says "no posts found" — otherwise a silently broken pipeline is
indistinguishable from a quiet news day.

And a re-run never costs the user their journal entry. summarize writes a fresh
file with empty note markers, so the existing notes are read first and written
back afterwards.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from src import config, fetch, notes, ocr, sources, summarize, transcribe
from src.records import Stats

log = logging.getLogger("daily-news")


def run_day(
    day: date,
    cfg: config.Config,
    fetcher=None,
    transcriber=None,
    ocr_runner=None,
    summarizer=None,
    notifier=None,
    generated: datetime | None = None,
) -> int:
    """Run one day end to end. Returns a process exit code."""
    _setup_logging(cfg, day)
    notify = notifier or _notify
    do_fetch = fetcher or fetch.fetch_day
    do_transcribe = transcriber or transcribe.transcribe_day
    do_ocr = ocr_runner or ocr.ocr_day
    do_summarize = summarizer or summarize.summarize_day

    log.info("starting run for %s", day.isoformat())

    enabled = sources.enabled_sources(cfg.paths.sources)
    if not enabled:
        log.info("every source is disabled, nothing to do")
        return 0

    raw = cfg.raw_dir(day)
    extracted = cfg.transcripts_dir(day)

    try:
        _, fetch_stats = do_fetch(cfg.paths.sources, raw, cfg.fetch)
    except fetch.SessionExpired as exc:
        log.error("Instagram session is not usable: %s", exc)
        log.error(
            "Re-authenticate with:\n"
            "  .venv/bin/instaloader --load-cookies chrome "
            "--sessionfile ~/.config/instaloader/session-%s",
            cfg.fetch.session_user,
        )
        notify("Instagram session expired — re-authenticate")
        return 1
    except Exception as exc:
        log.exception("fetch failed outright: %s", exc)
        notify(f"Daily news fetch failed: {exc}")
        return 1

    spoken, audio_stats = do_transcribe(raw, extracted, cfg.transcribe)
    on_image, image_stats = do_ocr(raw, extracted, cfg.transcribe)

    transcripts = list(spoken) + list(on_image)
    stats = _merge(fetch_stats, audio_stats, image_stats)
    log.info(
        "%d post(s), %d with usable text (%d spoken, %d on-image)",
        stats.post_count, stats.transcribed_count, len(spoken), len(on_image),
    )

    path = cfg.paths.news / f"{day.isoformat()}.md"
    carried = _existing_notes(path)

    try:
        do_summarize(day, transcripts, stats, cfg.paths.news, generated=generated)
    except Exception as exc:
        log.exception("summarize failed: %s", exc)
        log.error("transcripts are on disk, so re-running this date is cheap")
        notify(f"Daily news summary failed: {exc}")
        return 1

    if carried:
        notes.write_notes(path, carried)
        log.info("carried %d character(s) of journal notes into the new file", len(carried))

    for note in stats.notes:
        log.warning("partial failure: %s", note)

    log.info("wrote %s%s", path, " (incomplete)" if stats.incomplete else "")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build one day's news digest.")
    parser.add_argument(
        "--date", type=_a_date, default=date.today(),
        help="YYYY-MM-DD, for backfill or a re-run. Defaults to today.",
    )
    parser.add_argument(
        "--config", default=config.CONFIG_FILE, help="Path to config.toml.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_day(args.date, config.load(args.config))


# --- internals -------------------------------------------------------------


def _merge(*parts: Stats) -> Stats:
    """Combine stage stats.

    post_count comes from fetch alone — the later stages each count the files
    they looked at, so summing all of them would report every post two or three
    times over.
    """
    merged = Stats(post_count=parts[0].post_count if parts else 0)
    for part in parts:
        merged.transcribed_count += part.transcribed_count
        merged.incomplete = merged.incomplete or part.incomplete
        merged.notes.extend(part.notes)
    return merged


def _existing_notes(path: Path) -> str:
    """Read the journal block before it is overwritten.

    A malformed block must not abort the run: the day's news is worth writing
    even when a hand-edit broke the markers. The failure is logged loudly because
    it does mean notes are being dropped.
    """
    if not path.exists():
        return ""
    try:
        return notes.read_notes(path)
    except notes.NotesMarkerError as exc:
        log.error("could not read existing notes from %s, they will be lost: %s", path, exc)
        return ""


def _notify(message: str) -> None:
    """Surface a failure on the desktop.

    An unattended run that fails silently is worse than one that fails loudly:
    the first sign of trouble would otherwise be a digest that never appeared.
    """
    subprocess.run(
        ["osascript", "-e",
         f'display notification {message!r} with title "Daily News"'],
        check=False, capture_output=True,
    )


def _setup_logging(cfg: config.Config, day: date) -> None:
    cfg.paths.logs.mkdir(parents=True, exist_ok=True)
    logfile = cfg.paths.logs / f"{day.isoformat()}.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Re-invoked in-process by the tests; without this each run stacks another
    # pair of handlers and every line is written several times over.
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    for handler in (logging.FileHandler(logfile, encoding="utf-8"),
                    logging.StreamHandler(sys.stdout)):
        handler.setFormatter(fmt)
        root.addHandler(handler)


def _a_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}")


if __name__ == "__main__":
    sys.exit(main())
