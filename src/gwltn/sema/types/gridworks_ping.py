from typing import Literal
from gwltn.sema.base import SemaType
from gwltn.sema.property_format import UUID4Str


class GridworksPing(SemaType):
    """Sema: https://schemas.electricity.works/types/gridworks.ping"""

    message_id: UUID4Str
    type_name: Literal["gridworks.ping"] = "gridworks.ping"
