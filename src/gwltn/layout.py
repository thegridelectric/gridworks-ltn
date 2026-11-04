"""The house's two authored artifacts, loaded for the LTN: the layout
(a sema layout word) and its operational params, as one approved pair.
The LTN reads the few plant facts it plans on straight off the word: who
the LTN and the scada are, the zones and their thermal mass, the water
store, which store tanks exist, and each temperature channel's encoding.
The scada's own plant checks are the scada's."""

import json
from pathlib import Path
from typing import NamedTuple

from pydantic import TypeAdapter

from gwltn.names import (
    BUFFER_DEPTH_CHANNELS,
    MAX_STORE_TANKS,
    tank_depth_channels,
    tank_reader,
)
from gwltn.sema.enums import GNodeClass
from gwltn.sema.property_format import LeftRightDot
from gwltn.sema.types import (
    House0FamilyParams,
    House0Layout,
    House0NoSieg,
    House0NoSiegFamilyParams,
    NolanFamilyParams,
    NolanLayout,
    OperationalParams,
    WaterStore,
)
from gwltn.temperature import TemperatureEncoding

SemaLayout = House0Layout | House0NoSieg | NolanLayout
SEMA_LAYOUT_BY_TYPENAME: dict[LeftRightDot, type[SemaLayout]] = {
    word.type_name_value(): word for word in (House0Layout, House0NoSieg, NolanLayout)
}

# The ONLY layout ⊕ family-params pairings a house may run. The operational
# params carry a FamilyParams block whose TypeName names the layout family; a
# crossed pair would tune one family's plant with the other family's knobs.
APPROVED_PAIRS: dict[LeftRightDot, LeftRightDot] = TypeAdapter(
    dict[LeftRightDot, LeftRightDot]
).validate_python({
    House0Layout.type_name_value(): House0FamilyParams.type_name_value(),
    House0NoSieg.type_name_value(): House0NoSiegFamilyParams.type_name_value(),
    NolanLayout.type_name_value(): NolanFamilyParams.type_name_value(),
})


class LtnLayout(NamedTuple):
    """What the LTN plans on, read once off the layout word at boot.
    Missing word: none expected; this is the LTN's view of `word`, and
    `word` is the record."""

    word: SemaLayout
    ltn_alias: LeftRightDot
    scada_alias: LeftRightDot
    zone_list: list[str]
    critical_zone_list: list[str]
    zone_kwh_per_deg_f_list: list[float]
    water_store: WaterStore | None
    store_tank_indices: list[int]
    encodings: dict[str, TemperatureEncoding]

    @property
    def layout_type_name(self) -> str:
        return self.word.type_name

    @property
    def tank_temp_channel_names(self) -> list[str]:
        """The buffer's three layers, then each store tank's, top down."""
        names = list(BUFFER_DEPTH_CHANNELS)
        for idx in self.store_tank_indices:
            names.extend(tank_depth_channels(idx))
        return names

    def encoding(self, channel_name: str) -> TemperatureEncoding | None:
        """The encoding a channel's raw value is in: a data channel's
        telemetry name or a derived channel's output unit; None for a
        channel the layout does not have."""
        return self.encodings.get(channel_name)


def g_node_alias(word: SemaLayout, g_node_class: GNodeClass) -> LeftRightDot:
    aliases = [g.alias for g in word.g_nodes if g.g_node_class == g_node_class]
    if len(aliases) != 1:
        raise ValueError(
            f"Layout has {len(aliases)} {g_node_class.value} GNodes; wants exactly one"
        )
    return aliases[0]


def ltn_layout(word: SemaLayout) -> LtnLayout:
    node_names = {node.name for node in word.sh_nodes}
    encodings: dict[str, TemperatureEncoding] = {
        ch.name: ch.telemetry_name for ch in word.data_channels
    }
    encodings.update({ch.name: ch.output_unit for ch in word.derived_channels})
    zones = word.hydronic.zones
    return LtnLayout(
        word=word,
        ltn_alias=g_node_alias(word, GNodeClass.LeafTransactiveNode),
        scada_alias=g_node_alias(word, GNodeClass.Scada),
        zone_list=[z.name for z in zones],
        critical_zone_list=[z.name for z in zones if z.critical],
        zone_kwh_per_deg_f_list=[z.kwh_per_deg_f for z in zones],
        water_store=word.hydronic.water_store,
        store_tank_indices=[
            idx
            for idx in range(1, MAX_STORE_TANKS + 1)
            if tank_reader(idx) in node_names
        ],
        encodings=encodings,
    )


def check_approved_pair(layout_type_name: str, family_type_name: str) -> None:
    """Refuse any layout ⊕ family-params pairing outside APPROVED_PAIRS."""
    expected = APPROVED_PAIRS.get(layout_type_name)
    if expected is None:
        raise ValueError(
            f"Layout word {layout_type_name!r} has no approved family-params "
            f"partner. Approved pairs: {APPROVED_PAIRS}."
        )
    if family_type_name != expected:
        raise ValueError(
            f"Mismatched artifact pair: layout {layout_type_name!r} SHALL be paired "
            f"with {expected!r}, got family params {family_type_name!r}."
        )


def check_scada_alias(layout: LtnLayout, ops_word: OperationalParams) -> None:
    """The ops word names the Scada it tunes; refuse a pair whose ScadaAlias is
    not the alias of the layout's Scada GNode."""
    if layout.scada_alias != ops_word.scada_alias:
        raise ValueError(
            f"Mismatched artifact pair: operational params ScadaAlias "
            f"{ops_word.scada_alias!r} is not the layout's Scada GNode alias "
            f"{layout.scada_alias!r}."
        )


def decode_operational_params(ops: dict) -> OperationalParams:
    type_name = ops.get("TypeName")
    if type_name != OperationalParams.type_name_value():
        raise ValueError(
            f"Operational params TypeName {type_name!r} is not "
            f"{OperationalParams.type_name_value()!r}."
        )
    return OperationalParams.from_dict(ops)


def load_operational_params(path: Path) -> OperationalParams:
    """The house's authored operational-params artifact. REQUIRED: its
    values have no defaults anywhere else."""
    if not path.exists():
        raise FileNotFoundError(f"Operational params artifact not found at {path}.")
    return decode_operational_params(json.loads(path.read_text()))


def load_layout(layout_path: Path, ops_path: Path) -> LtnLayout:
    """The LTN's layout from the two authored artifacts, an approved pair."""
    static = json.loads(layout_path.read_text())
    ops = json.loads(ops_path.read_text())
    type_name = str(static.get("TypeName"))
    if type_name not in SEMA_LAYOUT_BY_TYPENAME:
        raise ValueError(
            f"{layout_path} has TypeName {type_name!r}; a layout must be "
            f"sema-authored, naming one of {sorted(SEMA_LAYOUT_BY_TYPENAME)}."
        )
    check_approved_pair(type_name, str((ops.get("FamilyParams") or {}).get("TypeName")))
    layout = ltn_layout(SEMA_LAYOUT_BY_TYPENAME[type_name].from_dict(static))
    check_scada_alias(layout, decode_operational_params(ops))
    return layout
