"""The LTN's half of the scada's wire: the `gw` envelope on `amq.topic`,
addressed by the token the scada subscribes to, and the two control
words the scada's link layer expects from its upstream peer.
"""

from __future__ import annotations

import uuid

from gwbase.transport_encoding import WrappedRoutingEnvelope
from gwbase.wrapped import wrap_bytes

from gwltn.sema.types import GridworksAck, GridworksPing

# The scada hears `gw/<ltn>/to/s/#`: `s` is its own node name on the wire,
# not a TransportClass token. gwbase treats an unknown short token as
# expected and routes on the inner type.
SCADA_TOKEN = "s"

ACK_TYPE = GridworksAck.type_name_value()
PING_TYPE = GridworksPing.type_name_value()


def scada_envelope(*, type_name: str, from_alias: str) -> WrappedRoutingEnvelope:
    """Routing for a wrapped message from this LTN to its scada:
    `gw.<ltn-dashed>.to.s.<type-dashed>`."""
    return WrappedRoutingEnvelope(
        type_name=type_name, from_alias=from_alias, to_class_token=SCADA_TOKEN
    )


def scada_body(
    *,
    from_alias: str,
    type_name: str,
    payload: dict,
    ack_required: bool = False,
) -> bytes:
    """The `gw` envelope the scada's codec accepts: Src this LTN, Dst `s`."""
    return wrap_bytes(
        src=from_alias,
        dst=SCADA_TOKEN,
        inner_type_name=type_name,
        inner_payload_dict=payload,
        ack_required=ack_required,
    )


def ack_payload(message_id: str) -> dict:
    return GridworksAck(ack_message_i_d=message_id).to_dict()


def ping_payload() -> dict:
    return GridworksPing(message_id=str(uuid.uuid4())).to_dict()
