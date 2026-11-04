"""Shared fixtures: the actor writes under XDG dirs, so tests point those
at a temp dir before any settings or actor exists; the dev identity and
the willow test pair stand in for a provisioned house."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from gwltn.config import GwltnSettings

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config"
DEV_IDENTITY = HERE.parent / "scripts" / "dev-g.node.gt.json"
LTN_ALIAS = "d1.isone.ct.newhaven.willow1"
SCADA_ALIAS = "d1.isone.ct.newhaven.willow1.scada"
OTHER_SCADA_ALIAS = "d1.isone.ct.newhaven.orange1.scada"
FLO_ALIAS = "d1.flo"


@pytest.fixture(autouse=True)
def xdg_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.setenv("GWLTN_SERVICE_ALIAS", LTN_ALIAS)
    monkeypatch.setenv("GWLTN_G_NODE_PATH", str(DEV_IDENTITY))
    monkeypatch.setenv(
        "GWLTN_HARDWARE_LAYOUT", str(CONFIG / "gw.house0.willow.layout.json")
    )
    monkeypatch.setenv(
        "GWLTN_OPERATIONAL_PARAMS",
        str(CONFIG / "gw.house0.willow.operational.params.json"),
    )
    monkeypatch.setenv("GWLTN_DATA_PATH", str(tmp_path / "data"))
    monkeypatch.setenv("GWLTN_WEATHER_SOURCE", "Sim")
    monkeypatch.setenv("GWLTN_SUPER_ALIAS", "d1.super1")
    monkeypatch.setenv("GWLTN_TIME_COORDINATOR_ALIAS", "d1.time")
    monkeypatch.setenv("GWLTN_FLO_ALIAS", FLO_ALIAS)
    return tmp_path


def broker_up(port: int) -> bool:
    """Whether the gwbase dev broker answers on this localhost port."""
    try:
        with socket.create_connection(("localhost", port), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture
def settings() -> GwltnSettings:
    return GwltnSettings.load()
