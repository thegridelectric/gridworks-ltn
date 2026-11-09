"""LtnActor: the LTN as a gwbase actor.

The scada is its one MQTT-native peer. The LTN hears everything the
scada sends upstream (`amq.topic`, routing key `gw.<scada>.to.ltn.#`,
bridged from the scada's MQTT publish), acks each message that asks for
one, and pings the scada every PING_S so the scada's link stays Active.
Everything the LTN sends the scada goes out wrapped on
`gw.<ltn>.to.s.<type>`.

The FLO and the market maker are GNodes on the class fabric. The
`flo.params` goes to the FLO GNode as a direct message over the
LeafTransactiveNode-to-Flo edge and its `flo.recommendation` comes back
direct over the edge back; the bid goes to the maker as a direct
message; the maker's `latest.price` broadcast arrives on the maker's own
mic exchange.

Two threads touch the LTN's state: pika consumes on one, and the LTN's
own asyncio loop (the main loop, the ping and the contract heartbeat)
runs on another. Every inbound message is marshalled onto the asyncio
loop, so the LTN's state is read and written on that one thread. Sends
are safe from any thread.
"""

from __future__ import annotations

import asyncio
import csv
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

import aiohttp
import httpx
import numpy as np
import pytz
from gwbase.gridworks_actor import GridworksActor
from gwbase.sema.enums import GwGNodeClass
from gwbase.sema.types.gridworks_header import GridworksHeader
from gwbase.transport_encoding import RoutingEnvelope, TransportClass

from gwltn.clock import Clock, WallClock
from gwltn.config import GwltnSettings
from gwltn.contract_handler import LtnContractHandler
from gwltn.data import LtnData
from gwltn.flo_request import (
    PreviousRun,
    PriceForecast,
    SeasonalFillForecast,
    StoreState,
    build_flo_params,
    load_previous_run,
    next_slot_start_s,
    previous_run_for,
    price_forecast_from,
    save_previous_run,
    seconds_to_slot_start,
)
from gwltn.layout import LtnLayout, load_layout, load_operational_params
from gwltn.market import (
    answers_request,
    bid_from_recommendation,
    cleared_pair_index,
    slot_is_current,
)
from gwltn.names import (
    BUFFER_DEPTH_CHANNELS,
    HP_SCADA_OPS_RELAY,
    LTN_NODE_NAME,
    STORE_COLD_PIPE,
)
from gwltn.scada_wire import (
    ACK_TYPE,
    PING_TYPE,
    ack_payload,
    ping_payload,
    scada_body,
    scada_envelope,
)
from gwltn.sema.base import SemaType
from gwltn.sema.codec import SemaCodec
from gwltn.sema.enums import (
    LogLevel,
    MarketQuantityUnit,
    RelayClosedOrOpen,
    SeasonalStorageMode,
    ServiceMode,
    SlowDispatchContractStatus,
)
from gwltn.sema.types import (
    Bid,
    FloOptimalPath,
    FloParams,
    FloRecommendation,
    FloRefusal,
    Glitch,
    GridworksEventCommMqttConnect,
    GridworksEventCommMqttDisconnect,
    GridworksEventCommMqttFullySubscribed,
    GridworksEventCommPeerActive,
    GridworksEventCommResponseTimeout,
    GridworksEventProblem,
    GridworksEventShutdown,
    GridworksEventStartup,
    HouseOperatingStatus,
    LatestPrice,
    LayoutLite,
    MachineStates,
    OperationalParams,
    PowerWatts,
    Report,
    ReportEvent,
    SendLayout,
    SlowContractHeartbeat,
    SlowContractRejection,
    SnapshotSpaceheat,
    WaterStore,
)
from gwltn.store_temps import scrub_and_fill_store_temps
from gwltn.temperature import convert_temp_to_f
from gwltn.weather_source import ForecastPair, WeatherSource, build_weather_source
from gwltn.words import NoNewContractWarning, SendSnap

# The scada's link layer pings its peer every 60 s and expects the same.
PING_S = 60.0
LATEST_PRICE_TYPE = LatestPrice.type_name_value()
BID_TYPE = Bid.type_name_value()
FLO_REQUEST_TYPE = FloParams.type_name_value()
FLO_REPLY_TYPE = FloRecommendation.type_name_value()
FLO_REFUSAL_TYPE = FloRefusal.type_name_value()

# Every word the LTN's snapshot holds decodes through its codec; the one
# the scada sends with no sema word yet decodes through its local word.
CODEC = SemaCodec()
LOCAL_WORD_BY_TYPE_NAME: dict[str, type[SemaType]] = {
    NoNewContractWarning.type_name_value(): NoNewContractWarning
}

# The proactor's lifecycle and comm events, as the scada's upstream link
# emits them; the LTN keeps them for the record.
EVENT_WORDS = (
    GridworksEventStartup,
    GridworksEventShutdown,
    GridworksEventProblem,
    GridworksEventCommMqttConnect,
    GridworksEventCommMqttDisconnect,
    GridworksEventCommMqttFullySubscribed,
    GridworksEventCommPeerActive,
    GridworksEventCommResponseTimeout,
)
EventWord = (
    GridworksEventStartup
    | GridworksEventShutdown
    | GridworksEventProblem
    | GridworksEventCommMqttConnect
    | GridworksEventCommMqttDisconnect
    | GridworksEventCommMqttFullySubscribed
    | GridworksEventCommPeerActive
    | GridworksEventCommResponseTimeout
)


def scada_binding_key(scada_alias: str) -> str:
    """Every wrapped message this LTN's scada addresses to the ltn class.
    Bound by the scada's alias, so another house's scada is never heard."""
    return f"gw.{scada_alias.replace('.', '-')}.to.ltn.#"


