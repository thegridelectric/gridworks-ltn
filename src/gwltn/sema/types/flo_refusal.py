from typing import Literal
from gwltn.sema.base import SemaType
from gwltn.sema.enums import FloRefusalReason
from gwltn.sema.property_format import LeftRightDot
from gwltn.sema.property_format import NonEmptyString
from gwltn.sema.property_format import UTCMilliseconds
from gwltn.sema.property_format import UTCSeconds


class FloRefusal(SemaType):
    """Sema: https://schemas.electricity.works/types/flo.refusal/000"""

    bidder_alias: LeftRightDot
    start_unix_s: UTCSeconds
    reason: FloRefusalReason
    detail: NonEmptyString | None = None
    created_ms: UTCMilliseconds
    type_name: Literal["flo.refusal"] = "flo.refusal"
    version: Literal["000"] = "000"
