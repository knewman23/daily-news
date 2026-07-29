"""The localhost web app.

Binds loopback only. There is no authentication and none is wanted: the server
writes to the user's own files and is reachable from nowhere else.

Two rules shape the request handling.

Every date in a URL is matched against a strict `YYYY-MM-DD` pattern and the
resolved path is checked to be inside the news directory before anything is
read. `/api/day/../../etc/passwd` is a rejection, not a file read.

The malformed-notes case is a 409 rather than a best-effort append. If a hand-edit
broke a file's marker block, silently guessing where the notes belong risks
writing over the generated summary — which the user cannot get back.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from src import digest, notes, runlog, sources

log = logging.getLogger(__name__)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HANDLE_RE = re.compile(r"^[A-Za-z0-9._@%-]{1,64}$")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class ApiError(Exception):
    """A failure with an HTTP status the browser should see."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def build_server(
    news_dir: str | Path,
    sources_path: str | Path,
    web_dir: str | Path,
    host: str = "127.0.0.1",
    port: int = 8420,
    lookup: Callable[[str], Any] | None = None,
    logs_dir: str | Path | None = None,
) -> ThreadingHTTPServer:
    """Assemble the server. Port 0 binds an ephemeral port, which the tests use."""
    news = Path(news_dir)
    web = Path(web_dir)
    config_path = Path(sources_path)
    logs = Path(logs_dir) if logs_dir else Path("logs")
    verify = lookup if lookup is not None else _default_lookup

    class Handler(_Handler):
        news_dir = news
        web_dir = web
        sources_path = config_path
        logs_dir = logs
        lookup = staticmethod(verify)

    return ThreadingHTTPServer((host, port), Handler)


def serve_in_thread(
    httpd: ThreadingHTTPServer,
    poll_interval: float = 0.05,
) -> threading.Thread:
    """Serve on a background thread.

    The poll interval is how long shutdown() can block, so the stdlib default of
    half a second makes a per-test server teardown the slowest thing in the suite.
    """
    thread = threading.Thread(
        target=httpd.serve_forever, args=(poll_interval,), daemon=True,
    )
    thread.start()
    return thread


