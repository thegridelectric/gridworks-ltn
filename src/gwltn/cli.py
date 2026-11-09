"""gwltn console entry point: `gwltn rabbit` runs the LTN actor, the one
systemd unit (`gwltn-rabbit.service`)."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from zoneinfo import ZoneInfo

import dotenv
from gwbase.logging_setup import HumanFormatter

from gwltn.config import GwltnSettings
from gwltn.ltn_actor import LtnActor


class LoggerNameAsAlias(logging.Filter):
    """On the stdout stream the alias slot of the gwbase line names the
    logger (``gwltn.ltn_actor``), since these records come from no actor."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.service_alias = record.name
        return True


def run_rabbit(settings: GwltnSettings) -> None:
    # The stdout stream (journald) in the same line format and zone as the
    # actor's file log, so the two agree on the hour.
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(HumanFormatter(ZoneInfo(settings.log_tz)))
    stdout.addFilter(LoggerNameAsAlias())
    logging.basicConfig(level=settings.log_level.upper(), handlers=[stdout])
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
