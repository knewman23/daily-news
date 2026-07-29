#!/usr/bin/env python3
"""Start the local web app.

    .venv/bin/python serve.py
"""

from __future__ import annotations

import argparse
import logging
import sys

from src import config, serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the daily news digests.")
    parser.add_argument("--config", default=config.CONFIG_FILE)
    parser.add_argument("--port", type=int, default=None, help="Override the configured port.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    cfg = config.load(args.config)
    if args.port:
        cfg = config.Config(
            paths=cfg.paths, fetch=cfg.fetch, transcribe=cfg.transcribe,
            serve=config.ServeConfig(host=cfg.serve.host, port=args.port),
        )

    serve.run(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