def run(cfg, web_dir: str | Path = "web") -> None:
    httpd = build_server(
        news_dir=cfg.paths.news,
        sources_path=cfg.paths.sources,
        web_dir=web_dir,
        host=cfg.serve.host,
        port=cfg.serve.port,
        logs_dir=cfg.paths.logs,
    )
    url = f"http://{cfg.serve.host}:{cfg.serve.port}"
    print(f"Daily News reading from {cfg.paths.news}")
    print(f"Open {url}  (ctrl-c to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()


class _Handler(BaseHTTPRequestHandler):
    news_dir: Path
    web_dir: Path
    sources_path: Path
    logs_dir: Path
    lookup: Callable[[str], Any]

    server_version = "daily-news"

    # --- routing ----------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch({
            "/api/days": self._days,
            "/api/tags": self._tags,
            "/api/search": self._search,
            "/api/sources": self._list_sources,
            "/api/runs": self._runs,
        }, self._static)

    def do_POST(self) -> None:
        self._dispatch({"/api/sources": self._add_source}, self._not_found)

    def do_PATCH(self) -> None:
        self._dispatch({}, self._not_found)

    def do_DELETE(self) -> None:
        self._dispatch({}, self._not_found)

    def _dispatch(self, exact: dict, fallback: Callable[[str], None]) -> None:
        path = urlparse(self.path).path
        try:
            if path in exact:
                exact[path]()
                return

            for prefix, handler in (
                ("/api/day/", self._day),
                ("/api/notes/", self._save_notes),
                ("/api/sources/", self._source_action),
                ("/api/log/", self._run_log),
            ):
                if path.startswith(prefix):
                    handler(path[len(prefix):])
                    return

            fallback(path)
        except ApiError as exc:
            self._json({"error": exc.message}, exc.status)
        except Exception as exc:                        # pragma: no cover
            log.exception("unhandled error for %s", path)
            self._json({"error": str(exc)}, 500)

    # --- days -------------------------------------------------------------

    def _days(self) -> None:
        self._json({"days": [
            {
                "date": day.date.isoformat(),
                "tags": day.tags,
                "sources": day.sources,
                "post_count": day.post_count,
                "transcribed_count": day.transcribed_count,
                "incomplete": day.incomplete,
            }
            for day in digest.list_days(self.news_dir)
        ]})

    def _day(self, raw: str) -> None:
        path = self._day_path(raw)
        meta = next(
            (d for d in digest.list_days(self.news_dir) if d.path == path), None
        )
        try:
            journal = notes.read_notes(path)
        except notes.NotesMarkerError as exc:
            log.warning("%s has a broken notes block: %s", path, exc)
            journal = ""

        self._json({
            "date": raw,
            "html": digest.render_html(path),
            "notes": journal,
            "tags": meta.tags if meta else [],
            "sources": meta.sources if meta else [],
            "post_count": meta.post_count if meta else 0,
            "transcribed_count": meta.transcribed_count if meta else 0,
            "incomplete": meta.incomplete if meta else False,
        })

    def _tags(self) -> None:
        self._json({"tags": digest.all_tags(self.news_dir)})

    def _search(self) -> None:
        params = parse_qs(urlparse(self.path).query)
        hits = digest.search(
            self.news_dir,
            (params.get("q") or [""])[0],
            (params.get("tag") or [""])[0],
        )
        self._json({"hits": [
            {
                "date": hit.date.isoformat(),
                "headline": hit.headline,
                "tags": hit.tags,
                "snippet": hit.snippet,
            }
            for hit in hits
        ]})

    # --- runs -------------------------------------------------------------

    def _runs(self) -> None:
        runs = runlog.load(self.logs_dir)
        self._json({
            "runs": runs,
            "problems": sum(1 for r in runs if not r.get("ok") or r.get("incomplete")),
        })

    def _run_log(self, raw: str) -> None:
        """The raw log for one date. Validated like a digest date, for the same reason."""
        day = unquote(raw).strip("/")
        if not DATE_RE.match(day):
            raise ApiError(400, f"expected a YYYY-MM-DD date, got {day!r}")

        path = (self.logs_dir / f"{day}.log").resolve()
        if not _inside(path, self.logs_dir):
            raise ApiError(400, "path escapes the log directory")

        text = runlog.read_log(self.logs_dir, day)
        if not text:
            raise ApiError(404, f"no log for {day}")
        self._json({"date": day, "log": text})

    # --- notes ------------------------------------------------------------

    def _save_notes(self, raw: str) -> None:
        if self.command != "POST":
            raise ApiError(405, "use POST to save notes")

        path = self._day_path(raw)
        body = self._body()
        text = body.get("notes")
        if not isinstance(text, str):
            raise ApiError(400, "body must be {\"notes\": \"...\"}")

        try:
            notes.write_notes(path, text)
        except notes.NotesMarkerError as exc:
            # Never guess where the block belongs: a wrong guess overwrites news
            # the user cannot recover.
            raise ApiError(409, f"the notes block in this file is malformed: {exc}")

        self._json({"ok": True, "notes": notes.read_notes(path)})

    # --- sources ----------------------------------------------------------

    def _list_sources(self) -> None:
        self._json({"sources": [
            {
                "handle": s.handle,
                "enabled": s.enabled,
                "added": s.added,
                "last_pull_at": s.last_pull_at,
                "last_seen": s.last_seen,
            }
            for s in sources.load(self.sources_path)
        ]})

    def _add_source(self) -> None:
        handle = self._body().get("handle")
        if not isinstance(handle, str):
            raise ApiError(400, "body must be {\"handle\": \"...\"}")

        try:
            record = sources.add(self.sources_path, handle, lookup=self.lookup)
        except ValueError as exc:
            raise ApiError(400, str(exc))
        except sources.DuplicateHandle as exc:
            raise ApiError(409, str(exc))
        except sources.LookupFailed as exc:
            # The reason belongs in the UI, not only the log — otherwise a typo
            # looks like the button simply not working.
            raise ApiError(400, str(exc))

        self._json({"ok": True, "source": {
            "handle": record.handle,
            "enabled": record.enabled,
            "added": record.added,
            "last_pull_at": record.last_pull_at,
            "last_seen": record.last_seen,
        }})

    def _source_action(self, raw: str) -> None:
        handle = unquote(raw)
        if not HANDLE_RE.match(handle):
            raise ApiError(400, "invalid handle")

        try:
            if self.command == "DELETE":
                sources.remove(self.sources_path, handle)
            elif self.command == "PATCH":
                enabled = self._body().get("enabled")
                if not isinstance(enabled, bool):
                    raise ApiError(400, "body must be {\"enabled\": true|false}")
                sources.set_enabled(self.sources_path, handle, enabled)
            else:
                raise ApiError(405, "use PATCH or DELETE")
        except sources.UnknownHandle as exc:
            raise ApiError(404, str(exc))
        except ValueError as exc:
            raise ApiError(400, str(exc))

        self._json({"ok": True})

    # --- static -----------------------------------------------------------

    def _static(self, path: str) -> None:
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (self.web_dir / relative).resolve()

        if not _inside(target, self.web_dir) or not target.is_file():
            self._not_found(path)
            return

        body = target.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream"),
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self, path: str) -> None:
        self._json({"error": f"not found: {path}"}, 404)

    # --- helpers ----------------------------------------------------------

    def _day_path(self, raw: str) -> Path:
        """Resolve a date segment to a digest file, or raise.

        Validating the shape and then re-checking containment covers both a
        malformed date and an encoded traversal that survives unquoting.
        """
        day = unquote(raw).strip("/")
        if not DATE_RE.match(day):
            raise ApiError(400, f"expected a YYYY-MM-DD date, got {day!r}")
        try:
            date.fromisoformat(day)
        except ValueError:
            raise ApiError(400, f"not a real date: {day!r}")

        path = (self.news_dir / f"{day}.md").resolve()
        if not _inside(path, self.news_dir):
            raise ApiError(400, "path escapes the news directory")
        if not path.is_file():
            raise ApiError(404, f"no digest for {day}")
        return path

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            raise ApiError(400, "expected a JSON body")
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ApiError(400, f"malformed JSON: {exc}")
        if not isinstance(payload, dict):
            raise ApiError(400, "expected a JSON object")
        return payload

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def _default_lookup(handle: str) -> None:
    """Verify a handle against Instagram. Imported lazily to keep tests offline."""
    from src import config, fetch

    cfg = config.load()
    fetch.lookup_profile(fetch.make_loader(cfg.fetch), handle)
