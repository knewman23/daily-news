#!/usr/bin/env python3
"""Start the local web app.

    .venv/bin/python serve.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace

from src import config, serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the daily news digests.")
    parser.add_argument("--config", default=config.CONFIG_FILE)
    parser.add_argument("--port", type=int, default=None, help="Override the configured port.")
    parser.add_argument(
        "--host",
        default=None,
        help=(
            "Override the configured bind address. Only for running inside a "
            "container, where 127.0.0.1 is reachable from nothing: bind 0.0.0.0 "
            "there and publish the port on loopback instead."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    cfg = config.load(args.config)
    if args.port or args.host:
        cfg = replace(
            cfg,
            serve=config.ServeConfig(
                host=args.host or cfg.serve.host,
                port=args.port or cfg.serve.port,
            ),
        )

    serve.run(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
