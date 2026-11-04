"""Where the scada's weather forecast comes from.

The derived generator and the LTN ask the scada's weather source for a
forecast and never reach a network themselves. A box pulls the bundle its
operational params name from the GridWorks weather service and keeps the
last forecast message and the bundle record beside it as sema instances,
read before any network call so a restart never waits on the service; a
test plant is given a simulated source whose forecast and response time
the test sets, so the suite never depends on a live service. Behind the
forecast sits the location's seasonal template, laid on the bundle's grid
when no forecast covers the horizon or the forecast lacks a series the
consumers read. The bundle record and the template are provisioning: the
LTN reads them from its config dir at boot, pulls whichever is missing,
and does not start without both, so a box installed without the service
is found out at its first boot rather than at its first outage. Plant
time comes from the LTN's clock; only the pull timeouts are on the wall.
"""

import asyncio
import json
import logging
import urllib.request
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import auto
from pathlib import Path
from typing import NamedTuple, Optional

import aiohttp

from gwltn.clock import Clock
from gwltn.sema.base import SemaError
from gwltn.sema.enums import Quantity, Unit, WeatherForecastFidelity
from gwltn.sema.enums.gw_str_enum import GwStrEnum
from gwltn.sema.property_format import LeftRightDot, UTCSeconds
from gwltn.sema.types import (
    OperationalParams,
    WeatherChannelGt,
    WeatherForecast,
    WeatherForecastBinding,
    WeatherForecastBundleGt,
    WeatherForecastChannelGt,
    WeatherForecastSeries,
    WeatherSeasonalTemplateGt,
)

# Hours of forecast the consumers read.
FORECAST_HOURS = 48

# The series the consumers read. A forecast without one of them is filled
# from the seasonal template.
TEMPERATURE = Quantity.Temperature
WIND_SPEED = Quantity.WindSpeed
CONSUMED_QUANTITIES = (TEMPERATURE, WIND_SPEED)

# Whole units per scaled integer, by the channel's Unit.
UNIT_DIVISOR: dict[Unit, float] = {
    Unit.FahrenheitX10: 10.0,
    Unit.FahrenheitX100: 100.0,
    Unit.MilesPerHourX10: 10.0,
    Unit.MilesPerHourX1000: 1000.0,
    Unit.WattsPerSquareMeter: 1.0,
}


class WeatherSourceKind(GwStrEnum):
    Gwwf = auto()
    Sim = auto()


class HourlyForecast(NamedTuple):
    """Consecutive forecast slices after a moment, unscaled: the start of each
    slice in UTC seconds, its outdoor air temperature in °F and its wind
    speed in mph. A view derived from a ForecastPair, never stored or sent."""

    time: list[UTCSeconds]
    oat_f: list[float]
    wind_speed_mph: list[float]


def binding_for(
    bundle: WeatherForecastBundleGt, quantity: Quantity
) -> Optional[WeatherForecastBinding]:
    """The bundle's binding whose observed channel is `quantity`, if it binds one."""
    for binding in bundle.binding_list:
        if binding.observation_channel.quantity == quantity:
            return binding
    return None


def slice_grid(bundle: WeatherForecastBundleGt) -> list[int]:
    """The slice grid every forecast channel of the bundle shares."""
    return bundle.binding_list[0].forecast_channel.slice_duration_s_list


