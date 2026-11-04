from typing import Literal
from gwltn.sema.base import SemaType
from gwltn.sema.property_format import GitCommit
from gwltn.sema.property_format import LeftRightDot
from gwltn.sema.property_format import UTCMilliseconds


class ScadaCommit(SemaType):
    """Sema: https://schemas.electricity.works/types/gw.scada.commit/000"""

    scada_alias: LeftRightDot
    git_commit: GitCommit
    message_created_ms: UTCMilliseconds
    type_name: Literal["gw.scada.commit"] = "gw.scada.commit"
    version: Literal["000"] = "000"