class LtnActor(GridworksActor):
    MAIN_LOOP_SLEEP_SECONDS = 61

    def __init__(self, *, settings: GwltnSettings, clock: Clock | None = None) -> None:
        super().__init__(
            settings=settings,
            g_node_class=GwGNodeClass.LeafTransactiveNode,
            my_super_alias=settings.super_alias,
            my_time_coordinator_alias=settings.time_coordinator_alias,
        )
        self.ltn_settings = settings
        self.layout: LtnLayout = load_layout(
            settings.hardware_layout, settings.operational_params
        )
        self.ops: OperationalParams = load_operational_params(
            settings.operational_params
        )
        if self.layout.ltn_alias != self.alias:
            raise ValueError(
                f"Layout {settings.hardware_layout} names LTN {self.layout.ltn_alias!r}; "
                f"this actor is {self.alias!r}"
            )
        self.scada_alias: str = self.layout.scada_alias
        self.flo_alias: str = settings.flo_alias
        self.plant_clock: Clock = clock or WallClock()
        self.weather_source: WeatherSource = build_weather_source(
            settings.weather_source,
            self.ops,
            settings.hardware_layout.parent,
            settings.weather_api_url,
            settings.weather_pull_timeout_s,
            self.plant_clock,
            self.logger.name,
        )
        self.data_dir: Path = settings.data_path
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.event_dir: Path = self.data_dir / "events"

        self.data = LtnData()
        self.latest_channel_values: Dict[str, int] = {}
        self.latest_temps_f: Dict[str, float] = {}
        self.temperatures_available: bool = False
        self.timezone = pytz.timezone(settings.timezone_str)
        self.flo_horizon_hours = settings.flo_horizon_hours
        self.flo_params: FloParams | None = None
        self.hp_is_off = False
        self.weather_forecast: Optional[ForecastPair] = None
        self.price_forecast: Optional[PriceForecast] = None
        self.tank_temp_channel_names: list[str] | None = None
        self.report_output_dir = self.data_dir / "report"
        self.report_output_dir.mkdir(parents=True, exist_ok=True)
        self.recommendation: FloRecommendation | None = None
        self.previous_run_file = self.data_dir / "previous_run.json"
        self.previous_run: PreviousRun | None = load_previous_run(
            self.previous_run_file
        )
        contract_logger = self.logger.getChild(LtnContractHandler.LOGGER_NAME)
        contract_logger.setLevel(settings.contract_rep_logging_level)
        self.contract_handler: LtnContractHandler = LtnContractHandler(
            node_name=LTN_NODE_NAME,
            scada_alias=self.scada_alias,
            settings=settings,
            logger=contract_logger,
            send_to_scada=self.send_to_scada,
        )
        # The loop wakes every MAIN_LOOP_SLEEP_SECONDS; a lead shorter than
        # that can pass between two wakes and the slot gets no bid.
        if settings.bid_lead_s <= self.MAIN_LOOP_SLEEP_SECONDS:
            raise ValueError(
                f"bid_lead_s ({settings.bid_lead_s}) must exceed the main loop "
                f"sleep ({self.MAIN_LOOP_SLEEP_SECONDS} s)"
            )
        self.bid_lead_s: int = settings.bid_lead_s
        # Gets strategy from scada sending LayoutLite
        self.layout_lite: Optional[LayoutLite] = None
        # The posture facts ride gw.house.operating.status, the LTN's one source;
        # until the first one arrives the LTN refuses to dispatch.
        self.operating_status: Optional[HouseOperatingStatus] = None
        self.accepts_dispatch: bool = False
        self.service_mode: ServiceMode = ServiceMode.Heating
        self.seasonal_storage_mode: SeasonalStorageMode = SeasonalStorageMode.AllTanks
        self.keep_buffer_full: bool = settings.keep_buffer_full

        self.heard: list[str] = []
        self.acked = 0
        self._stop_requested = False
        self.loop = asyncio.new_event_loop()
        self.stop_event: asyncio.Event | None = None
        self.loop_thread = threading.Thread(
            target=self.run_loop, daemon=True, name="ltn-loop"
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def local_start(self) -> None:
        super().local_start()
        self.loop_thread.start()

    def local_stop(self) -> None:
        super().local_stop()
        self._stop_requested = True
        self.loop.call_soon_threadsafe(self.request_loop_stop)
        self.loop_thread.join()

    def request_loop_stop(self) -> None:
        if self.stop_event is not None:
            self.stop_event.set()

    def run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.main())
        finally:
            self.loop.close()

    async def main(self) -> None:
        self.stop_event = asyncio.Event()
        if self._stop_requested:
            return
        tasks = [
            asyncio.create_task(self.main_loop(), name="ltn-main"),
            asyncio.create_task(self.ping_loop(), name="ltn-ping"),
            asyncio.create_task(
                self.contract_handler.contract_heartbeat_task(),
                name="contract_heartbeat",
            ),
        ]
        await self.stop_event.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def on_loop(self, fn, *args) -> None:
        """Run `fn(*args)` on the LTN's asyncio loop thread."""
        self.loop.call_soon_threadsafe(fn, *args)

    # ------------------------------------------------------------------
    # Transport: bindings, inbound, sends
    # ------------------------------------------------------------------

    def local_rabbit_startup(self) -> None:
        self.subscribe_amq_topic(binding_key=scada_binding_key(self.scada_alias))
        self.subscribe_broadcast(
            from_alias=self.ops.market_maker_alias,
            from_class=TransportClass.MarketMaker,
            type_name=LATEST_PRICE_TYPE,
        )

    def process_message(
        self,
        *,
        envelope: RoutingEnvelope,
        body: bytes,
        header: GridworksHeader | None,
    ) -> None:
        if header is None:
            if envelope.type_name == LATEST_PRICE_TYPE:
                self.process_latest_price_bytes(envelope.from_alias, body)
            elif envelope.type_name == FLO_REPLY_TYPE:
                self.process_flo_recommendation_bytes(envelope.from_alias, body)
            elif envelope.type_name == FLO_REFUSAL_TYPE:
                self.process_flo_refusal_bytes(envelope.from_alias, body)
            return
        if header.src != self.scada_alias:
            self.logger.warning(f"Dropped {envelope.type_name} from {header.src}")
            return
        # The ack first, on this thread, before any work: the scada's timer is 5 s.
        if header.ack_required and header.message_id:
            self.send_ack(header.message_id)
        try:
            decoded = self.decode_scada_word(json.loads(body))
        except Exception as e:  # noqa: BLE001 -- the live path keeps running
            self.logger.warning(
                f"Could not decode {envelope.type_name} from the scada: {e}"
            )
            return
        self.on_loop(self.process_scada_message, decoded)

    def decode_scada_word(self, payload: dict) -> SemaType:
        """The scada's word, through the snapshot codec; a word the snapshot
        lacks a sema word for decodes through its local word."""
        local_word = LOCAL_WORD_BY_TYPE_NAME.get(str(payload.get("TypeName")))
        if local_word is not None:
            return local_word.from_dict(payload)
        word = CODEC.from_dict(payload)
        assert isinstance(word, SemaType)
        return word

    def process_latest_price_bytes(self, from_alias: str, body: bytes) -> None:
        if from_alias != self.ops.market_maker_alias:
            self.log(
                f"Ignoring latest.price from {from_alias}: the market maker is {self.ops.market_maker_alias}"
            )
            return
        try:
            price = LatestPrice.from_bytes(body)
        except Exception as e:  # noqa: BLE001 -- the live path keeps running
            self.logger.warning(f"Could not decode latest.price from {from_alias}: {e}")
            return
        self.on_loop(self.process_latest_price, price)

    def process_flo_recommendation_bytes(self, from_alias: str, body: bytes) -> None:
        if from_alias != self.flo_alias:
            self.log(
                f"Ignoring {FLO_REPLY_TYPE} from {from_alias}: the FLO is {self.flo_alias}"
            )
            return
        try:
            recommendation = FloRecommendation.from_bytes(body)
        except Exception as e:  # noqa: BLE001 -- the live path keeps running
            self.logger.warning(
                f"Could not decode {FLO_REPLY_TYPE} from {from_alias}: {e}"
            )
            return
        self.on_loop(self.process_flo_recommendation, recommendation)

    def process_flo_refusal_bytes(self, from_alias: str, body: bytes) -> None:
        if from_alias != self.flo_alias:
            self.log(
                f"Ignoring {FLO_REFUSAL_TYPE} from {from_alias}: the FLO is {self.flo_alias}"
            )
            return
        try:
            refusal = FloRefusal.from_bytes(body)
        except Exception as e:  # noqa: BLE001 -- the live path keeps running
            self.logger.warning(
                f"Could not decode {FLO_REFUSAL_TYPE} from {from_alias}: {e}"
            )
            return
        self.on_loop(self.process_flo_refusal, refusal)

    def send_to_scada(self, payload: SemaType) -> None:
        """A word to the scada, wrapped as its codec expects. Safe from any thread."""
        d = payload.to_dict()
        type_name = d["TypeName"]
        self.send(
            envelope=scada_envelope(type_name=type_name, from_alias=self.alias),
            body=scada_body(from_alias=self.alias, type_name=type_name, payload=d),
        )

    def send_ack(self, message_id: str) -> None:
        payload = ack_payload(message_id)
        self.send(
            envelope=scada_envelope(type_name=ACK_TYPE, from_alias=self.alias),
            body=scada_body(from_alias=self.alias, type_name=ACK_TYPE, payload=payload),
        )
        self.acked += 1

    async def ping_loop(self) -> None:
        while True:
            await asyncio.sleep(PING_S)
            if self.consuming:
                payload = ping_payload()
                self.send(
                    envelope=scada_envelope(type_name=PING_TYPE, from_alias=self.alias),
                    body=scada_body(
                        from_alias=self.alias, type_name=PING_TYPE, payload=payload
                    ),
                )

    def send_to_mm(self, bid: Bid) -> None:
        """The bid, direct to the maker over the LTN-to-MarketMaker edge."""
        self.send(
            envelope=self.direct_envelope(
                type_name=BID_TYPE,
                to_class=TransportClass.MarketMaker,
                to_alias=self.ops.market_maker_alias,
            ),
            body=bid.to_bytes(),
        )

    def send_to_flo(self, params: FloParams) -> None:
        """The request, direct to the FLO GNode over the LTN-to-Flo edge.
        Safe from any thread."""
        self.send(
            envelope=self.direct_envelope(
                type_name=FLO_REQUEST_TYPE,
                to_class=TransportClass.Flo,
                to_alias=self.flo_alias,
            ),
            body=params.to_bytes(),
        )

    # ------------------------------------------------------------------
    # The LTN (on the asyncio loop thread from here down)
    # ------------------------------------------------------------------

    @property
    def water_store(self) -> WaterStore:
        store = self.layout.water_store
        if store is None:
            raise RuntimeError(
                f"Layout {self.layout.ltn_alias} has no water store to run a FLO on"
            )
        return store

    def process_flo_recommendation(self, recommendation: FloRecommendation) -> None:
        """The FLO's answer: when it answers the open request, the
        recommendation is kept for the clearing, copied to the scada for
        the record, and the bid goes off it to the maker. An answer to any
        other request is logged and dropped; the LTN has bid without it or
        will request again."""
        self.heard.append(FLO_REPLY_TYPE)
        if self.flo_params is None or not answers_request(
            self.flo_params, recommendation
        ):
            open_slot = self.flo_params.start_unix_s if self.flo_params else None
            self.log(
                f"Dropped a recommendation for {recommendation.bidder_alias} "
                f"slot {[p.start_unix_s for p in recommendation.optimal_path_list[:1]]}; "
                f"the open request is for slot {open_slot}"
            )
            return
        self.recommendation = recommendation
        self.send_to_scada(recommendation)
        bid = bid_from_recommendation(self.ops, self.flo_params, recommendation)
        self.contract_handler.latest_bid = bid
        self.send_to_mm(bid)
        self.log(f"Bid: {bid}")

    def process_flo_refusal(self, refusal: FloRefusal) -> None:
        """The FLO's answer that there is no recommendation. One for the
        open request is copied to the scada for the record and raised as a
        Warning glitch, so the fleet sees why the slot has no FLO bid; the
        LTN bids as it does when no answer comes. One for another bidder
        or slot is dropped."""
        self.heard.append(FLO_REFUSAL_TYPE)
        if (
            self.flo_params is None
            or refusal.bidder_alias != self.flo_params.g_node_alias
            or refusal.start_unix_s != self.flo_params.start_unix_s
        ):
            open_slot = self.flo_params.start_unix_s if self.flo_params else None
            self.log(
                f"Dropped a refusal for {refusal.bidder_alias} slot "
                f"{refusal.start_unix_s}; the open request is for slot {open_slot}"
            )
            return
        self.send_to_scada(refusal)
        summary = f"FLO refused slot {refusal.start_unix_s}: {refusal.reason.value}"
        self.log(f"{summary}, {refusal.detail}")
        self.send_to_scada(
            Glitch(
                from_g_node_alias=self.alias,
                node=LTN_NODE_NAME,
                type=LogLevel.Warning,
                summary=summary,
                details=refusal.detail or "",
                created_ms=self.plant_clock.now_ms(),
            )
        )

    def process_scada_message(self, word: SemaType) -> None:
        self.heard.append(word.type_name)
        match word:
            case LayoutLite():
                self.process_layout_lite(word)
            case HouseOperatingStatus():
                self.process_house_operating_status(word)
            case NoNewContractWarning():
                self.process_no_new_contract_warning(word)
            case PowerWatts():
                self.process_power_watts(word)
            case Report():
                self.process_report(word)
            case SnapshotSpaceheat():
                self.process_snapshot(word)
            case SlowContractHeartbeat():
                self.contract_handler.process_slow_contract_heartbeat(word)
            case SlowContractRejection():
                self.contract_handler.process_slow_contract_rejection(word)
            case ReportEvent():
                self.process_report(word.report)
            case _ if isinstance(word, EVENT_WORDS):
                self.process_event(word)
            case _:
                pass

    def process_no_new_contract_warning(self, payload: NoNewContractWarning) -> None:
        """
        Resending a "Created" hb if it exists
        """
        hb = self.contract_handler.latest_hb
        if hb:
            if hb.status == SlowDispatchContractStatus.Created:
                self.send_to_scada(hb)

    def process_power_watts(self, pwr: PowerWatts) -> None:
        self.data.latest_power_w = pwr.watts

    def process_snapshot(self, snapshot: SnapshotSpaceheat) -> None:
        self.data.latest_snapshot = snapshot
        for reading in snapshot.latest_reading_list:
            self.latest_channel_values[reading.channel_name] = reading.value

    def process_house_operating_status(self, status: HouseOperatingStatus) -> None:
        """The scada's slow operating facts: the LTN bids and runs FLOs only
        while the house accepts dispatch and serves heat, and sizes the FLO
        by the storage mode."""
        self.log(f"Operating status: {status}")
        self.operating_status = status
        self.accepts_dispatch = status.accepts_dispatch
        self.service_mode = status.service_mode
        self.seasonal_storage_mode = status.seasonal_storage_mode
        self.log(f"FLO seasonal storage mode: {self.seasonal_storage_mode}")

    def process_layout_lite(self, layout: LayoutLite) -> None:
        """ContractState: Initializing -> Ready if needed"""
        self.log("Processing layout lite")
        self.logger.info(f"Processing layout lite: {layout}")
        self.layout_lite = layout

        self.tank_temp_channel_names = self.layout.tank_temp_channel_names

        if self.contract_handler.layout_received is False:
            self.contract_handler.layout_received = (
                True  # Necessary for bids & contracts
            )
            self.log("Received layout data - LTN now ready for contract operations")

    def process_report(self, report: Report) -> None:
        self.data.latest_report = report
        # Check if HP is on or off by looking at relay 6
        for machine_state in report.state_list:
            ms: MachineStates = machine_state
            if HP_SCADA_OPS_RELAY in ms.machine_handle:
                if ms.state_list[-1] == RelayClosedOrOpen.RelayOpen:
                    self.hp_is_off = True
                else:
                    self.hp_is_off = False
        if self.ltn_settings.save_events:
            report_file = (
                self.report_output_dir / f"Report.{report.slot_start_unix_s}.json"
            )
            with report_file.open("w") as f:
                f.write(json.dumps(report.to_dict(), indent=2))

    def process_event(self, event: EventWord) -> None:
        if self.ltn_settings.save_events:
            self.event_dir.mkdir(parents=True, exist_ok=True)
            event_dt = datetime.fromtimestamp(
                event.time_created_ms / 1000, tz=self.timezone
            )
            event_file = (
                self.event_dir
                / f"{event_dt.isoformat()}.{event.type_name}.uid[{event.message_id}].json"
            )
            with event_file.open("w") as f:
                f.write(json.dumps(event.to_dict(), indent=2))

    def snap(self) -> None:
        self.send_to_scada(SendSnap(from_g_node_alias=self.alias))

    def send_layout(self) -> None:
        self.send_to_scada(
            SendLayout(
                from_g_node_alias=self.alias,
                message_created_ms=self.plant_clock.now_ms(),
            )
        )
        self.log("Requesting layout")

    async def main_loop(self) -> None:
        async with aiohttp.ClientSession() as session:
            await self.main_cycle(session)

    async def main_cycle(self, session: aiohttp.ClientSession) -> None:
        await asyncio.sleep(5)
        self.send_layout()

        while not self._stop_requested:
            if not self.accepts_dispatch or self.service_mode != ServiceMode.Heating:
                # SCADA-side: plant is not accepting heating intent
                if self.operating_status is None:
                    self.log(
                        "Main loop idle: waiting for gw.house.operating.status from SCADA "
                        "(accepts_dispatch stays false until then)"
                    )
                else:
                    self.log(
                        f"Main loop idle: accepts_dispatch={self.accepts_dispatch}, "
                        f"service_mode={self.service_mode.value} "
                        "(need accepts_dispatch and Heating to run FLO)"
                    )
                await asyncio.sleep(self.MAIN_LOOP_SLEEP_SECONDS)
                continue

            # One request per slot, bid_lead_s before it starts: the store
            # is measured then, the request goes to the FLO, and the bid
            # goes off its answer when it arrives. The request stays until
            # the next slot's replaces it, so the clearing price at the slot
            # start still finds it, and a late answer is dropped against it.
            now_s = self.plant_clock.now()
            slot_start_s = next_slot_start_s(now_s)
            to_slot_s = seconds_to_slot_start(now_s)
            if to_slot_s > self.bid_lead_s:
                self.log(
                    f"Waiting until {self.bid_lead_s} s before the slot to request "
                    f"the FLO ({to_slot_s} s to go)."
                )
            elif (
                self.flo_params is not None
                and self.flo_params.start_unix_s == slot_start_s
            ):
                if self.recommendation is None:
                    self.log(
                        f"Requested the FLO for slot {slot_start_s}; no answer yet."
                    )
                else:
                    self.log(f"Bid for slot {slot_start_s} is placed.")
            else:
                try:
                    await self.run_flo(session, slot_start_s)
                except Exception as e:
                    self.logger.exception("[ltn] Exception running the FLO! %s", e)
                    self.log(f"Exception running the FLO! {e!r}")

            if self.contract_handler.latest_hb is None:
                self.log("No active contract.")

            await asyncio.sleep(self.MAIN_LOOP_SLEEP_SECONDS)

    async def run_flo(self, session: aiohttp.ClientSession, slot_start_s: int) -> None:
        """Build the request for the slot and send it to the FLO.

        Async because it fetches the forecasts; the FLO answers over the
        broker and `process_flo_recommendation` bids off the answer.
        """
        await self.get_weather(session)
        await self.get_price_forecast()

        if not self.layout_lite:
            self.log(
                "Do not have layout lite from scada so not running the FLO... must not be connected!!"
            )
            return

        if not self.accepts_dispatch or self.service_mode != ServiceMode.Heating:
            # A known, occasional race: the main loop checked this before
            # calling, and the scada's posture changed while the forecasts
            # were fetched. Logged, not a glitch; nothing is wrong.
            self.log(
                "Not asking the FLO: the scada no longer accepts dispatch in "
                "Heating mode."
            )
            return

        self.log("Finding thermocline position and top temperature")
        result = await self.get_three_layer_storage_model()
        self.log(f"{self.seasonal_storage_mode.value} model: {result}")

        if result is None:
            self.log("Get thermocline and centroid failed! Not running FLO!")
            return
        t, m, b, th1, th2 = result

        buffer_available_kwh = await self.get_buffer_available_kwh()
        if self.price_forecast is None:
            self.log("Not running flo - no price forecast")
            return
        if self.weather_forecast is None:
            self.log("Not running flo - no weather forecast")
            return
        try:
            self.flo_params = build_flo_params(
                ops=self.ops,
                water_store=self.water_store,
                storage_mode=self.seasonal_storage_mode,
                forecast=self.weather_forecast,
                now_s=self.plant_clock.now(),
                horizon_hours=self.flo_horizon_hours,
                lmp_usd_mwh=self.price_forecast.lmp_usd_per_mwh,
                dist_usd_mwh=self.price_forecast.dp_usd_per_mwh,
                store=StoreState(t, m, b, th1, th2, hp_on=not self.hp_is_off),
                buffer_available_kwh=buffer_available_kwh,
                g_node_alias=self.alias,
                start_unix_s=slot_start_s,
                created_ms=self.plant_clock.now_ms(),
                previous=previous_run_for(self.previous_run, slot_start_s),
            )
        except SeasonalFillForecast as e:
            self.log(f"Not running flo - {e}")
            return
        self.log(f"FloParams:\n {self.flo_params.to_bytes().decode()}\n")
        self.log(
            f"Settings ask for a {self.flo_horizon_hours} hour horizon; using {len(self.flo_params.step_duration_minutes)}"
        )
        self.recommendation = None
        self.send_to_scada(self.flo_params)
        self.send_to_flo(self.flo_params)
        self.log(f"Requested the FLO for slot {slot_start_s} from {self.flo_alias}")

    def latest_contract_is_live(self) -> bool:
        """Whether the latest bid is for the slot the plant clock is in."""
        bid = self.contract_handler.latest_bid
        if not bid:
            return False
        if not slot_is_current(bid.market_slot_name, self.plant_clock.now()):
            self.log(
                f"Bid time mismatch: bid for {bid.market_slot_name} is not for "
                "the current hour"
            )
            return False
        return True

    def process_latest_price(self, payload: LatestPrice) -> None:
        self.heard.append(LATEST_PRICE_TYPE)
        self.contract_handler.latest_price = payload
        self.log("Received latest price")
        if self.contract_handler.latest_bid is None:
            self.log("Ignoring - no bid exists")
            return

        # Validate bid timeframe
        if not self.latest_contract_is_live():
            glitch = Glitch(
                from_g_node_alias=self.alias,
                node=LTN_NODE_NAME,
                type=LogLevel.Warning,
                summary="Invalid bid timeframe",
                details=f"Stale bid detected. Bid slot start: {self.contract_handler.latest_bid.market_slot_name}. Current price: {payload.market_slot_name}",
                created_ms=self.plant_clock.now_ms(),
            )
            self.contract_handler.latest_bid = None
            self.send_to_scada(glitch)
            self.log("Sent glitch for invalid bid timeframe and set latest bid to None")
            return

        if (
            datetime.now(self.timezone).minute != 0
            and datetime.now(self.timezone).second <= 5
        ):
            self.log(
                "The latest price was not received within the first 5 seconds of the hour. Abort."
            )
            return

        pq_pairs = self.contract_handler.latest_bid.pq_pairs
        self.log(
            f"Sorted pq pairs: {sorted((x.price_x1000, x.quantity_x1000) for x in pq_pairs)} (x1000)"
        )
        self.log(f"Payload price: {payload.price_times1000} (x1000)")
        # Quantity is AvgkW, so QuantityX1000 is avg_w
        assert (
            self.contract_handler.latest_bid.quantity_unit == MarketQuantityUnit.AvgkW
        )
        cleared = cleared_pair_index(pq_pairs, payload.price_times1000)
        avg_w = pq_pairs[cleared].quantity_x1000
        self.log(f"Decided on quantity: {avg_w} WattHours")
        self.keep_cleared_path(cleared)

        # 1 hour
        self.contract_handler.next_contract_energy_wh = avg_w * 1
        if self.contract_handler.next_contract_energy_wh < 1000:
            self.contract_handler.next_contract_energy_wh = 0

        if self.contract_handler.latest_hb:
            self.contract_handler.start_completing_old_contract()
        elif self.contract_handler.can_create_contract():
            self.contract_handler.create_new_contract()
        else:
            self.log("Why am I here")

    def keep_cleared_path(self, cleared: int) -> None:
        """The path the cleared pair leads to, with the price the request
        forecast for the slot after it: saved as the next run's previous
        run, and copied to the scada for the record."""
        if self.recommendation is None or self.flo_params is None:
            self.log(
                "No recommendation held for this slot; no previous run for the next"
            )
            return
        path: FloOptimalPath = self.recommendation.optimal_path_list[cleared]
        if path.start_unix_s != self.flo_params.start_unix_s:
            self.log(
                f"Recommendation is for slot {path.start_unix_s}, the request for "
                f"{self.flo_params.start_unix_s}; no previous run for the next"
            )
            return
        self.previous_run = PreviousRun(
            path=path,
            elec_usd_mwh_now=float(
                self.flo_params.lmp_usd_mwh_list[1]
                + self.flo_params.dist_usd_mwh_list[1]
            ),
        )
        save_previous_run(self.previous_run_file, self.previous_run)
        self.send_to_scada(path)
        self.log(
            f"Kept the cleared path: {path.hp_avg_w_list[:6]} W, store {path.store_wh_list[:6]} Wh"
        )

    def fill_missing_store_temps(self) -> None:
        """Scrub implausible store layers and fill the missing ones from
        below (`store_temps.scrub_and_fill_store_temps`, the pass the
        scada runs too)."""
        all_store_layers = sorted([
            x for x in (self.tank_temp_channel_names or []) if "tank" in x
        ])
        scrub_and_fill_store_temps(
            self.latest_temps_f, all_store_layers, STORE_COLD_PIPE
        )
        self.latest_temps_f = {
            k: self.latest_temps_f[k] for k in sorted(self.latest_temps_f)
        }

    def get_latest_temperatures(self):
        if self.tank_temp_channel_names is None:
            self.temperatures_available = False
            self.log(
                "Can't get latest temperatures, don't have temperature channel names!"
            )
            return
        temps = {}
        for ch_name in self.tank_temp_channel_names:
            raw = self.latest_channel_values.get(ch_name)
            if raw is None:
                continue
            try:
                encoding = self.layout.encoding(ch_name)
                if encoding is None:
                    raise Exception(
                        f"temperature channels should have units! {ch_name}"
                    )
                temp_f = convert_temp_to_f(raw=raw, encoding=encoding)
            except Exception as e:
                note = f"Temperature conversion failed for {ch_name}: {e}"
                self.log(note)
                continue
            temps[ch_name] = temp_f

        self.latest_temps_f = temps.copy()
        self.fill_missing_store_temps()
        self.temperatures_available = (
            set(self.tank_temp_channel_names) <= self.latest_temps_f.keys()
        )

    async def get_RSWT(self, minus_deltaT=False):
        if self.weather_forecast is None:
            raise Exception("weather_forecast cannot be None here!")
        try:
            curve = self.ops.heating_curve
            alpha = curve.alpha_times10 / 10
            beta = curve.beta_times100 / 100
            gamma = curve.gamma_ex6 / 1e6
            weather = self.weather_forecast.next_hours(1, self.plant_clock.now())
            oat = weather.oat_f[0]
            ws = weather.wind_speed_mph[0]
            r = alpha + beta * oat + gamma * ws * (65 - oat)
            rhp = r if r > 0 else 0
            intermediate_rswt = curve.intermediate_rswt_f
            dd_rswt = curve.dd_rswt_f
            intermediate_power = curve.intermediate_power_kw
            dd_power = curve.dd_power_kw
            no_power_rswt = -alpha / beta
            x_rswt = np.array([no_power_rswt, intermediate_rswt, dd_rswt])
            y_hpower = np.array([0, intermediate_power, dd_power])
            A = np.vstack([x_rswt**2, x_rswt, np.ones_like(x_rswt)]).T
            a, b, c = np.linalg.solve(A, y_hpower)
            c2 = c - rhp
            rswt = round((-b + (b**2 - 4 * a * c2) ** 0.5) / (2 * a), 2)
            deltaT = (
                curve.dd_delta_t_f / curve.dd_power_kw * (a * rswt**2 + b * rswt + c)
            )
            deltaT = deltaT if deltaT > 0 else 0
            if minus_deltaT:
                return rswt - deltaT
            return rswt
        except Exception:
            self.log("Could not find RSWT!")
            return None

    async def kmeans(self, data, k=3, max_iters=100, tol=1e-4):
        data = np.array(data).reshape(-1, 1)
        centroids = data[np.random.choice(len(data), k, replace=False)]
        labels = np.zeros(len(data), dtype=int)
        for _ in range(max_iters):
            labels = np.argmin(np.abs(data - centroids.T), axis=1)
            new_centroids = np.zeros_like(centroids)
            for i in range(k):
                cluster_points = data[labels == i]
                if len(cluster_points) > 0:
                    new_centroids[i] = cluster_points.mean()
                else:
                    new_centroids[i] = data[np.random.choice(len(data))]
            if np.all(np.abs(new_centroids - centroids) < tol):
                break
            centroids = new_centroids
        return labels

    async def get_three_layer_storage_model(
        self,
    ) -> Optional[Tuple[float, float, float, int, int]]:
        # Get all storage tank temperatures in a dict
        if self.tank_temp_channel_names is None:
            self.send_layout()
            await asyncio.sleep(5)
        self.get_latest_temperatures()
        if not self.temperatures_available or self.tank_temp_channel_names is None:
            self.log(
                "Not enough tank temperatures available to compute top temperature and thermocline!"
            )
            return None
        all_layers = sorted([
            x
            for x in self.tank_temp_channel_names
            if (
                "buffer"
                if self.seasonal_storage_mode == SeasonalStorageMode.BufferOnly
                else "tank"
            )
            in x
        ])
        try:
            tank_temps = {key: self.latest_temps_f[key] for key in all_layers}
        except KeyError as e:
            self.log(
                f"Failed to get all the tank temps in get_three_layer_storage_model! Bailing on process {e}"
            )
            return None

        if self.seasonal_storage_mode == SeasonalStorageMode.BufferOnly:
            top_temp = round(tank_temps[BUFFER_DEPTH_CHANNELS[0]], 1)
            middle_temp = round(tank_temps[BUFFER_DEPTH_CHANNELS[1]], 1)
            bottom_temp = round(tank_temps[BUFFER_DEPTH_CHANNELS[2]], 1)
            thermocline1 = 1
            thermocline2 = 2
            return top_temp, middle_temp, bottom_temp, thermocline1, thermocline2

        # Process layer temperatures
        layer_temps = [tank_temps[key] for key in tank_temps]
        iter_count = 0
        while sorted(layer_temps, reverse=True) != layer_temps and iter_count < 20:
            iter_count += 1
            layer_temps = []
            for layer in tank_temps:
                if layer_temps:
                    if tank_temps[layer] > layer_temps[-1]:
                        mean = round((layer_temps[-1] + tank_temps[layer]) / 2)
                        layer_temps[-1] = mean
                        layer_temps.append(mean)
                    else:
                        layer_temps.append(tank_temps[layer])
                else:
                    layer_temps.append(tank_temps[layer])
            for i, layer in enumerate(tank_temps):
                tank_temps[layer] = layer_temps[i]
            if iter_count == 20:
                layer_temps = sorted(layer_temps, reverse=True)

        # Cluster 10 times and select the result with the highest top temperature
        data = layer_temps.copy()
        clustering_runs = []
        for i in range(10):
            labels = await self.kmeans(data, k=3)
            cluster_0 = sorted(
                [data[i] for i in range(len(data)) if labels[i] == 0], reverse=True
            )
            cluster_1 = sorted(
                [data[i] for i in range(len(data)) if labels[i] == 1], reverse=True
            )
            cluster_2 = sorted(
                [data[i] for i in range(len(data)) if labels[i] == 2], reverse=True
            )
            cluster_top = max(
                cluster_0,
                cluster_1,
                cluster_2,
                key=lambda x: np.mean(x) if len(x) > 0 else 0,
            )
            top_temp = sum(cluster_top) / len(cluster_top)
            clustering_runs.append({
                "cluster_0": cluster_0,
                "cluster_1": cluster_1,
                "cluster_2": cluster_2,
                "top_temp": top_temp,
            })
        best_run = max(clustering_runs, key=lambda x: x["top_temp"])
        cluster_0 = best_run["cluster_0"]
        cluster_1 = best_run["cluster_1"]
        cluster_2 = best_run["cluster_2"]

        # Dealing with 3 clusters
        if cluster_0 and cluster_1 and cluster_2:
            cluster_top = max(
                cluster_0, cluster_1, cluster_2, key=lambda x: sum(x) / len(x)
            )
            cluster_bottom = min(
                cluster_0, cluster_1, cluster_2, key=lambda x: sum(x) / len(x)
            )
            cluster_middle = [
                cluster_x
                for cluster_x in [cluster_0, cluster_1, cluster_2]
                if cluster_x != cluster_top and cluster_x != cluster_bottom
            ][0]

            thermocline1 = max(1, len(cluster_top))
            thermocline2 = thermocline1 + len(cluster_middle)

            top_temp = round(sum(cluster_top) / len(cluster_top))
            middle_temp = round(sum(cluster_middle) / len(cluster_middle))
            bottom_temp = round(sum(cluster_bottom) / len(cluster_bottom))
            self.log(
                f"Storage model: {top_temp}({thermocline1}){middle_temp}({thermocline2}){bottom_temp} ({len(layer_temps)} layers)"
            )
            return top_temp, middle_temp, bottom_temp, thermocline1, thermocline2

        # Dealing with less than 3 clusters
        else:
            if cluster_0 and cluster_2:
                cluster_1 = cluster_2
            elif cluster_1 and cluster_2:
                cluster_0 = cluster_2
            # Two clusters
            if cluster_0 and cluster_1:
                cluster_top = max(cluster_0, cluster_1, key=lambda x: sum(x) / len(x))
                cluster_bottom = min(
                    cluster_0, cluster_1, key=lambda x: sum(x) / len(x)
                )
                thermocline1 = len(cluster_top)
                top_temp = round(sum(cluster_top) / len(cluster_top))
                bottom_temp = round(sum(cluster_bottom) / len(cluster_bottom))
                self.log(
                    f"Storage model: {top_temp}({thermocline1}){bottom_temp} ({len(layer_temps)} layers)"
                )
                return top_temp, top_temp, bottom_temp, thermocline1, thermocline1
            # Single cluster
            else:
                cluster_top = max(cluster_0, cluster_1, cluster_2, key=lambda x: len(x))
                top_temp = round(sum(cluster_top) / len(cluster_top))
                thermocline1 = len(layer_temps)
                self.log(
                    f"Storage model: {top_temp}({thermocline1}) ({len(layer_temps)} layers)"
                )
                return top_temp, top_temp, top_temp, thermocline1, thermocline1

    async def get_buffer_available_kwh(self):
        if self.keep_buffer_full:
            return 0
        if self.seasonal_storage_mode == SeasonalStorageMode.BufferOnly:
            return 0
        if self.tank_temp_channel_names is None:
            self.send_layout()
            await asyncio.sleep(5)
        self.get_latest_temperatures()
        buffer_temperatures: Dict[str, float] = {
            k: v
            for k, v in self.latest_temps_f.items()
            if "buffer" in k and v is not None
        }
        self.log(f"Buffer temperatures: {buffer_temperatures}")
        if not buffer_temperatures:
            self.log(
                "Missing temperatures in get_buffer_available_kwh, returning 0 kWh"
            )
            return 0
        try:
            rswt = await self.get_RSWT(minus_deltaT=False)
            rswt_minus_deltaT = await self.get_RSWT(minus_deltaT=True)
            if rswt is None or rswt_minus_deltaT is None:
                raise ValueError("no RSWT")
            m_layer_kg = 120 / 4 * 3.785
            buffer_available_energy = 0
            for bl in buffer_temperatures:
                if buffer_temperatures[bl] >= rswt_minus_deltaT:
                    buffer_available_energy += (
                        m_layer_kg
                        * 4.187
                        / 3600
                        * (buffer_temperatures[bl] - rswt_minus_deltaT)
                        * 5
                        / 9
                    )
            if round(buffer_available_energy, 2) == 0:
                for bl in buffer_temperatures:
                    buffer_available_energy += (
                        -m_layer_kg
                        * 4.187
                        / 3600
                        * (rswt - buffer_temperatures[bl])
                        * 5
                        / 9
                    )
            self.log(f"Buffer available kWh: {round(buffer_available_energy, 2)}")
            return round(buffer_available_energy, 2)
        except Exception as e:
            self.log(
                f"Something failed in get_buffer_available_kwh ({e}), returning 0 kWh"
            )
            return 0

    async def get_house_available_kwh(self):
        setpoints = {}
        temps = {}
        thermal_mass = {}
        zone_names = []
        for zone_setpoint in [
            x for x in self.latest_channel_values if "zone" in x and "set" in x
        ]:
            zone_name = zone_setpoint.replace("-set", "")
            zone_names.append(zone_name)
            # Get setpoints for each zone
            if self.latest_channel_values[zone_setpoint] is not None:
                setpoints[zone_name] = round(
                    self.latest_channel_values[zone_setpoint] / 1000, 1
                )
            # Get temperatures for each zone
            zone_temp = zone_setpoint.replace("-set", "-temp")
            if (
                zone_temp in self.latest_channel_values
                and self.latest_channel_values[zone_temp] is not None
            ):
                temps[zone_name] = round(
                    self.latest_channel_values[zone_temp] / 1000, 1
                )
            # Get thermal mass for each zone
            zone_name_no_prefix = (
                zone_name[6:] if zone_name[:4] == "zone" else zone_name
            )
            if zone_name_no_prefix in self.layout.zone_list:
                zone_index = self.layout.zone_list.index(zone_name_no_prefix)
                thermal_mass[zone_name] = self.layout.zone_kwh_per_deg_f_list[
                    zone_index
                ]

        self.log(f"Found all zone setpoints: {setpoints}")
        self.log(f"Found all zone temperatures: {temps}")
        self.log(f"Found all zone thermal masses: {thermal_mass}")
        house_availale_kwh = 0
        for zone in zone_names:
            if "zone4" in zone or "upstairs" in zone:
                continue
            if zone in temps and zone in setpoints and zone in thermal_mass:
                house_availale_kwh += thermal_mass[zone] * (
                    temps[zone] - setpoints[zone]
                )
        house_availale_kwh = round(house_availale_kwh, 2)
        self.log(f"House available kWh: {house_availale_kwh}")
        return min(
            0, house_availale_kwh
        )  # TODO: TEMPORARY only consider negative values

    async def get_weather(self, session: aiohttp.ClientSession) -> None:
        """The forecast pair from the weather source."""
        self.weather_forecast = await self.weather_source.forecast(session)

    async def get_price_forecast(self) -> None:
        """Updates self.price_forecast for the start of next hour. All in USD/MWh"""
        prices_file = self.data_dir / "price_forecast.csv"
        try:
            # Get price forecast from the price service API
            url = "https://price-service.electricity.works/hw1-isone-me-versant-keene-ps/gw0-price-forecast"
            async with httpx.AsyncClient() as client:
                response = await client.get(url)
                if response.status_code == 200:
                    self.log(
                        "Successfully received price forecast from the price service API"
                    )
                    data = response.json()
                    self.price_forecast = PriceForecast(
                        dp_usd_per_mwh=data["DistList"],
                        lmp_usd_per_mwh=data["LmpList"],
                    )
                    # Save price forecast to a local CSV file
                    # Check if current hour's data exists in the existing file
                    current_hour_row = None
                    if prices_file.exists():
                        current_hour_timestamp = data["HourStartS"][0] - 3600
                        with open(prices_file, "r", newline="") as f:
                            reader = csv.reader(f)
                            next(reader)  # skip the header row
                            for row in reader:
                                if float(row[0]) == current_hour_timestamp:
                                    current_hour_row = row
                                    break
                    # Write the new file
                    with open(prices_file, "w", newline="") as f:
                        writer = csv.writer(f)
                        writer.writerow(["HourStartS", "DistList", "LmpList"])
                        if current_hour_row:
                            writer.writerow(current_hour_row)
                        for i in range(len(data["DistList"])):
                            writer.writerow([
                                data["HourStartS"][i],
                                data["DistList"][i],
                                data["LmpList"][i],
                            ])
                    self.log(f"Saved price forecast to {prices_file}")
                else:
                    raise Exception(
                        f"Failed to receive price forecast from API, status code: {response.status_code}"
                    )

        except Exception as e:
            self.log(
                f"FAILED to receive price forecast from the price service API and/or to save it to a local CSV: {e}"
            )
            self.log("Trying to read price forecast from the local CSV file")
            try:
                # Read the local CSV file with the latest received price forecast
                with open(prices_file, mode="r", newline="") as file:
                    reader = csv.reader(file)
                    next(reader)
                    rows = list(reader)
                timestamps = [float(row[0]) for row in rows]
                dist_usd_mwh = [float(row[1]) for row in rows]
                lmp_usd_mwh = [float(row[2]) for row in rows]

                self.price_forecast = price_forecast_from(
                    timestamps, dist_usd_mwh, lmp_usd_mwh, self.plant_clock.now()
                )
                self.log("Successfully read price forecast from local CSV.")

            except Exception as e:
                self.log(
                    f"Could not get a price forecast from the local CSV file: {e}."
                )
                await self.send_glitch(
                    f"Failed to read price forecast from local CSV file: {e}",
                    log_level=LogLevel.Error,
                )

    async def send_glitch(self, summary, details="", log_level=LogLevel.Info):
        glitch = Glitch(
            from_g_node_alias=self.alias,
            node=LTN_NODE_NAME,
            type=log_level,
            summary=summary,
            details=details,
            created_ms=self.plant_clock.now_ms(),
        )
        self.send_to_scada(glitch)
        self.log("Sent glitch")

    def log(self, note: str) -> None:
        self.logger.info(f"[ltn] {note}")
