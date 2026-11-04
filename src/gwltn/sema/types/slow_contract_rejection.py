from typing import Literal
from gwltn.sema.base import SemaType
from gwltn.sema.enums import DispatchRefusalReason
from gwltn.sema.property_format import LeftRightDot
from gwltn.sema.property_format import UTCMilliseconds
from gwltn.sema.property_format import UUID4Str


class SlowContractRejection(SemaType):
    """Sema: https://schemas.electricity.works/types/slow.contract.rejection/000"""

    from_g_node_alias: LeftRightDot
    contract_id: UUID4Str
    reason: DispatchRefusalReason
    message_created_ms: UTCMilliseconds
    type_name: Literal["slow.contract.rejection"] = "slow.contract.rejection"
    version: Literal["000"] = "000"
