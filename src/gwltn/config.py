"""Settings for gridworks-ltn."""

import logging
from pathlib import Path

from gwbase.config import GNodeSettings
from gwbase.config.paths import config_dir, data_dir
from gwbase.transport_format import LeftRightDot
from pydantic import Field
from pydantic_settings import SettingsConfigDict

from gwltn.weather_source import WeatherSourceKind

SERVICE_NAME = "ltn"
LAYOUT_FILE = "hardware-layout.json"
OPS_PARAMS_FILE = "operational-params.json"


class GwltnSettings(GNodeSettings):
    """Reads from env (GWLTN_*) and/or a `.env` file at the repo root.

    Inherits `rabbit: RabbitBrokerClient`, `g_node_path`, `log_level` and
    the rest of `ServiceSettings` from `GNodeSettings`. `service_alias`
    MUST match the g.node.gt.json Alias; `GridworksActor` enforces the
    binding at boot. The transport class is intrinsic to the LTN's role
    and is passed up by `LtnActor` itself.
    """

    service_alias: LeftRightDot = "d1.isone.me.versant.keene.beech.ltn"
    service_name: str = SERVICE_NAME  # XDG path segment for logs/state

    # The Orchestrator tier's control plane: whose heartbeats to answer and
    # whose simulated time to follow. No defaults: each universe names its
    # own.
    super_alias: LeftRightDot
    time_coordinator_alias: LeftRightDot
    # The FLO GNode this LTN sends its `flo.params` to and takes its
    # `flo.recommendation` from. No default: each universe names its own.
    flo_alias: LeftRightDot

    # The house's two authored artifacts, the layout and its operational
    # params, in the LTN's config dir; and the data dir where the LTN
    # keeps its contract, previous run, price forecast and saved events.
    hardware_layout: Path = Field(
        default_factory=lambda: config_dir(SERVICE_NAME) / LAYOUT_FILE
    )
    operational_params: Path = Field(
        default_factory=lambda: config_dir(SERVICE_NAME) / OPS_PARAMS_FILE
    )
    data_path: Path = Field(default_factory=lambda: data_dir(SERVICE_NAME))

    timezone_str: str = "America/New_York"
    save_events: bool = False
    fuel_substitution: bool = False
    fuel_sub_usd_per_mwh: int = 490  # hack until we account for COP etc
    contract_rep_logging_level: int = logging.INFO
    flo_logging_level: int = logging.INFO
    flo_horizon_hours: int = 48
    # Count no buffer energy when true.
    keep_buffer_full: bool = False
    weather_source: WeatherSourceKind = WeatherSourceKind.Gwwf
    weather_api_url: str = "https://forecast.electricity.works/hw1-isone-weather"
    weather_pull_timeout_s: float = 20

    model_config = SettingsConfigDict(
        env_prefix="GWLTN_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def load(cls) -> "GwltnSettings":
        """Settings from env and `.env`. The fields with no default are what
        pyright reads as missing constructor arguments; the settings
        sources supply them, so this is the one place that call is made."""
        return cls()  # pyright: ignore[reportCallIssue]
