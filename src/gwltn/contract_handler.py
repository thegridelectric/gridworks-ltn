import asyncio
import json
import random
import time
import uuid
from datetime import datetime, timedelta
from logging import Logger
from typing import Callable, Optional

import pytz

from gwltn.config import GwltnSettings
from gwltn.sema.base import SemaType
from gwltn.sema.enums import MarketPriceUnit, SlowDispatchContractStatus
from gwltn.sema.types import (
    Bid,
    LatestPrice,
    SlowContractHeartbeat,
    SlowContractRejection,
    SlowDispatchContract,
)

# SignedProof placeholder on every heartbeat the ltn sends; retired by
# heartbeat signing (no signing word or key material yet).
SIGNED_PROOF_STUB = "signed_proof_stub"


def contract_end_s(contract: SlowDispatchContract) -> int:
    """Unix second at which the contract's slot ends."""
    return contract.start_s + contract.duration_minutes * 60


class LtnContractHandler:
    """Handles Ltn's side of representation contract with SCADA and dispatch contracts"""

    HEARTBEAT_INTERVAL_S = 60
    DONE_STATES = [
        SlowDispatchContractStatus.TerminatedByLtn,
        SlowDispatchContractStatus.TerminatedByScada,
        SlowDispatchContractStatus.CompletedUnknownOutcome,
        SlowDispatchContractStatus.CompletedSuccess,
        SlowDispatchContractStatus.CompletedFailureByLtn,
        SlowDispatchContractStatus.CompletedFailureByScada,
    ]
    ACTIVE_STATES = [
        SlowDispatchContractStatus.Created,
        SlowDispatchContractStatus.Received,
        SlowDispatchContractStatus.Confirmed,
        SlowDispatchContractStatus.Active,
    ]
    HEARTBEAT_INTERVAL_SECONDS = 60  # Send heartbeats once per minute
    LOGGER_NAME = "LtnContractHandler"

    def __init__(
        self,
        node_name: str,
        scada_alias: str,
        settings: GwltnSettings,
        logger: Logger,
        send_to_scada: Callable[[SemaType], None],
    ):
        """`node_name` is the LTN's spaceheat node, the `FromNode` of every
        heartbeat; `scada_alias` the scada every contract is with.
        `send_to_scada` is the LTN's one send to its scada; it is safe
        from any thread."""
        self.settings = settings
        self.node_name = node_name
        self.scada_alias = scada_alias
        self.logger = logger
        self.send_to_scada = send_to_scada
        self._stop_requested = False
        self.timezone = pytz.timezone(self.settings.timezone_str)
        self.contract_file = self.settings.data_path / "slow_dispatch_contract.json"
        self.next_contract_energy_wh: Optional[int] = None
        self.energy_used_wh: float = 0
        self.latest_power_w: int = 0
        self.energy_updated_s: Optional[float] = None
        self.layout_received: bool = False  # have we received the layout
        self.latest_hb: Optional[SlowContractHeartbeat] = (
            None  # Current active hb, None means no active contract
        )
        self.latest_price: Optional[LatestPrice] = None
        self.latest_bid: Optional[Bid] = None

    def load_heartbeat(self) -> Optional[SlowContractHeartbeat]:
        """Loads existing SlowContractHeartbeat from persistent storage

        - Returns None if no heartbeat exists
        - Otherwise loads the heartbeat and sets internal state
        """
        if not self.contract_file.exists():
            self.logger.info("No contract file found")
            return None

        with open(self.contract_file, "r") as f:
            contract_data = json.load(f)

        try:
            hb = SlowContractHeartbeat.from_dict(contract_data)

            # Store as latest heartbeat
            if hb.status in self.DONE_STATES:
                # This is a completed contract, not active
                self.logger.info("Loaded completed contract")
                return None

            # If contract has ended, mark as complete and send that message. Set latest_hb to None
            if time.time() > contract_end_s(hb.contract):
                if hb.status not in self.DONE_STATES:
                    # Contract has expired, need to mark as completed
                    self.logger.info(
                        "Loaded contract has expired, will send completion heartbeat"
                    )
                    completion_hb = self.create_completion_heartbeat()
                    self.latest_hb = None
                    return completion_hb
                else:
                    return None
            else:
                self.logger.info(
                    f"Loaded active contract: {self.formatted_contract(hb)}"
                )
                self.energy_updated_s = hb.message_created_ms / 1000
                if hb.from_node == self.node_name:
                    if hb.status != SlowDispatchContractStatus.Created:
                        raise Exception("Only save ltn heartbeats with status Created!")
                    self.energy_used_wh = 0
                    self.latest_hb = hb
                    return hb
                else:
                    if hb.watt_hours_used is None:
                        raise Exception("hb from Scada must have WattHoursUsed!")
                    self.energy_used_wh = hb.watt_hours_used
                self.latest_hb = hb
                return None
        except Exception as e:
            self.logger.error(
                "Ignoring persisted contract heartbeat due to load error",
                exc_info=e,
            )
            return None

    def store_heartbeat(self, hb: Optional[SlowContractHeartbeat] = None) -> None:
        """Stores a SlowContractHeartbeat to persistent storage"""
        hb_to_store = self.latest_hb
        if hb:
            hb_to_store = hb
        if not hb_to_store:
            raise Exception("Can't store hb if latest_hb is none!")

        if hb_to_store.from_node == self.node_name:
            if hb_to_store.status not in [
                SlowDispatchContractStatus.Created,
                SlowDispatchContractStatus.TerminatedByLtn,
                SlowDispatchContractStatus.CompletedUnknownOutcome,
            ]:
                raise Exception(
                    f"Does not store ltn hb with status {hb_to_store.status}"
                )

        with open(self.contract_file, "w") as f:
            json.dump(hb_to_store.to_dict(), f, indent=4)

    def initialize(self) -> Optional[SlowContractHeartbeat]:
        """Initialize contract handler state from persistent storage

        Returns a heartbeat if one needs to be sent to SCADA
        """
        hb = self.load_heartbeat()
        return hb

    def process_slow_contract_heartbeat(self, scada_hb: SlowContractHeartbeat) -> None:
        """- Does nothing if scada_hb does not match (latest_hb is None, mis-match in ContractId,
        or MessageCreatedMs is out of order)
        Otherwise:
          - sets self.latest_hb to this hb
          - updates energy_used_wh and energy_updated_s
          - stores the heartbeat
          - sets latest_hb to None if scada sends Completed or Terminated

        """
        if self.latest_hb is None:
            self.logger.info(
                f"Unexpected Scada hb ... we have no heartbeat! {scada_hb}"
            )
            return
        if scada_hb.contract.contract_id != self.latest_hb.contract.contract_id:
            self.logger.info("Not the same ContractId ... ignoring")
            return
        if self.latest_hb is not None:
            if scada_hb.message_created_ms < self.latest_hb.message_created_ms:
                self.logger.info("hb received out of order. Ignoring!")
                return
        if scada_hb.watt_hours_used is None:
            raise Exception("this can't happen, axiomatically in Hb")

        self.logger.info(self.formatted_contract(scada_hb))
        self.energy_used_wh = scada_hb.watt_hours_used
        self.energy_updated_s = time.time()
        self.store_heartbeat(scada_hb)

        if scada_hb.status == SlowDispatchContractStatus.CompletedUnknownOutcome:
            self.latest_hb = None  # we can create new contract now!
            if self.can_create_contract():
                self.create_new_contract()
        elif scada_hb.status == SlowDispatchContractStatus.TerminatedByScada:
            self.latest_hb = None
        else:
            self.latest_hb = scada_hb

    def process_slow_contract_rejection(self, rejection: SlowContractRejection) -> None:
        """The scada will not take the offered contract. Drop the pending
        Created heartbeat so the rejected offer is not resent or reloaded."""
        if (
            self.latest_hb is None
            or rejection.contract_id != self.latest_hb.contract.contract_id
        ):
            self.logger.info(
                f"Rejection for a contract we are not offering ... ignoring: {rejection}"
            )
            return
        self.logger.warning(
            f"Scada refused contract {rejection.contract_id}: {rejection.reason.value}"
        )
        self.latest_hb = None
        self.contract_file.unlink(missing_ok=True)

    def create_new_contract(self) -> None:
        """
        - Create a new SlowDispatchContract at the top of the hour
        - Create a new Heartbeat, stores in latest_hb
        - Sends new Heartbeat to Scada
        """
        if self.next_contract_energy_wh is None:
            raise Exception("next_contract_energy_wh should not be None!")
        watthours = self.next_contract_energy_wh
        self.next_contract_energy_wh = None

        now = time.time()
        after_top_s = now % 3600
        if after_top_s >= 10:
            self.logger.info(
                f"Not starting new contract! more than 10 seconds after top of hour: {round(after_top_s)}"
            )
            return

        if self.latest_price is None:
            self.logger.info("Don't have a latest price. Not creating new contract")
            return

        if self.latest_hb is not None:
            self.logger.info("WHY IS latest_hb not none??")
            self.latest_hb = None

        # Only create if there's no active contract or the active contract is done
        if self.latest_hb:
            raise Exception("Cannot create new contract while one is active")

        # Create contract starting at the next 5-minute boundary
        now = time.time()
        start_s = int(now - (now % 3600))

        oil_boiler_on = False
        if watthours > 0 and self.use_oil_as_fuel_substitute():
            oil_boiler_on = True
            watthours = 0

        duration_minutes = 60
        avg_power_watts = int(watthours * (60 / duration_minutes))
        contract = SlowDispatchContract(
            scada_alias=self.scada_alias,
            start_s=start_s,
            duration_minutes=duration_minutes,
            avg_power_watts=avg_power_watts,
            oil_boiler_on=oil_boiler_on,
            contract_id=str(uuid.uuid4()),
        )

        # Create initial heartbeat
        hb = SlowContractHeartbeat(
            from_node=self.node_name,
            contract=contract,
            status=SlowDispatchContractStatus.Created,
            message_created_ms=int(time.time() * 1000),
            my_digit=random.choice(range(10)),
            your_last_digit=None,  # First message in chain
            is_authoritative=True,
            signed_proof=SIGNED_PROOF_STUB,
        )

        # Update and store the heartbeat
        self.latest_hb = hb
        self.store_heartbeat()

        self.logger.info(
            f"Created new contract! {self.formatted_contract(self.latest_hb)}"
        )
        self.send_to_scada(self.latest_hb)

    def is_onpeak(self) -> bool:
        time_now = datetime.now(self.timezone)
        time_in_2min = time_now + timedelta(minutes=2)
        peak_hours = [7, 8, 9, 10, 11] + [16, 17, 18, 19]
        if (
            time_now.hour in peak_hours or time_in_2min.hour in peak_hours
        ) and time_now.weekday() < 5:
            return True
        else:
            return False

    def use_oil_as_fuel_substitute(self) -> bool:
        if not self.is_onpeak():
            return False
        if self.latest_price is None:
            return False
        if self.latest_price.price_unit != MarketPriceUnit.USDPerMWh:
            raise Exception(
                f"Stop being so parochial and assuming {MarketPriceUnit.USDPerMWh}"
            )
        price_usd_per_mwh = self.latest_price.price_times1000 / 1000
        self.logger.info(
            f"Latest price is ${price_usd_per_mwh}/MWh. Fuel sub threshold ${self.settings.fuel_sub_usd_per_mwh}/MWh"
        )
        if (
            price_usd_per_mwh > self.settings.fuel_sub_usd_per_mwh
            and self.settings.fuel_substitution
        ):
            return True
        elif not self.settings.fuel_substitution:
            self.logger.info("fuel_substation variable set to False! ")
            return False
        return False

    async def contract_heartbeat_task(self):
        """Task that sends regular heartbeats while a contract is active"""
        await asyncio.sleep(2)
        hb = self.initialize()
        if hb is not None:
            if hb.status == SlowDispatchContractStatus.Created:
                # we didn't get any response, send again
                self.send_to_scada(hb)
        while not self._stop_requested:
            try:
                # Only send heartbeats if we have an active contract
                if self.latest_hb and self.latest_hb.status in [
                    SlowDispatchContractStatus.Created,
                    SlowDispatchContractStatus.Received,
                    SlowDispatchContractStatus.Active,
                ]:
                    # Check if contract has expired
                    if time.time() > contract_end_s(self.latest_hb.contract):
                        # Contract expired - initiate completion
                        self.send_to_scada(self.create_completion_heartbeat())
                    else:
                        if self.latest_hb.status == SlowDispatchContractStatus.Created:
                            send_hb = self.latest_hb  # RESEND first contract
                        else:
                            send_hb = (
                                self.create_midcontract_heartbeat()
                            )  # Create a mid-contract heartbeat

                        self.send_to_scada(send_hb)
            except Exception as e:
                self.logger.error(f"Error in heartbeat cycle: {e}")
            # Wait for next heartbeat interval
            await asyncio.sleep(self.HEARTBEAT_INTERVAL_S)

    def start_completing_old_contract(self) -> None:
        """ """
        if self.latest_hb is None:
            raise Exception("Only call when latest_hb exists")

        if contract_end_s(self.latest_hb.contract) > time.time():
            raise Exception(
                "Past the top of the hour but still in prev contract timeslot!?"
            )

        try:
            self.send_to_scada(self.create_completion_heartbeat())
        except Exception as e:
            self.logger.info(
                f"Tried to send contract completion in process_latest_price. Perhaps race cond? {e}"
            )

    def create_termination_heartbeat(self, reason: str) -> SlowContractHeartbeat:
        """Create a heartbeat terminating the current contract"""
        if not self.latest_hb:
            raise Exception("No active contract to terminate")

        hb = SlowContractHeartbeat(
            from_node=self.node_name,
            contract=self.latest_hb.contract,
            previous_status=self.latest_hb.status,
            status=SlowDispatchContractStatus.TerminatedByLtn,
            cause=reason,
            message_created_ms=int(time.time() * 1000),
            my_digit=random.choice(range(10)),
            your_last_digit=self.latest_hb.my_digit,
            is_authoritative=True,
            signed_proof=SIGNED_PROOF_STUB,
        )

        # Store heartbeat
        self.store_heartbeat(hb)
        # Clear active contract
        self.latest_hb = None
        return hb

    def create_completion_heartbeat(self) -> SlowContractHeartbeat:
        """Create a heartbeat marking the current contract as completed"""
        if not self.latest_hb:
            raise Exception("No active contract to terminate")
        if not time.time() > contract_end_s(self.latest_hb.contract):
            raise Exception("Should not call this if contract time is not over!")
        hb = SlowContractHeartbeat(
            from_node=self.node_name,
            contract=self.latest_hb.contract,
            previous_status=self.latest_hb.status,
            status=SlowDispatchContractStatus.CompletedUnknownOutcome,
            cause="Ltn notes MarketSlot complete",
            message_created_ms=int(time.time() * 1000),
            my_digit=random.choice(range(10)),
            your_last_digit=self.latest_hb.my_digit,
            is_authoritative=True,
            signed_proof=SIGNED_PROOF_STUB,
        )
        return hb

    def create_midcontract_heartbeat(self) -> SlowContractHeartbeat:
        """Generate the next heartbeat in the contract sequence

        Will raise an exception if latest_hb is None, if the time is past
        the latest_hb contract end, or if the status is Terminated
        """
        if self.latest_hb is None:
            raise Exception("Should not call this if latest_hb is None!")
        if time.time() > contract_end_s(self.latest_hb.contract):
            raise Exception("Don't call this for an expired contract!")
        if self.latest_hb.status == SlowDispatchContractStatus.Created:
            return self.latest_hb  # haven't gotten the Received ack yet
        elif self.latest_hb.status in [
            SlowDispatchContractStatus.Received,
            SlowDispatchContractStatus.Active,
        ]:
            return SlowContractHeartbeat(
                from_node=self.node_name,
                contract=self.latest_hb.contract,
                status=SlowDispatchContractStatus.Active,
                message_created_ms=int(time.time() * 1000),
                my_digit=random.choice(range(10)),
                your_last_digit=self.latest_hb.my_digit,
                is_authoritative=True,
                signed_proof=SIGNED_PROOF_STUB,
            )
        else:
            raise Exception(
                f"Cannot call this if latest_hb.status is {self.latest_hb.status}"
            )

    def can_create_contract(self) -> bool:
        """layout received, no latest_hb, and have calculated next energy"""
        return (
            self.layout_received
            and self.latest_hb is None
            and self.next_contract_energy_wh is not None
        )

    def formatted_contract(
        self, hb_to_format: Optional[SlowContractHeartbeat] = None
    ) -> str:
        """Format contract heartbeat information for logging in a human-readable format."""
        hb = self.latest_hb
        if hb_to_format:
            hb = hb_to_format

        if not hb:
            return ""

        slot_start = datetime.fromtimestamp(
            hb.contract.start_s, tz=self.timezone
        ).strftime("%H:%M")

        slot_end = datetime.fromtimestamp(
            contract_end_s(hb.contract), tz=self.timezone
        ).strftime("%H:%M")

        created_time = datetime.fromtimestamp(
            hb.message_created_ms / 1000, tz=self.timezone
        ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        # Calculate total energy for the contract in Wh
        total_energy = hb.contract.avg_power_watts * hb.contract.duration_minutes / 60

        # Energy used so far (only present in SCADA heartbeats)
        if hb.status == SlowDispatchContractStatus.Created:
            energy_used = 0
        else:
            energy_used = hb.watt_hours_used

        # Format the log string
        log_str = (
            f"Contract[{hb.contract.contract_id[:3]}] "
            f"{slot_start} - {slot_end}: "
            f"Total: {total_energy:.1f} Wh | "
            f"Used: {energy_used} Wh | "
            f"Status: {hb.status.value}  (created by {hb.from_node} at {created_time})"
        )

        # Add reason if present
        if hb.cause:
            log_str += f" | Reason: {hb.cause}"

        return log_str
