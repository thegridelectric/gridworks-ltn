"""The LTN forms the slot it bids on from the ops word."""

import json
from pathlib import Path

from gwltn.market import answers_request, bid_from_recommendation, market_slot_name
from gwltn.sema.enums import MarketPriceUnit, MarketQuantityUnit
from gwltn.sema.types import (
    FloOptimalPath,
    FloRecommendation,
    OperationalParams,
    PriceQuantityUnitless,
)
from tests.test_flo_request import ALIAS, START_S, build

CONFIG = Path(__file__).resolve().parent / "config"


def ops() -> OperationalParams:
    return OperationalParams.from_dict(
        json.loads((CONFIG / "gw.house0.willow.operational.params.json").read_text())
    )


def test_the_slot_is_the_ops_words_maker_and_product_at_the_start() -> None:
    assert (
        market_slot_name(ops(), 1762178400)
        == "e.rt60gate5.hw1.isone.me.versant.keene.1762178400"
    )


def recommendation(alias: str, start_s: int) -> FloRecommendation:
    pairs = [
        PriceQuantityUnitless(price_x1000=30_000, quantity_x1000=0),
        PriceQuantityUnitless(price_x1000=10_000, quantity_x1000=5_000),
    ]
    return FloRecommendation(
        bidder_alias=alias,
        pq_pairs=pairs,
        price_unit=MarketPriceUnit.USDPerMWh,
        quantity_unit=MarketQuantityUnit.AvgkW,
        optimal_path_list=[
            FloOptimalPath(
                start_unix_s=start_s,
                step_duration_minutes=[60],
                hp_avg_w_list=[p.quantity_x1000],
                store_wh_list=[0],
            )
            for p in pairs
        ],
        flo_name="bruce.dijkstra",
        flo_repo="thdfw/optimal-flexibility",
        flo_git_commit="0123456789abcdef0123456789abcdef01234567",
        created_ms=START_S * 1000,
    )


def test_the_answer_is_paired_by_bidder_and_slot() -> None:
    params = build()
    assert answers_request(params, recommendation(ALIAS, START_S))
    assert not answers_request(params, recommendation(ALIAS, START_S + 3600))
    assert not answers_request(
        params, recommendation("d1.isone.ct.newhaven.oak1", START_S)
    )


def test_the_bid_is_the_recommendations_curve_for_the_requests_slot() -> None:
    params = build()
    bid = bid_from_recommendation(ops(), params, recommendation(ALIAS, START_S))
    assert bid.bidder_alias == ALIAS
    assert bid.market_slot_name == market_slot_name(ops(), START_S)
    assert [(p.price_x1000, p.quantity_x1000) for p in bid.pq_pairs] == [
        (30_000, 0),
        (10_000, 5_000),
    ]
    assert bid.quantity_unit is MarketQuantityUnit.AvgkW
