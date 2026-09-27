"""Reconciled-account value type only; live controller intentionally not distributed."""

from dataclasses import dataclass
from typing import Literal

from liquid_autonomous_trader.live_policy import AccountRiskSnapshot


@dataclass(frozen=True)
class ReconciledAccount:
    account_id: str
    route: Literal["paper", "live"]
    risk: AccountRiskSnapshot
    positions_complete: bool
    orders_complete: bool
    ownership_complete: bool
    protection_complete: bool


class ControllerBlocked(RuntimeError):
    pass
