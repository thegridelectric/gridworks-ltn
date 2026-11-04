from typing import Literal
from gwltn.sema.base import SemaType
from gwltn.sema.enums import LogLevel
from gwltn.sema.property_format import LeftRightDot
from gwltn.sema.property_format import SpaceheatName
from gwltn.sema.property_format import UTCMilliseconds


class Glitch(SemaType):
    """Sema: https://schemas.electricity.works/types/glitch/000"""

    from_g_node_alias: LeftRightDot
    node: SpaceheatName
    type: LogLevel
    summary: str
    details: str
    created_ms: UTCMilliseconds
    type_name: Literal["glitch"] = "glitch"
    version: Literal["000"] = "000"
