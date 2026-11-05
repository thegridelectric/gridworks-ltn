"""The LTN's flo.params is built from the house's facts: the ops word, the
layout's water store, the operating status and a live forecast."""

import json
from pathlib import Path
from typing import Any

import pytest

from gwltn.flo_request import (
    MAX_HORIZON_HOURS,
    PRICE_MAX_USD_MWH,
    PRICE_MIN_USD_MWH,
    PreviousRun,
    SeasonalFillForecast,
    StoreState,
    build_flo_params,
    load_forecast_kwh,
    load_previous_run,
    next_slot_start_s,
    previous_run_for,
    save_previous_run,
    seconds_to_slot_start,
)
from gwltn.sema.enums import SeasonalStorageMode, WeatherForecastFidelity
from gwltn.sema.types import (
    FloOptimalPath,
    OperationalParams,
    WaterStore,
    WeatherForecast,
    WeatherForecastBundleGt,
)
from gwltn.weather_source import ForecastPair, HourlyForecast, utc_seconds
from tests.weather_fixtures import bundle_dict, forecast_dict

CONFIG = Path(__file__).resolve().parent / "config"
ALIAS = "d1.isone.ct.newhaven.willow1"
HORIZON = 48
START_S = utc_seconds("2026-10-18T22:00:00Z")
NOW_S = START_S - 1200
STORE = StoreState(
    top_f=160.0,
    middle_f=130.0,
    bottom_f=95.0,
    thermocline1=2,
    thermocline2=5,
    hp_on=False,
)


def ops() -> OperationalParams:
    return OperationalParams.from_bytes(
        (CONFIG / "gw.house0.willow.operational.params.json").read_bytes()
    )


def three_tanks() -> WaterStore:
    return WaterStore(
        total_store_tanks=3, gallons_per_tank=120, temp_sensors_per_tank=3
    )


def live_pair() -> ForecastPair:
    return ForecastPair(
        message=WeatherForecast.from_dict(forecast_dict()),
        bundle=WeatherForecastBundleGt.from_dict(bundle_dict()),
    )


def build(**overrides):
    kwargs: dict[str, Any] = dict(
        ops=ops(),
        water_store=three_tanks(),
        storage_mode=SeasonalStorageMode.AllTanks,
        forecast=live_pair(),
        now_s=NOW_S,
        horizon_hours=HORIZON,
        lmp_usd_mwh=[40.0 + i for i in range(HORIZON)],
        dist_usd_mwh=[60.0] * HORIZON,
        store=STORE,
        buffer_available_kwh=0.0,
        g_node_alias=ALIAS,
        start_unix_s=START_S,
        created_ms=NOW_S * 1000,
    )
    kwargs.update(overrides)
    return build_flo_params(**kwargs)


def test_the_request_is_the_houses_facts() -> None:
    o = ops()
    pair = live_pair()
    params = build()
    assert params.g_node_alias == ALIAS
    assert params.start_unix_s == START_S
    assert params.weather_forecast_id == pair.message.forecast_uid
    assert params.tanks_modeled == 3
    assert params.gallons_per_tank == 120
    assert params.temp_sensors_per_tank == 3
    assert params.hp_max_kw_el == o.hp_max_kw_el
    assert params.hp_turn_on_minutes == o.hp_turn_on_minutes
    assert params.max_tank_temp_f == o.heating_curve.max_ewt_f
    assert (
        params.cop_intercept,
        params.cop_oat_coeff,
        params.cop_lwt_coeff,
        params.cop_min,
        params.cop_min_oat_f,
    ) == (
        o.cop_curve.intercept,
        o.cop_curve.oat_coeff,
        o.cop_curve.lwt_coeff,
        o.cop_curve.min,
        o.cop_curve.min_oat_f,
    )
    assert params.tuning == o.flo_tuning
    assert (params.price_max_usd_mwh, params.price_min_usd_mwh) == (
        PRICE_MAX_USD_MWH,
        PRICE_MIN_USD_MWH,
    )
    assert params.step_duration_minutes == [60] * HORIZON
    assert params.oat_f_list == pair.next_hours(HORIZON, NOW_S).oat_f
    assert params.lmp_usd_mwh_list[0] == 40.0 and params.dist_usd_mwh_list[-1] == 60.0
    assert len(params.load_kwh_list) == len(params.rswt_f_list) == HORIZON
    assert (
        params.initial_top_temp_f,
        params.initial_thermocline2,
        params.hp_currently_on,
    ) == (160.0, 5, False)
    assert (
        params.previous_optimal_path is None
        and params.previous_estimate_elec_usd_mwh_now is None
    )
    assert params.to_dict()["TypeName"] == "flo.params"


