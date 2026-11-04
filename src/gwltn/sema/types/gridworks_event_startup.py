from typing import Literal
from gwltn.sema.base import SemaType
from gwltn.sema.property_format import LeftRightDot
from gwltn.sema.property_format import UTCMilliseconds
from gwltn.sema.property_format import UUID4Str


class GridworksEventStartup(SemaType):
    """Sema: https://schemas.electricity.works/types/gridworks.event.startup"""

    message_id: UUID4Str
    time_created_ms: UTCMilliseconds
    src: LeftRightDot
    type_name: Literal["gridworks.event.startup"] = "gridworks.event.startup"
