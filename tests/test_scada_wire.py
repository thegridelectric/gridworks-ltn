"""What the LTN sends the scada is a word in a gw envelope: the envelope's
MessageType is the payload's TypeName, Src is the LTN and Dst is the
scada's node name, and a payload without a TypeName is refused."""

import json

import pytest

from gwltn.scada_wire import SCADA_TOKEN, scada_body, scada_envelope
from gwltn.sema.types import FloOptimalPath

LTN = "d1.isone.ct.newhaven.willow1"


def test_a_word_wraps_under_its_own_type_name() -> None:
    path = FloOptimalPath(
        start_unix_s=1762009200,
        step_duration_minutes=[60, 60],
        hp_avg_w_list=[3000, 0],
        store_wh_list=[38500, 31200],
    )
    d = path.to_dict()
    body = json.loads(scada_body(from_alias=LTN, type_name=d["TypeName"], payload=d))
    assert body["Header"]["MessageType"] == "flo.optimal.path"
    assert body["Header"]["Src"] == LTN
    assert body["Header"]["Dst"] == SCADA_TOKEN
    assert body["Payload"]["TypeName"] == "flo.optimal.path"
    assert scada_envelope(type_name="flo.optimal.path", from_alias=LTN).routing_key == (
        "gw.d1-isone-ct-newhaven-willow1.to.s.flo-optimal-path"
    )


def test_a_payload_without_a_type_name_is_refused() -> None:
    with pytest.raises(ValueError, match="TypeName"):
        scada_body(
            from_alias=LTN, type_name="flo.optimal.path", payload={"horizon": 48}
        )
