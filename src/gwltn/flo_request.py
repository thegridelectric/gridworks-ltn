"""The LTN's request to its FLO: one flo.params built from the house's
facts (the ops word, the layout's water store, the operating status, the
forecast and the store's measured state)."""

import json
from pathlib import Path
from typing import NamedTuple, Optional, Sequence

import numpy as np

from gwltn.market import MARKET_SLOT_S
from gwltn.sema.enums import SeasonalStorageMode, WeatherForecastFidelity
from gwltn.sema.property_format import LeftRightDot, UTCMilliseconds, UTCSeconds
from gwltn.sema.types import (
    FloOptimalPath,
    FloParams,
    HeatingCurve,
    OperationalParams,
    WaterStore,
)
from gwltn.weather_source import FORECAST_HOURS, ForecastPair, HourlyForecast

MAX_HORIZON_HOURS = 48
STEP_MINUTES = 60

# The market's price bounds. No word states them yet; the MarketMaker's
# product word retires these two constants.
PRICE_MAX_USD_MWH = 2000.0
PRICE_MIN_USD_MWH = -100.0


class SeasonalFillForecast(ValueError):
    """The forecast pair is a seasonal fill, so the LTN runs no FLO on it."""


class StoreState(NamedTuple):
    """The store as measured when the request is built: the three-layer
    temperatures, the thermoclines as sensor-layer counts from the top,
    and whether the heat pump is running."""

    top_f: float
    middle_f: float
    bottom_f: float
    thermocline1: int
    thermocline2: int
    hp_on: bool


class PriceForecast(NamedTuple):
    """The hourly price forecast the request is built on, each list one
    value per hour from the coming slot, in USD/MWh. Missing word: the
    price service's forecast word, decoded instead of this."""

    dp_usd_per_mwh: list[float]
    lmp_usd_per_mwh: list[float]


class PreviousRun(NamedTuple):
    """The path the previous run's cleared price led to, with the price
    that run forecast for the slot after it. No word states the pair yet;
    a `flo.previous.run` word retires the file form below."""

    path: FloOptimalPath
    elec_usd_mwh_now: float

    def ends_at(self) -> int:
        """When the path's first step ends: the start of the slot the run
        is the previous run for."""
        return self.path.start_unix_s + self.path.step_duration_minutes[0] * 60


def previous_run_for(
    run: Optional[PreviousRun], start_unix_s: int
) -> Optional[PreviousRun]:
    """The run when its first step ends at `start_unix_s`, else nothing: a
    run from any other slot is no previous run for this one."""
    if run is None or run.ends_at() != start_unix_s:
        return None
    return run


def save_previous_run(file: Path, run: PreviousRun) -> None:
    """The run on disk, the path as its word."""
    file.write_text(
        json.dumps({
            "Path": run.path.to_dict(),
            "ElecUsdMwhNow": run.elec_usd_mwh_now,
        })
    )


def load_previous_run(file: Path) -> Optional[PreviousRun]:
    """The run saved by `save_previous_run`, or nothing when there is none."""
    if not file.exists():
        return None
    data = json.loads(file.read_text())
    return PreviousRun(
        path=FloOptimalPath.from_dict(data["Path"]),
        elec_usd_mwh_now=float(data["ElecUsdMwhNow"]),
    )


