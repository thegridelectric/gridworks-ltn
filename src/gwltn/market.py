"""The market facts the LTN forms from its ops word, the slot it bids on,
and the clearing rule that turns its bid curve and a cleared price into
a quantity."""

from collections.abc import Sequence

from gwltn.sema.enums import MarketPriceUnit, MarketQuantityUnit
from gwltn.sema.property_format import MarketSlotName, is_market_slot_name
from gwltn.sema.types import (
    Bid,
    FloParams,
    FloRecommendation,
    OperationalParams,
    PriceQuantityUnitless,
)


def market_slot_name(ops: OperationalParams, slot_start_s: int) -> MarketSlotName:
    """The energy slot on the ops word's maker and product starting at
    `slot_start_s`."""
    return is_market_slot_name(
        f"e.{ops.market_product_name.value}.{ops.market_maker_alias}.{slot_start_s}"
    )


def cleared_pair_index(
    pq_pairs: Sequence[PriceQuantityUnitless], price_x1000: int
) -> int:
    """The index of the pair a bid curve commits to at a cleared price.

    A pair holds for every price above the next lower pair's price, up to
    and including its own, so the pair that applies is the lowest-priced
    one at or above the cleared price. A price above every pair takes the
    highest-priced pair, the curve's ceiling.
    """
    if not pq_pairs:
        raise ValueError("A bid curve needs at least one pair")
    at_or_above = [i for i, p in enumerate(pq_pairs) if p.price_x1000 >= price_x1000]
    if at_or_above:
        return min(at_or_above, key=lambda i: pq_pairs[i].price_x1000)
    return max(range(len(pq_pairs)), key=lambda i: pq_pairs[i].price_x1000)


# The bid is not yet signed; the maker takes the stub.
SIGNED_MARKET_FEE_TXN_STUB = "BogusAlgoSignature"


def answers_request(params: FloParams, recommendation: FloRecommendation) -> bool:
    """Whether the recommendation is the FLO's answer to this request: no
    correlation id travels, so the pairing is the bidder and the slot of
    the first path."""
    return (
        recommendation.bidder_alias == params.g_node_alias
        and bool(recommendation.optimal_path_list)
        and recommendation.optimal_path_list[0].start_unix_s == params.start_unix_s
    )


def bid_from_recommendation(
    ops: OperationalParams, params: FloParams, recommendation: FloRecommendation
) -> Bid:
    """The LTN's bid for the request's slot: the recommendation's curve,
    quantities as average kW."""
    return Bid(
        bidder_alias=params.g_node_alias,
        market_slot_name=market_slot_name(ops, params.start_unix_s),
        pq_pairs=list(recommendation.pq_pairs),
        injection_is_positive=True,
        price_unit=MarketPriceUnit.USDPerMWh,
        quantity_unit=MarketQuantityUnit.AvgkW,
        signed_market_fee_txn=SIGNED_MARKET_FEE_TXN_STUB,
    )
