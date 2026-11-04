from typing import Literal
from gwltn.sema.base import SemaType
from gwltn.sema.property_format import UUID4Str


class GridworksAck(SemaType):
    """Sema: https://schemas.electricity.works/types/gridworks.ack"""

    ack_message_i_d: UUID4Str
    type_name: Literal["gridworks.ack"] = "gridworks.ack"
