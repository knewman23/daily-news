"""Typed access to config.toml.

Hand-edited settings only. The handle list is machine-written and lives in
config/sources.json, so a bad write there can never corrupt the whisper model
choice or the server port.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILE = "config.toml"


class ConfigError(Exception):
    """config.toml is missing, unreadable, or malformed."""


@dataclass(frozen=True)
class Paths:
    root: Path
    news: Path
    raw: Path
    transcripts: Path
    logs: Path
    sources: Path


@dataclass(frozen=True)
class FetchConfig:
    session_user: str = ""
    first_run_lookback_hours: int = 48
    max_lookback_days: int = 14
    max_retries: int = 3
    backoff_seconds: int = 30
    # "instaloader" talks to the API directly and is what gets flagged;
    # "chrome" reads the same data out of a real logged-in browser.
    backend: str = "instaloader"
    # `localhost`, not `127.0.0.1`: Chrome may bind only [::1], and another
    # browser holding the IPv4 address answers on the same port number.
    cdp_url: str = "http://localhost:9222"
    # 30s was not enough for a cold profile load competing with the feed and
    # story queries -- it aborted a real run. This is a ceiling, not a delay.
    page_timeout_seconds: int = 60


@dataclass(frozen=True)
class TranscribeConfig:
    model: str = "small"
    compute_type: str = "int8"
    min_words: int = 10


@dataclass(frozen=True)
class ServeConfig:
    host: str = "127.0.0.1"
    port: int = 8420


@dataclass(frozen=True)
class PublishConfig:
    enabled: bool = False
    remote: str = "origin"
    branch: str = "main"
    site: str = "site"


@dataclass(frozen=True)
class InterestsConfig:
    """What counts as news worth keeping.

    Plain language rather than keywords: the summarizer is judging meaning, and a
    keyword list would drop a story about Iran that never uses the word.
    """

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetainConfig:
    media_days: int = 3


@dataclass(frozen=True)
class EmailConfig:
    enabled: bool = False
    to: str = ""
    sender: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    keychain_service: str = "daily-news-smtp"
    keychain_account: str = ""
    site_url: str = ""


@dataclass(frozen=True)
class Config:
    paths: Paths
    fetch: FetchConfig
    transcribe: TranscribeConfig
    serve: ServeConfig
    publish: PublishConfig
    email: EmailConfig
    retain: RetainConfig
    interests: InterestsConfig

    def raw_dir(self, day) -> Path:
        return self.paths.raw / day.isoformat()

    def transcripts_dir(self, day) -> Path:
        return self.paths.transcripts / day.isoformat()


def load(path: str | Path = CONFIG_FILE) -> Config:
    p = Path(path)
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {p}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"malformed TOML in {p}: {exc}") from exc

    root = p.resolve().parent
    raw = data.get("paths", {})

    def resolved(key: str, default: str) -> Path:
        return root / str(raw.get(key, default))

    return Config(
        paths=Paths(
            root=root,
            news=resolved("news", "news"),
            raw=resolved("raw", "data/raw"),
            transcripts=resolved("transcripts", "data/transcripts"),
            logs=resolved("logs", "logs"),
            sources=resolved("sources", "config/sources.json"),
        ),
        fetch=_section(FetchConfig, data.get("fetch", {}), "fetch", p),
        transcribe=_section(TranscribeConfig, data.get("transcribe", {}), "transcribe", p),
        serve=_section(ServeConfig, data.get("serve", {}), "serve", p),
        publish=_section(PublishConfig, data.get("publish", {}), "publish", p),
        email=_section(EmailConfig, data.get("email", {}), "email", p),
        retain=_section(RetainConfig, data.get("retain", {}), "retain", p),
        interests=_interests(data.get("interests", {}), p),
    )


def _interests(values: dict, path: Path) -> InterestsConfig:
    """Lists arrive from TOML as lists; the dataclass is frozen, so tuples."""
    section = _section(InterestsConfig, values, "interests", path)
    return InterestsConfig(
        include=tuple(section.include or ()),
        exclude=tuple(section.exclude or ()),
    )


def _section(cls, values: dict, name: str, path: Path):
    """Build one config section, rejecting keys the code does not read.

    A typo'd key would otherwise be silently ignored and the default used, which
    looks like the setting simply having no effect.
    """
    fields = set(cls.__dataclass_fields__)
    unknown = set(values) - fields
    if unknown:
        raise ConfigError(
            f"{path}: unknown key(s) in [{name}]: {', '.join(sorted(unknown))}"
        )
    return cls(**values)
