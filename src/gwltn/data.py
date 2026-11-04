"""The latest of what the scada reports, kept for the FLO and the contract."""

from gwltn.sema.types import Report, SnapshotSpaceheat


class LtnData:
    def __init__(self) -> None:
        self.latest_snapshot: SnapshotSpaceheat | None = None
        self.latest_report: Report | None = None
        self.latest_power_w: int | None = None
