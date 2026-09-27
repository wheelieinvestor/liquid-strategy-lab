"""Allowlisted local diagnostics, never provider exception bodies or account payloads."""

import re
from decimal import Decimal

from liquid_autonomous_trader.computer_adapter import ComputerContractError
from liquid_autonomous_trader.computer_mcp import (
    ComputerMCPError,
    PostOutcomeUnknown,
    SessionRejected,
)
from liquid_autonomous_trader.controller import ControllerBlocked
from liquid_autonomous_trader.liquid_operations import LiquidOperationBlocked
from liquid_autonomous_trader.live_policy import PolicyViolation
from liquid_autonomous_trader.local_accounting import AccountingError
from liquid_autonomous_trader.xyz_bars import InvalidXYZBars


def exception_diagnostic(exc):
    result = {"error_type": type(exc).__name__}
    # Exact classes: do not disclose arbitrary messages from transport subclasses.
    if type(exc) in (
        ValueError,
        PolicyViolation,
        LiquidOperationBlocked,
        AccountingError,
        InvalidXYZBars,
        ComputerContractError,
        ComputerMCPError,
        PostOutcomeUnknown,
        SessionRejected,
    ) and re.fullmatch(r"[a-z_][a-z_0-9-]{0,99}", str(exc)):
        result["reason"] = str(exc)
    if type(exc) is ControllerBlocked:
        result["reasons"] = [
            reason for reason in exc.blockers if re.fullmatch(r"[a-z_][a-z_0-9-]{0,99}", reason)
        ]
    if type(exc) in (ComputerMCPError, PostOutcomeUnknown):
        categories = getattr(exc, "provider_categories", ())
        allowed = {
            "minimum_collateral",
            "minimum_order_size",
            "insufficient_balance",
            "leverage_limit",
            "market_unavailable",
            "price_precision",
            "quantity_precision",
            "automation_policy",
        }
        if categories:
            result["provider_reported_categories"] = [c for c in categories if c in allowed]
    return result


def admission_diagnostic(request):
    """Numeric inputs needed to explain sizing, with no account/credential fields."""
    names = (
        "entry",
        "proposed_stop",
        "quantity_step",
        "requested_notional",
        "requested_collateral",
        "stressed_cost",
        "selected_leverage",
        "liquidation_distance_price",
        "stressed_slippage_price",
        "stop_price_step",
    )
    return {
        name: str(value)
        for name in names
        if isinstance(value := getattr(request, name), Decimal) and value.is_finite()
    }


def execution_readiness(raw):
    """Fail closed on missing readiness evidence; export only fixed reason codes."""
    agents = raw.get("agents", {})
    if not isinstance(agents, dict):
        raise ValueError("invalid_agent_status")
    reasons = []
    diagnostics = []

    def describe(scope, result):
        if not isinstance(result, dict):
            return
        codes = result.get("reasons") or [result.get("reason") or result.get("error_type")]
        for code in codes if isinstance(codes, list) else []:
            if isinstance(code, str) and re.fullmatch(r"[a-zA-Z_][a-zA-Z_0-9-]{0,99}", code):
                diagnostics.append(scope + ":" + code)
                if code in {
                    "shared_position_protection_unverified",
                    "venue_stop_disagrees_with_liquid",
                    "shared_account_ownership_mismatch",
                }:
                    reasons.append("position_safety_unverified")

    counts = {}
    for category in ("owners", "entries"):
        results = agents.get(category, {})
        if not isinstance(results, dict) or any(
            not isinstance(result, dict) for result in results.values()
        ):
            raise ValueError("invalid_agent_status")
        counts[category] = sum(bool(r.get("error_type")) for r in results.values())
        for agent, result in results.items():
            if result.get("error_type"):
                scope = (
                    agent
                    if category == "entries"
                    and agent
                    in {"btc_momentum", "flow_show_mirror", "inverse_cramer", "xyz100_gex"}
                    else "position_management"
                    if category == "owners"
                    else "entry"
                )
                describe(scope, result)
        if counts[category]:
            reasons.append("management_errors" if category == "owners" else "entry_errors")
        if any(r.get("result") == "unknown_write_reconciliation_only" for r in results.values()):
            reasons.append("management_reconciliation_pending")
    for diagnostic, code in (
        ("stop_recovery", "stop_recovery_degraded"),
        ("accounting_recovery", "accounting_recovery_degraded"),
    ):
        if raw.get(diagnostic):
            # A recovery failure says nothing by itself about surviving native
            # stop protection; report degradation without inventing exposure.
            reasons.append(code)
            describe(diagnostic, raw[diagnostic])
    describe("cycle", raw)
    describe("provider_policy", raw.get("entry_policy"))
    checks = (
        (raw.get("activated") is True, "not_activated"),
        (raw.get("cycle") == "completed", "cycle_incomplete"),
        (raw.get("halted") is False, "halted_or_unknown"),
        (raw.get("unresolved_write_count") == 0, "unresolved_writes"),
        (raw.get("pending_management_count") == 0, "pending_management"),
        (raw.get("accounting_ready") is True, "accounting_not_ready"),
        (raw.get("provider_policy_ready") is True, "provider_policy_not_ready"),
        (agents.get("entries_enabled") is True, "entries_disabled"),
    )
    reasons.extend(reason for ready, reason in checks if not ready)
    return {
        "healthy": not reasons,
        "blocker_codes": sorted(set(reasons)),
        "diagnostic_codes": sorted(set(diagnostics))[:12],
        "management_error_count": counts["owners"],
        "entry_error_count": counts["entries"],
    }


def expected_xyz_wait(exc, now):
    """Only calendar-proven entry downtime is normal; stale/gapped data is not."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from liquid_autonomous_trader.cash_calendar import xnys_session

    if not (
        type(exc) is ValueError
        and str(exc) in {"gamma_cash_session_closed", "gamma_fresh_settled_bar_required"}
        or type(exc) is InvalidXYZBars
        and str(exc) == "insufficient_bars_for_one_hour_trend"
    ):
        return None
    session = xnys_session(now.astimezone(ZoneInfo("America/New_York")).date())
    if type(exc) is ValueError and str(exc) == "gamma_fresh_settled_bar_required":
        if session is not None and session.open_at <= now < session.open_at + timedelta(minutes=15):
            return {"result": "waiting_for_session_bars"}
    elif type(exc) is ValueError:
        if session is None or not session.open_at <= now < session.close_at:
            return {"result": "waiting_for_cash_session"}
    elif session is not None and session.open_at <= now < session.open_at + timedelta(minutes=75):
        return {"result": "waiting_for_session_bars"}
    return None
