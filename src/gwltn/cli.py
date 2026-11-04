"""gwltn console entry point: `gwltn rabbit` runs the LTN actor, the one
systemd unit (`gwltn-rabbit.service`)."""

from __future__ import annotations

import argparse
import logging
import sys
import time

import dotenv

from gwltn.config import GwltnSettings
from gwltn.ltn_actor import LtnActor


def run_rabbit(settings: GwltnSettings) -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    actor = LtnActor(settings=settings)
    actor.start()
    try:
        while actor.main_loop_running:
            time.sleep(1)
    except KeyboardInterrupt:
        actor.stop()


def main(argv: list[str] | None = None) -> None:
    dotenv.load_dotenv(dotenv.find_dotenv())  # populate env BEFORE Settings()
    parser = argparse.ArgumentParser(prog="gwltn")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("rabbit", help="run the LTN actor")
    args = parser.parse_args(argv)
    if args.command == "rabbit":
        run_rabbit(GwltnSettings.load())