class ForecastPair(NamedTuple):
    """A gw.weather.forecast message with the gw.weather.forecast.bundle.gt
    record it was emitted on. Slice times and units live on the record, so
    the message is read through the pair."""

    message: WeatherForecast
    bundle: WeatherForecastBundleGt

    def series_for(self, quantity: Quantity) -> Optional[WeatherForecastSeries]:
        for series in self.message.series_list:
            if series.quantity == quantity:
                return series
        return None

    def missing_quantities(self) -> list[Quantity]:
        """The consumed quantities the message has no series for."""
        return [q for q in CONSUMED_QUANTITIES if self.series_for(q) is None]

    def fidelity_label(self) -> str:
        """One fidelity when every series shares it, else each series' own."""
        fidelities = {s.fidelity for s in self.message.series_list}
        if len(fidelities) == 1:
            return fidelities.pop().value
        return ", ".join(
            f"{s.quantity.value} {s.fidelity.value}" for s in self.message.series_list
        )

    def slice_starts(self) -> list[UTCSeconds]:
        """The start of every slice the message carries, from the message's
        FirstSliceStart along the bundle's slice grid."""
        start_s = utc_seconds(self.message.first_slice_start)
        starts: list[UTCSeconds] = []
        for duration_s in slice_grid(self.bundle)[
            : len(self.message.series_list[0].values)
        ]:
            starts.append(start_s)
            start_s += duration_s
        return starts

    def unscaled(self, quantity: Quantity) -> list[float]:
        """The series of `quantity` in whole units per its observed channel.
        A missing wind series reads as calm; a missing temperature series
        is a defect the source prevents."""
        series = self.series_for(quantity)
        if series is None:
            if quantity == WIND_SPEED:
                return [0.0] * len(self.message.series_list[0].values)
            raise ValueError(
                f"Forecast on {self.bundle.name} has no {quantity.value} series"
            )
        binding = binding_for(self.bundle, quantity)
        if binding is None:
            raise ValueError(
                f"Bundle {self.bundle.name} binds no {quantity.value} channel"
            )
        divisor = unit_divisor(self.bundle, binding.observation_channel)
        return [v / divisor for v in series.values]

    def next_hours(self, hours: int, now_s: float) -> HourlyForecast:
        """Up to `hours` slices starting after plant time `now_s`, unscaled
        per the bundle's observation units."""
        starts = self.slice_starts()
        oat_f = self.unscaled(TEMPERATURE)
        wind_speed_mph = self.unscaled(WIND_SPEED)
        future = [i for i in range(len(starts)) if starts[i] > now_s][:hours]
        return HourlyForecast(
            time=[starts[i] for i in future],
            oat_f=[oat_f[i] for i in future],
            wind_speed_mph=[wind_speed_mph[i] for i in future],
        )

    def covers_next(self, hours: int, now_s: float) -> bool:
        return len(self.next_hours(hours, now_s).time) >= hours


def utc_seconds(iso: str) -> UTCSeconds:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def utc_iso(seconds: float) -> str:
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def unit_divisor(bundle: WeatherForecastBundleGt, channel: WeatherChannelGt) -> float:
    if channel.unit not in UNIT_DIVISOR:
        raise ValueError(
            f"Bundle {bundle.name} channel {channel.name} unit {channel.unit.value} has no unscaling here"
        )
    return UNIT_DIVISOR[channel.unit]