def test_buffer_only_models_one_tank() -> None:
    params = build(
        storage_mode=SeasonalStorageMode.BufferOnly,
        store=STORE._replace(thermocline1=1, thermocline2=2),
    )
    assert params.tanks_modeled == 1
    assert params.seasonal_storage_mode is SeasonalStorageMode.BufferOnly


def test_the_horizon_is_the_shortest_of_asked_cap_forecast_and_prices() -> None:
    assert len(build(horizon_hours=96).step_duration_minutes) == MAX_HORIZON_HOURS
    assert len(build(lmp_usd_mwh=[40.0] * 10).step_duration_minutes) == 10
    assert len(build(horizon_hours=6).oat_f_list) == 6


def test_a_seasonal_fill_is_refused() -> None:
    live = live_pair()
    message = live.message.to_dict()
    message["SeriesList"][0]["Fidelity"] = (
        WeatherForecastFidelity.SeasonalTemplate.value
    )
    pair = ForecastPair(message=WeatherForecast.from_dict(message), bundle=live.bundle)
    with pytest.raises(SeasonalFillForecast, match="Temperature"):
        build(forecast=pair)


def test_the_buffers_energy_comes_off_the_first_hours() -> None:
    curve = ops().heating_curve  # 5.5 - 0.10 * oat
    weather = HourlyForecast(
        time=[1, 2, 3], oat_f=[30.0, 30.0, 30.0], wind_speed_mph=[0.0, 0.0, 0.0]
    )
    assert load_forecast_kwh(curve, weather, 0.0) == [2.5, 2.5, 2.5]
    assert load_forecast_kwh(curve, weather, 3.0) == [0.0, 2.0, 2.5]
    assert load_forecast_kwh(curve, weather, -1.0) == [3.5, 2.5, 2.5]


def test_a_previous_run_is_handed_on() -> None:
    path = FloOptimalPath(
        start_unix_s=START_S - 3600,
        step_duration_minutes=[60, 60],
        hp_avg_w_list=[3000, 0],
        store_wh_list=[20000, 18000],
    )
    params = build(previous=PreviousRun(path=path, elec_usd_mwh_now=101.5))
    assert params.previous_optimal_path == path
    assert params.previous_estimate_elec_usd_mwh_now == 101.5


def test_a_previous_run_is_only_for_the_slot_its_first_step_ends_at() -> None:
    path = FloOptimalPath(
        start_unix_s=START_S - 3600,
        step_duration_minutes=[60, 60],
        hp_avg_w_list=[3000, 0],
        store_wh_list=[20000, 18000],
    )
    run = PreviousRun(path=path, elec_usd_mwh_now=101.5)
    assert previous_run_for(run, START_S) == run
    assert previous_run_for(run, START_S + 3600) is None
    assert previous_run_for(None, START_S) is None


def test_a_previous_run_survives_the_disk(tmp_path: Path) -> None:
    path = FloOptimalPath(
        start_unix_s=START_S - 3600,
        step_duration_minutes=[60, 60],
        hp_avg_w_list=[3000, 0],
        store_wh_list=[20000, 18000],
    )
    run = PreviousRun(path=path, elec_usd_mwh_now=101.5)
    file = tmp_path / "previous_run.json"
    assert load_previous_run(file) is None
    save_previous_run(file, run)
    assert json.loads(file.read_text())["Path"]["TypeName"] == "flo.optimal.path"
    assert load_previous_run(file) == run


def test_the_next_slot_is_the_next_whole_hour_of_the_epoch() -> None:
    assert next_slot_start_s(START_S - 180) == START_S
    assert next_slot_start_s(START_S) == START_S + 3600
    assert seconds_to_slot_start(START_S - 180) == 180
    assert seconds_to_slot_start(START_S) == 3600
    assert seconds_to_slot_start(START_S - 0.5) == 1


def test_the_slot_does_not_move_at_a_clock_change() -> None:
    """At 01:57 EDT on 2026-11-01 the next hour on the wall is 01:00 EST,
    one slot later in the epoch; a local-time hour would name 02:00 EST
    (07:00Z) and the bid would miss the price. Spring forward the same."""
    assert next_slot_start_s(utc_seconds("2026-11-01T05:57:00Z")) == utc_seconds(
        "2026-11-01T06:00:00Z"
    )
    assert next_slot_start_s(utc_seconds("2026-03-08T06:57:00Z")) == utc_seconds(
        "2026-03-08T07:00:00Z"
    )
