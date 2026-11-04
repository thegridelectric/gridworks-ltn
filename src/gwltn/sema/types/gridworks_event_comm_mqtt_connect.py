from typing import Literal
from gwltn.sema.base import SemaType
from gwltn.sema.property_format import LeftRightDot
from gwltn.sema.property_format import UTCMilliseconds
from gwltn.sema.property_format import UUID4Str


class GridworksEventCommMqttConnect(SemaType):
    """Sema: https://schemas.electricity.works/types/gridworks.event.comm.mqtt.connect/001"""

    message_id: UUID4Str
    time_created_ms: UTCMilliseconds
    src: LeftRightDot
    peer_name: str
    type_name: Literal["gridworks.event.comm.mqtt.connect"] = (
        "gridworks.event.comm.mqtt.connect"
    )
    version: Literal["001"] = "001"
