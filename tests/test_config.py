"""The LTN's settings: the bid lead is seconds before the slot, inside one
slot."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from gwltn.config import GwltnSettings
from gwltn.market import MARKET_SLOT_S

REQUIRED = {
    "super_alias": "d1.super1",
    "time_coordinator_alias": "d1.time",
    "flo_alias": "d1.flo",
}


def settings(**kwargs: Any) -> GwltnSettings:
    """Settings from the env and the given fields; the test runs in an
    empty cwd so no `.env` is read."""
    return GwltnSettings(**REQUIRED, **kwargs)  # pyright: ignore[reportCallIssue]


@pytest.fixture(autouse=True)
def empty_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GWLTN_BID_LEAD_S", raising=False)


def test_bid_lead_defaults_to_three_minutes() -> None:
    assert settings().bid_lead_s == 180


def test_bid_lead_reads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GWLTN_BID_LEAD_S", "120")
    assert settings().bid_lead_s == 120


@pytest.mark.parametrize("lead_s", [0, -1, MARKET_SLOT_S, MARKET_SLOT_S + 1])
def test_bid_lead_outside_one_slot_is_refused(lead_s: int) -> None:
    with pytest.raises(ValidationError):
        settings(bid_lead_s=lead_s)
