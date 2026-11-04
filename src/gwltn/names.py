"""The plant names the LTN reads off its scada's channels and relays.
They are the scada's spaceheat names, fixed by the house layout grammar
the layout words encode; the LTN needs these few and no registry."""

LTN_NODE_NAME = "ltn"
"""The LTN's own spaceheat node in the house, the `FromNode` of every
contract heartbeat and the `Node` of every glitch it raises."""

STORE_COLD_PIPE = "store-cold-pipe"
HP_SCADA_OPS_RELAY = "hp-scada-ops-relay"

BUFFER_DEPTH_CHANNELS: tuple[str, str, str] = (
    "buffer-depth1",
    "buffer-depth2",
    "buffer-depth3",
)
"""The buffer's three effective (derived) temperature channels."""

MAX_STORE_TANKS = 6


def tank_reader(idx: int) -> str:
    """The node that reads store tank `idx` (1 to 6); a tank is in the
    layout when this node is."""
    if not 1 <= idx <= MAX_STORE_TANKS:
        raise ValueError(f"Tank idx must be between 1 and {MAX_STORE_TANKS}")
    return f"tank{idx}"


def tank_depth_channels(idx: int) -> tuple[str, str, str]:
    """Store tank `idx`'s three effective temperature channels, top down."""
    reader = tank_reader(idx)
    return (f"{reader}-depth1", f"{reader}-depth2", f"{reader}-depth3")