def next_whole_hour_s(now_s: float) -> int:
    return (int(now_s) // 3600 + 1) * 3600


def forecast_on(
    bundle: WeatherForecastBundleGt,
    fidelity: WeatherForecastFidelity,
    first_slice_start_s: int,
    values: dict[Quantity, list[float]],
    now_s: float,
) -> WeatherForecast:
    """A gw.weather.forecast on `bundle` created at plant time `now_s`: one
    series per quantity in `values`, each scaled per the observed channel
    the bundle binds for it, as many slices as the lists carry."""
    series_list: list[WeatherForecastSeries] = []
    for quantity, unscaled in values.items():
        binding = binding_for(bundle, quantity)
        if binding is None:
            raise ValueError(f"Bundle {bundle.name} binds no {quantity.value} channel")
        divisor = unit_divisor(bundle, binding.observation_channel)
        series_list.append(
            WeatherForecastSeries(
                channel_name=binding.forecast_channel.name,
                quantity=quantity,
                values=[round(v * divisor) for v in unscaled],
                fidelity=fidelity,
                source_updated_time=utc_iso(now_s),
            )
        )
    return WeatherForecast(
        bundle_name=bundle.name,
        forecast_uid=str(uuid.uuid4()),
        message_created_ms=int(now_s * 1000),
        first_slice_start=utc_iso(first_slice_start_s),
        series_list=series_list,
    )


def seasonal_fill(
    bundle: WeatherForecastBundleGt, template: WeatherSeasonalTemplateGt, now_s: float
) -> ForecastPair:
    """The bundle's whole slice grid from the next whole hour after plant
    time `now_s`, each slice at its month's template temperature, marked
    SeasonalTemplate. The template knows no wind: the wind series is calm
    when the bundle binds one and absent when it does not."""
    start_s = next_whole_hour_s(now_s)
    oat_f: list[float] = []
    for duration_s in slice_grid(bundle):
        month = datetime.fromtimestamp(start_s, tz=timezone.utc).month
        oat_f.append(template.temp_by_month[month - 1] / 100)
        start_s += duration_s
    values: dict[Quantity, list[float]] = {TEMPERATURE: oat_f}
    if binding_for(bundle, WIND_SPEED) is not None:
        values[WIND_SPEED] = [0.0] * len(oat_f)
    message = forecast_on(
        bundle,
        WeatherForecastFidelity.SeasonalTemplate,
        next_whole_hour_s(now_s),
        values,
        now_s,
    )
    return ForecastPair(message, bundle)


class WeatherSource(ABC):
    # The consumed quantities the forecast answered last was filled for
    # want of; empty when it was served as pulled.
    missing_quantities: list[Quantity] = []

    @abstractmethod
    async def forecast(self, session: aiohttp.ClientSession) -> Optional[ForecastPair]:
        """The latest forecast covering the next FORECAST_HOURS, or None when
        the source holds none."""
        raise NotImplementedError


class WeatherProvisioningError(RuntimeError):
    """The scada cannot start: a provisioning record (the bundle record or
    the seasonal template) is neither in the config dir nor obtainable
    from the weather service, or the bundle binds no temperature channel."""


def record_file(config_dir: Path, subject: str, word: type) -> Path:
    """The config-dir file for a sema record: `<subject>-<type.name>-<version>.json`."""
    return (
        config_dir / f"{subject}-{word.type_name_value()}-{word.version_value()}.json"
    )


class GwwfWeatherSource(WeatherSource):
    """The latest forecast for one bundle from the GridWorks weather
    service's read facade, with the last message, the bundle record and
    the location's seasonal template kept in the config dir and read
    first. The bundle record and the template are provisioning: boot
    pulls whichever is missing and raises WeatherProvisioningError when
    it cannot; the template fills behind the forecast."""

    def __init__(
        self,
        bundle_name: LeftRightDot,
        api_url: str,
        config_dir: Path,
        timeout_s: float,
        clock: Clock,
        logger: logging.Logger,
    ) -> None:
        self.bundle_name = bundle_name
        self.api_url = api_url.rstrip("/")
        self.timeout_s = timeout_s
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)
        self.clock = clock
        self.logger = logger
        self.config_dir = config_dir
        self.missing_quantities = []
        self.forecast_file = record_file(config_dir, bundle_name, WeatherForecast)
        self.bundle_file = record_file(config_dir, bundle_name, WeatherForecastBundleGt)
        self.bundle: Optional[WeatherForecastBundleGt] = self.stored_bundle()
        if self.bundle is None:
            self.bundle = self.provision_bundle()
        if binding_for(self.bundle, TEMPERATURE) is None:
            raise WeatherProvisioningError(
                f"Bundle {self.bundle_name} binds no {TEMPERATURE.value} channel; the seasonal fill needs one"
            )
        self.template: Optional[WeatherSeasonalTemplateGt] = self.stored_template(
            self.bundle.location_alias
        )
        if self.template is None:
            self.template = self.provision_template(self.bundle.location_alias)
        self.message: Optional[WeatherForecast] = self.stored_forecast()

    def template_file(self, location_alias: LeftRightDot) -> Path:
        return record_file(self.config_dir, location_alias, WeatherSeasonalTemplateGt)

    def provision_records(self, path: str, what: str) -> list[dict]:
        """A read-facade listing pulled at boot, before the scada's loop
        runs; failure to reach it is failure to start."""
        url = f"{self.api_url}/{path}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_s) as response:
                return json.load(response)
        except (OSError, ValueError) as e:
            raise WeatherProvisioningError(
                f"No {what} in {self.config_dir} and the weather service pull {url} failed: {e!r}"
            ) from e

    def provision_bundle(self) -> WeatherForecastBundleGt:
        what = f"bundle record {self.bundle_name}"
        records = [
            WeatherForecastBundleGt.from_dict(d)
            for d in self.provision_records("bundles", what)
        ]
        mine = [r for r in records if r.name == self.bundle_name]
        if not mine:
            raise WeatherProvisioningError(
                f"No {what} in {self.config_dir} and the weather service has none"
            )
        self.bundle_file.write_text(json.dumps(mine[0].to_dict(), indent=2))
        self.logger.info(f"Provisioned the {what} from the weather service")
        return mine[0]

    def provision_template(
        self, location_alias: LeftRightDot
    ) -> WeatherSeasonalTemplateGt:
        what = f"seasonal template for {location_alias}"
        records = [
            WeatherSeasonalTemplateGt.from_dict(d)
            for d in self.provision_records("seasonal-templates", what)
        ]
        now_s = self.clock.now()
        mine = [
            r
            for r in records
            if r.location_alias == location_alias and utc_seconds(r.start) <= now_s
        ]
        if not mine:
            raise WeatherProvisioningError(
                f"No {what} in {self.config_dir} and the weather service has none"
            )
        template = max(mine, key=lambda r: r.start)
        self.template_file(location_alias).write_text(
            json.dumps(template.to_dict(), indent=2)
        )
        self.logger.info(f"Provisioned the {what} from the weather service")
        return template

    async def forecast(self, session: aiohttp.ClientSession) -> Optional[ForecastPair]:
        self.missing_quantities = []
        pulled = await self.pull_forecast(session)
        if pulled is not None:
            self.message = pulled
        if self.bundle is None or (
            self.message is not None and not self.bundle_matches(self.message)
        ):
            self.bundle = await self.pull_bundle(session)
        if self.bundle is None:
            self.logger.warning(
                f"No bundle record for {self.bundle_name} stored or reachable; no forecast"
            )
            return None
        if binding_for(self.bundle, TEMPERATURE) is None:
            self.logger.warning(
                f"Bundle {self.bundle_name} binds no {TEMPERATURE.value} channel; no forecast"
            )
            return None
        now_s = self.clock.now()
        if self.message is None:
            why = "No forecast stored or reachable"
        else:
            pair = ForecastPair(self.message, self.bundle)
            missing = pair.missing_quantities()
            if missing:
                self.missing_quantities = missing
                why = f"Forecast {self.message.first_slice_start} has no {', '.join(q.value for q in missing)} series"
            elif pair.covers_next(FORECAST_HOURS, now_s):
                return pair
            else:
                why = f"Forecast {self.message.first_slice_start} does not cover the next {FORECAST_HOURS} hours"
        if (
            self.template is None
            or self.template.location_alias != self.bundle.location_alias
        ):
            self.template = await self.pull_template(
                session, self.bundle.location_alias, now_s
            )
        if self.template is None:
            self.logger.warning(
                f"{why}, and no seasonal template for {self.bundle.location_alias} stored or reachable; no forecast"
            )
            return None
        self.logger.warning(f"{why}; filling from the seasonal template")
        return seasonal_fill(self.bundle, self.template, now_s)

    def bundle_matches(self, message: WeatherForecast) -> bool:
        """The stored bundle is the one the message was emitted on: same
        name, and every series on a channel the bundle binds."""
        if self.bundle is None or self.bundle.name != message.bundle_name:
            return False
        bound = {b.forecast_channel.name for b in self.bundle.binding_list}
        return all(s.channel_name in bound for s in message.series_list)

    async def pull_forecast(
        self, session: aiohttp.ClientSession
    ) -> Optional[WeatherForecast]:
        url = f"{self.api_url}/latest-forecast/{self.bundle_name}"
        try:
            async with session.get(url, timeout=self.timeout) as response:
                if response.status != 200:
                    self.logger.warning(
                        f"Forecast pull {url} returned {response.status}"
                    )
                    return None
                message = WeatherForecast.from_dict(await response.json())
        except (aiohttp.ClientError, asyncio.TimeoutError, SemaError, ValueError) as e:
            self.logger.warning(f"Forecast pull {url} failed: {e!r}")
            return None
        self.forecast_file.write_text(json.dumps(message.to_dict(), indent=2))
        slices = len(message.series_list[0].values)
        fidelities = ", ".join(
            f"{s.quantity.value} {s.fidelity.value}" for s in message.series_list
        )
        self.logger.info(
            f"Pulled a {slices}-slice forecast starting {message.first_slice_start} ({fidelities})"
        )
        return message

    async def pull_bundle(
        self, session: aiohttp.ClientSession
    ) -> Optional[WeatherForecastBundleGt]:
        url = f"{self.api_url}/bundles"
        try:
            async with session.get(url, timeout=self.timeout) as response:
                if response.status != 200:
                    self.logger.warning(f"Bundle pull {url} returned {response.status}")
                    return None
                records = [
                    WeatherForecastBundleGt.from_dict(d) for d in await response.json()
                ]
        except (aiohttp.ClientError, asyncio.TimeoutError, SemaError, ValueError) as e:
            self.logger.warning(f"Bundle pull {url} failed: {e!r}")
            return None
        mine = [r for r in records if r.name == self.bundle_name]
        if not mine:
            self.logger.warning(f"The weather service has no bundle {self.bundle_name}")
            return None
        self.bundle_file.write_text(json.dumps(mine[0].to_dict(), indent=2))
        return mine[0]

    async def pull_template(
        self,
        session: aiohttp.ClientSession,
        location_alias: LeftRightDot,
        now_s: float,
    ) -> Optional[WeatherSeasonalTemplateGt]:
        """The location's template with the latest Start at or before plant
        time `now_s`, kept in the config dir."""
        url = f"{self.api_url}/seasonal-templates"
        try:
            async with session.get(url, timeout=self.timeout) as response:
                if response.status != 200:
                    self.logger.warning(
                        f"Seasonal template pull {url} returned {response.status}"
                    )
                    return None
                records = [
                    WeatherSeasonalTemplateGt.from_dict(d)
                    for d in await response.json()
                ]
        except (aiohttp.ClientError, asyncio.TimeoutError, SemaError, ValueError) as e:
            self.logger.warning(f"Seasonal template pull {url} failed: {e!r}")
            return None
        mine = [
            r
            for r in records
            if r.location_alias == location_alias and utc_seconds(r.start) <= now_s
        ]
        if not mine:
            self.logger.warning(
                f"The weather service has no seasonal template for {location_alias}"
            )
            return None
        template = max(mine, key=lambda r: r.start)
        self.template_file(location_alias).write_text(
            json.dumps(template.to_dict(), indent=2)
        )
        return template

    def stored_template(
        self, location_alias: LeftRightDot
    ) -> Optional[WeatherSeasonalTemplateGt]:
        path = self.template_file(location_alias)
        if not path.exists():
            return None
        try:
            return WeatherSeasonalTemplateGt.from_bytes(path.read_bytes())
        except (SemaError, ValueError) as e:
            self.logger.warning(
                f"Stored seasonal template {path} does not decode: {e!r}"
            )
            return None

    def stored_forecast(self) -> Optional[WeatherForecast]:
        if not self.forecast_file.exists():
            return None
        try:
            return WeatherForecast.from_bytes(self.forecast_file.read_bytes())
        except (SemaError, ValueError) as e:
            self.logger.warning(
                f"Stored forecast {self.forecast_file} does not decode: {e!r}"
            )
            return None

    def stored_bundle(self) -> Optional[WeatherForecastBundleGt]:
        if not self.bundle_file.exists():
            return None
        try:
            return WeatherForecastBundleGt.from_bytes(self.bundle_file.read_bytes())
        except (SemaError, ValueError) as e:
            self.logger.warning(
                f"Stored bundle record {self.bundle_file} does not decode: {e!r}"
            )
            return None


