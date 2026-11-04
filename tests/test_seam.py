"""Against the real dev broker, across the MQTT seam the scada uses: a
paho stand-in scada publishes on the MQTT door what the scada's link
layer sends upstream, and the gwbase LTN on the AMQP face answers on the
topic the scada subscribes to, inside the scada's 5 s ack timer, and
takes the house's layout and operating status. A stand-in maker's
`latest.price` on its mic exchange reaches the LTN, and a stand-in FLO's
direct `flo.recommendation` reaches it over the Flo-to-LeafTransactiveNode
edge. A second stand-in scada's traffic is asserted undelivered.
Self-skips without a broker."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from queue import Empty, Queue

import paho.mqtt.client as mqtt
import pika
import pytest
from gwbase.transport_encoding import (
    TransportClass,
    json_direct_routing_key,
    routing_code,
)
from paho.mqtt.enums import CallbackAPIVersion

from gwltn.config import GwltnSettings
from gwltn.layout import load_layout
from gwltn.ltn_actor import LtnActor
from gwltn.market import market_slot_name
from gwltn.sema.base import SemaType
from gwltn.sema.enums import (
    MarketPriceUnit,
    MarketQuantityUnit,
    SeasonalStorageMode,
    ServiceMode,
    StandbyPosture,
    TaValidationState,
    TopState,
)
from gwltn.sema.types import (
    FloOptimalPath,
    FloRecommendation,
    GridworksEventStartup,
    GridworksPing,
    HouseOperatingStatus,
    LatestPrice,
    LayoutLite,
    PriceQuantityUnitless,
)
from tests.conftest import (
    CONFIG,
    FLO_ALIAS,
    OTHER_SCADA_ALIAS,
    SCADA_ALIAS,
    broker_up,
)

MQTT_PORT = 1885
AMQP_URL = "amqp://smqPublic:smqPublic@localhost:5672/d1__1"
ACK_TIMER_S = 5.0  # gwproactor ProactorSettings ack timeout


def dashed(alias: str) -> str:
    return alias.replace(".", "-")


def wait_for(predicate, timeout_s: float, what: str) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def willow_layout_lite() -> LayoutLite:
    """The layout.lite the willow scada would send, from the same pair the
    LTN boots on."""
    layout = load_layout(
        CONFIG / "gw.house0.willow.layout.json",
        CONFIG / "gw.house0.willow.operational.params.json",
    )
    return LayoutLite(
        from_g_node_alias=layout.scada_alias,
        hardware_layout_type_name=layout.layout_type_name,
        zone_list=layout.zone_list,
        critical_zone_list=layout.critical_zone_list,
        sh_nodes=list(layout.word.sh_nodes),
        data_channels=list(layout.word.data_channels),
        derived_channels=list(layout.word.derived_channels),
        state_machines=[],
        created_ms=int(time.time() * 1000),
        message_id=str(uuid.uuid4()),
    )


def heating_status() -> HouseOperatingStatus:
    return HouseOperatingStatus(
        scada_alias=SCADA_ALIAS,
        validation_state=TaValidationState.ValidatedRealAssetAndGps,
        standby=False,
        standby_posture=StandbyPosture.MonitorOnly,
        seasonal_storage_mode=SeasonalStorageMode.AllTanks,
        service_mode=ServiceMode.Heating,
        accepts_dispatch=True,
        top_state=TopState.Auto,
        scada_blind=False,
        ltn_dispatching=False,
        created_ms=int(time.time() * 1000),
    )


def recommendation(alias: str, start_s: int) -> FloRecommendation:
    pair = PriceQuantityUnitless(price_x1000=10_000, quantity_x1000=5_000)
    return FloRecommendation(
        bidder_alias=alias,
        pq_pairs=[pair],
        price_unit=MarketPriceUnit.USDPerMWh,
        quantity_unit=MarketQuantityUnit.AvgkW,
        optimal_path_list=[
            FloOptimalPath(
                start_unix_s=start_s,
                step_duration_minutes=[60],
                hp_avg_w_list=[pair.quantity_x1000],
                store_wh_list=[0],
            )
        ],
        flo_name="bruce.dijkstra",
        flo_repo="thdfw/optimal-flexibility",
        flo_git_commit="0123456789abcdef0123456789abcdef01234567",
        created_ms=start_s * 1000,
    )


class StandInScada:
    """A scada's upstream link, as paho sees it: publishes on
    `gw/<scada>/to/ltn/<type>` the `gw` envelope the proactor wraps a word
    in, and hears `gw/<ltn>/to/s/#`."""

    def __init__(self, alias: str, ltn_alias: str, *, listens: bool = True) -> None:
        self.alias = alias
        self.heard: Queue[tuple[str, dict]] = Queue()
        self.client = mqtt.Client(
            CallbackAPIVersion.VERSION2, client_id=f"test-{dashed(alias)}"
        )
        self.client.username_pw_set("smqPublic", "smqPublic")
        self.client.on_message = lambda c, u, m: self.heard.put((
            m.topic,
            json.loads(m.payload),
        ))
        self.client.connect("localhost", MQTT_PORT)
        if listens:
            self.client.subscribe(f"gw/{dashed(ltn_alias)}/to/s/#")
        self.client.loop_start()

    def publish(self, word: SemaType, *, ack_required: bool = False) -> str:
        """Publish the word in the scada's envelope; returns the envelope's
        MessageId (the word's own, when it has one)."""
        payload = word.to_dict()
        message_id = payload.get("MessageId", str(uuid.uuid4()))
        envelope = {
            "TypeName": "gw",
            "Header": {
                "Src": self.alias,
                "Dst": "ltn",
                "MessageType": word.type_name,
                "MessageId": message_id,
                "AckRequired": ack_required,
                "TypeName": "gridworks.header",
                "Version": "001",
            },
            "Payload": payload,
        }
        topic = f"gw/{dashed(self.alias)}/to/ltn/{dashed(word.type_name)}"
        self.client.publish(topic, json.dumps(envelope).encode())
        return message_id

    def next_heard(self, timeout_s: float) -> tuple[str, dict] | None:
        try:
            return self.heard.get(timeout=timeout_s)
        except Empty:
            return None

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


def publish_recommendation(flo_alias: str, recommendation: FloRecommendation) -> None:
    """The FLO's direct reply on its own mic exchange, as gwbase routes it:
    the Flo-to-LeafTransactiveNode edge forwards it to the LTN's queue."""
    connection = pika.BlockingConnection(pika.URLParameters(AMQP_URL))
    try:
        channel = connection.channel()
        channel.basic_publish(
            exchange=f"{routing_code(TransportClass.Flo)}mic_tx",
            routing_key=json_direct_routing_key(
                from_alias=flo_alias,
                from_class_token=routing_code(TransportClass.Flo),
                type_name=recommendation.type_name,
                to_class_token=routing_code(TransportClass.LeafTransactiveNode),
                to_alias=recommendation.bidder_alias,
            ),
            body=recommendation.to_bytes(),
        )
    finally:
        connection.close()


def publish_latest_price(price: LatestPrice) -> None:
    """The maker's broadcast on its own mic exchange, as gwbase routes it."""
    connection = pika.BlockingConnection(pika.URLParameters(AMQP_URL))
    try:
        channel = connection.channel()
        channel.basic_publish(
            exchange="mmmic_tx",
            routing_key=f"rjb.{dashed(price.from_g_node_alias)}.mm.latest-price",
            body=price.to_bytes(),
        )
    finally:
        connection.close()


@pytest.mark.broker
def test_the_scada_seam(settings: GwltnSettings, tmp_path: Path) -> None:
    if not (broker_up(5672) and broker_up(MQTT_PORT)):
        pytest.skip("no dev broker on localhost:5672 + 1885")
    ltn = settings.service_alias
    scada = StandInScada(SCADA_ALIAS, ltn)
    other = StandInScada(OTHER_SCADA_ALIAS, ltn, listens=False)
    actor = LtnActor(settings=settings)
    actor.start()
    try:
        wait_for(lambda: actor.consuming, 15, "the actor to consume")

        # Ack-required messages are acked inside the scada's timer.
        event = GridworksEventStartup(
            message_id=str(uuid.uuid4()),
            time_created_ms=int(time.time() * 1000),
            src=SCADA_ALIAS,
        )
        ping = GridworksPing(message_id=str(uuid.uuid4()))
        for word in (event, ping):
            sent = time.time()
            message_id = scada.publish(word, ack_required=True)
            heard = scada.next_heard(ACK_TIMER_S)
            assert heard is not None, f"no ack for {word.type_name} in {ACK_TIMER_S} s"
            topic, body = heard
            latency_s = time.time() - sent
            assert topic == f"gw/{dashed(ltn)}/to/s/gridworks-ack"
            assert body["TypeName"] == "gw"
            assert body["Header"]["Src"] == ltn
            assert body["Header"]["Dst"] == "s"
            assert body["Header"]["MessageType"] == "gridworks.ack"
            assert body["Payload"]["TypeName"] == "gridworks.ack"
            assert body["Payload"]["AckMessageID"] == message_id
            print(f"ack for {word.type_name} in {latency_s * 1000:.0f} ms")
        assert actor.acked == 2

        # The house's layout and operating status reach the LTN's state.
        scada.publish(willow_layout_lite())
        scada.publish(heating_status())
        wait_for(lambda: actor.accepts_dispatch, 5, "the operating status")
        assert actor.layout_lite is not None
        assert actor.contract_handler.layout_received
        assert actor.tank_temp_channel_names is not None
        assert actor.service_mode is ServiceMode.Heating
        assert actor.heard == [
            "gridworks.event.startup",
            "gridworks.ping",
            "layout.lite",
            "gw.house.operating.status",
        ]

        # The maker's latest.price arrives on its mic exchange; with no bid
        # held it is kept and nothing is sent.
        slot_start_s = int(time.time()) // 3600 * 3600
        price = LatestPrice(
            from_g_node_alias=actor.ops.market_maker_alias,
            price_times1000=86_090,
            price_unit=MarketPriceUnit.USDPerMWh,
            market_slot_name=market_slot_name(actor.ops, slot_start_s),
            message_id=str(uuid.uuid4()),
        )
        publish_latest_price(price)
        wait_for(
            lambda: actor.contract_handler.latest_price is not None,
            5,
            "the latest price",
        )
        assert actor.contract_handler.latest_price == price
        assert actor.contract_handler.latest_bid is None

        # The FLO's direct reply reaches the LTN over the edge; with no
        # request open it is dropped and no bid goes out.
        publish_recommendation(FLO_ALIAS, recommendation(ltn, slot_start_s + 3600))
        wait_for(
            lambda: actor.heard[-1:] == ["flo.recommendation"], 5, "the FLO's reply"
        )
        assert actor.recommendation is None
        assert actor.contract_handler.latest_bid is None

        # Fan-in: another house's scada on the same broker is not heard.
        other.publish(GridworksPing(message_id=str(uuid.uuid4())), ack_required=True)
        assert scada.next_heard(2.0) is None
        assert actor.acked == 2
        assert actor.heard[-1] == "flo.recommendation"
    finally:
        actor.stop()
        scada.close()
        other.close()