def next_slot_start_s(now_s: float) -> int:
    """The start of the market slot after the one `now_s` is in, in UTC
    seconds: whole slots of the epoch, so a clock change in the plant's
    timezone does not move it."""
    return (int(now_s) // MARKET_SLOT_S + 1) * MARKET_SLOT_S


def seconds_to_slot_start(now_s: float) -> int:
    """Whole seconds from `now_s` to the start of the next market slot."""
    return next_slot_start_s(now_s) - int(now_s)


def live_forecast(forecast: ForecastPair) -> ForecastPair:
    """The pair itself when every series is live or stored; a seasonal
    fill is refused."""
    filled = [
        s.quantity.value
        for s in forecast.message.series_list
        if s.fidelity is WeatherForecastFidelity.SeasonalTemplate
    ]
    if filled:
        raise SeasonalFillForecast(
            f"Forecast {forecast.message.forecast_uid} on {forecast.message.bundle_name} "
            f"is a seasonal fill ({', '.join(filled)})"
        )
    return forecast


def load_forecast_kwh(
    curve: HeatingCurve, weather: HourlyForecast, buffer_available_kwh: float
) -> list[float]:
    """The house's hourly heat load from the heating curve, with the
    buffer's available energy taken off the first hours (or, when the
    buffer is short, added to the first)."""
    alpha = curve.alpha_times10 / 10
    beta = curve.beta_times100 / 100
    gamma = curve.gamma_ex6 / 1e6
    load = [
        max(0.0, alpha + beta * oat + gamma * ws * (65 - oat))
        for oat, ws in zip(weather.oat_f, weather.wind_speed_mph, strict=True)
    ]
    remaining = buffer_available_kwh
    if remaining < 0:
        load[0] += -remaining
    else:
        i = 0
        while remaining > 0 and i < len(load):
            taken = min(remaining, load[i])
            load[i] -= taken
            remaining -= taken
            i += 1
    return load


def rswt_forecast_f(curve: HeatingCurve, load_kwh: Sequence[float]) -> list[float]:
    """The required source water temperature per hour, from the heating
    curve's quadratic through its three power points."""
    no_power_rswt = -(curve.alpha_times10 / 10) / (curve.beta_times100 / 100)
    x_rswt = np.array([no_power_rswt, curve.intermediate_rswt_f, curve.dd_rswt_f])
    y_power = np.array([0, curve.intermediate_power_kw, curve.dd_power_kw])
    A = np.vstack([x_rswt**2, x_rswt, np.ones_like(x_rswt)]).T
    a, b, c = np.linalg.lstsq(A, y_power, rcond=None)[0]
    return [
        round((-b + (b**2 - 4 * a * (c - load)) ** 0.5) / (2 * a), 2)
        for load in load_kwh
    ]


def build_flo_params(
    *,
    ops: OperationalParams,
    water_store: WaterStore,
    storage_mode: SeasonalStorageMode,
    forecast: ForecastPair,
    now_s: float,
    horizon_hours: int,
    lmp_usd_mwh: Sequence[float],
    dist_usd_mwh: Sequence[float],
    store: StoreState,
    buffer_available_kwh: float,
    g_node_alias: LeftRightDot,
    start_unix_s: UTCSeconds,
    created_ms: UTCMilliseconds,
    previous: Optional[PreviousRun] = None,
) -> FloParams:
    """The request for one run: the horizon is the shortest of what was
    asked, the cap, the forecast and the prices; the store modeled is the
    whole store in AllTanks and one tank in BufferOnly."""
    weather = live_forecast(forecast).next_hours(FORECAST_HOURS, now_s)
    horizon = min(
        horizon_hours,
        MAX_HORIZON_HOURS,
        len(weather.time),
        len(lmp_usd_mwh),
        len(dist_usd_mwh),
    )
    weather = HourlyForecast(
        time=weather.time[:horizon],
        oat_f=weather.oat_f[:horizon],
        wind_speed_mph=weather.wind_speed_mph[:horizon],
    )
    load = load_forecast_kwh(ops.heating_curve, weather, buffer_available_kwh)
    tanks_modeled = (
        water_store.total_store_tanks
        if storage_mode is SeasonalStorageMode.AllTanks
        else 1
    )
    return FloParams(
        g_node_alias=g_node_alias,
        created_ms=created_ms,
        start_unix_s=start_unix_s,
        step_duration_minutes=[STEP_MINUTES] * horizon,
        lmp_usd_mwh_list=[float(p) for p in lmp_usd_mwh[:horizon]],
        dist_usd_mwh_list=[float(p) for p in dist_usd_mwh[:horizon]],
        oat_f_list=[float(t) for t in weather.oat_f],
        load_kwh_list=load,
        rswt_f_list=rswt_forecast_f(ops.heating_curve, load),
        weather_forecast_id=forecast.message.forecast_uid,
        seasonal_storage_mode=storage_mode,
        tanks_modeled=tanks_modeled,
        gallons_per_tank=water_store.gallons_per_tank,
        temp_sensors_per_tank=water_store.temp_sensors_per_tank,
        max_tank_temp_f=float(ops.heating_curve.max_ewt_f),
        initial_top_temp_f=float(store.top_f),
        initial_middle_temp_f=float(store.middle_f),
        initial_bottom_temp_f=float(store.bottom_f),
        initial_thermocline1=store.thermocline1,
        initial_thermocline2=store.thermocline2,
        hp_currently_on=store.hp_on,
        hp_max_kw_el=ops.hp_max_kw_el,
        hp_turn_on_minutes=ops.hp_turn_on_minutes,
        cop_intercept=ops.cop_curve.intercept,
        cop_oat_coeff=ops.cop_curve.oat_coeff,
        cop_lwt_coeff=ops.cop_curve.lwt_coeff,
        cop_min=ops.cop_curve.min,
        cop_min_oat_f=ops.cop_curve.min_oat_f,
        price_max_usd_mwh=PRICE_MAX_USD_MWH,
        price_min_usd_mwh=PRICE_MIN_USD_MWH,
        previous_optimal_path=previous.path if previous else None,
        previous_estimate_elec_usd_mwh_now=previous.elec_usd_mwh_now
        if previous
        else None,
        tuning=ops.flo_tuning,
    )