SIM_LOCATION = "sim.millinocket"
SIM_STATION = "sim"
SIM_BUNDLE_NAME = f"{SIM_LOCATION}.steady"
SIM_SLICES = 96
SIM_FORECASTER = "sim.forecaster"
SIM_METHOD = "sim.steady"


def sim_bundle(start_s: float) -> WeatherForecastBundleGt:
    """A 96-hour hourly bundle record for the simulated weather source,
    starting at the whole hour after `start_s`, °F x10 and mph x10 like
    the Millinocket one, observed at a simulated station."""
    start = utc_iso(next_whole_hour_s(start_s))

    def binding(quantity: Quantity, display: str, unit: Unit) -> WeatherForecastBinding:
        observed = f"{SIM_STATION}.{quantity.value.lower()}"
        return WeatherForecastBinding(
            forecast_channel=WeatherForecastChannelGt(
                name=f"{observed}.forecast.{SIM_FORECASTER}",
                target_channel_name=observed,
                forecaster=SIM_FORECASTER,
                method=SIM_METHOD,
                total_slices=SIM_SLICES,
                slice_duration_s_list=[3600] * SIM_SLICES,
                forecast_duration_minutes=SIM_SLICES * 60,
                start=start,
                id=str(uuid.uuid4()),
            ),
            observation_channel=WeatherChannelGt(
                name=observed,
                display_name=display,
                quantity=quantity,
                unit=unit,
                station_alias=SIM_STATION,
                emit_period_s=3600,
                emit_offset_s=0,
                start=start,
                id=str(uuid.uuid4()),
            ),
        )

    return WeatherForecastBundleGt(
        name=SIM_BUNDLE_NAME,
        display_name="Simulated hourly 96-hour forecast bundle",
        location_alias=SIM_LOCATION,
        binding_list=[
            binding(
                TEMPERATURE, "Simulated outdoor air temperature", Unit.FahrenheitX10
            ),
            binding(WIND_SPEED, "Simulated wind speed", Unit.MilesPerHourX10),
        ],
        emit_period_s=3600,
        emit_offset_s=60,
        start=start,
        id=str(uuid.uuid4()),
    )


