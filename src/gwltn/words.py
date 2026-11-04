"""The two words on the scada's wire that have no sema word yet, in the
snapshot's own style so they decode and encode like every other word.
Each is retired by the coined word of the same type name, at which point
it joins the seed request and this module goes. The generated snapshot
narrows `type_name` and `version` the same way; pyright skips that
folder, so the narrowing is excused here by hand."""

# pyright: reportIncompatibleVariableOverride=false

from typing import Literal

from gwltn.sema.base import SemaType
from gwltn.sema.property_format import LeftRightDot, UTCSeconds, UUID4Str


class NoNewContractWarning(SemaType):
    """The scada's notice that the contract named is ending and no new one
    has arrived, with the end of the grace period after which it dispatches
    on its own. Missing word: `no.new.contract.warning`."""

    from_g_node_alias: LeftRightDot
    contract_id: UUID4Str
    grace_end_time_s: UTCSeconds
    type_name: Literal["no.new.contract.warning"] = "no.new.contract.warning"
    version: Literal["000"] = "000"


class SendSnap(SemaType):
    """A request that the scada send its current `snapshot.spaceheat`.
    Missing word: `send.snap`."""

    from_g_node_alias: LeftRightDot
    type_name: Literal["send.snap"] = "send.snap"
    version: Literal["000"] = "000"
