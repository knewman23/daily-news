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
class Config:
    paths: Paths
    fetch: FetchConfig
    transcribe: TranscribeConfig
    serve: ServeConfig

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