def sim_forecast(
    now_s: float,
    oat_f: float = 30.0,
    wind_speed_mph: float = 5.0,
    bundle: Optional[WeatherForecastBundleGt] = None,
) -> ForecastPair:
    """A steady Live forecast on the simulated bundle: every slice from the
    whole hour after plant time `now_s` at `oat_f` and `wind_speed_mph`."""
    bundle = sim_bundle(now_s) if bundle is None else bundle
    slices = len(slice_grid(bundle))
    message = forecast_on(
        bundle,
        WeatherForecastFidelity.Live,
        next_whole_hour_s(now_s),
        {TEMPERATURE: [oat_f] * slices, WIND_SPEED: [wind_speed_mph] * slices},
        now_s,
    )
    return ForecastPair(message, bundle)


class SimWeatherSource(WeatherSource):
    """A steady 30 F and 5 mph on the simulated bundle from the plant's
    next whole hour. Three pins stand in for the service's ways of being
    slow or absent, all IO time: `release` holds every fetch open while
    clear (a test clears it, then sets it to let the fetch through);
    `delay_s` is a wall-clock delay per fetch; `misses_left` is how many
    fetches answer None before the pair is served. `fetches` counts
    calls."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.delay_s: float = 0
        self.release = asyncio.Event()
        self.release.set()
        self.misses_left: int = 0
        self.fetches: int = 0
        self.missing_quantities = []
        self.bundle = sim_bundle(clock.now())

    async def forecast(self, session: aiohttp.ClientSession) -> Optional[ForecastPair]:
        await self.release.wait()
        await asyncio.sleep(self.delay_s)
        self.fetches += 1
        if self.misses_left > 0:
            self.misses_left -= 1
            return None
        return sim_forecast(self.clock.now(), bundle=self.bundle)


def build_weather_source(
    kind: WeatherSourceKind,
    ops: OperationalParams,
    config_dir: Path,
    api_url: str,
    timeout_s: float,
    clock: Clock,
    base_log_name: str,
) -> WeatherSource:
    """The weather source the settings ask for, on the bundle the ops word
    names, reading plant time from the LTN's clock and logging under its
    base logger."""
    if kind == WeatherSourceKind.Gwwf:
        return GwwfWeatherSource(
            ops.weather_bundle_name,
            api_url,
            config_dir,
            timeout_s,
            clock,
            logging.getLogger(f"{base_log_name}.weather_source"),
        )
    if kind == WeatherSourceKind.Sim:
        return SimWeatherSource(clock)
    raise ValueError(f"Unknown weather source {kind}")
