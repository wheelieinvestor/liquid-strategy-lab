"""Predeclared acceptance matrix; case count is not independent history count."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from liquid_autonomous_trader.backtesting.events import canonical, digest

ARCHETYPES = {
    "market": (
        "trend_long",
        "trend_short",
        "chop",
        "false_break_up",
        "false_break_down",
        "volatility_cluster",
        "empirical_heavy_tail",
        "flash_crash_rebound",
        "opening_gap",
        "weekend_gap",
        "funding_flip",
        "mark_book_divergence",
        "depleted_depth",
        "shock_spread_latency",
        "grind_with_friction",
        "short_squeeze",
        "whipsaw_stops",
        "correlated_selloff",
        "trend_funding_drain",
        "stale_oracle",
    ),
    "execution": (
        "ack_delay",
        "partial_depth",
        "no_fill",
        "reject",
        "rate_limit",
        "unknown_entry",
        "cancel_latency",
        "cancel_after_fill",
        "stop_replace_latency",
        "lost_stop_ack",
        "crash_reserved",
        "crash_partial",
        "crash_unknown",
        "crash_stop_pending",
        "crash_software_exit",
        "duplicate_entry",
        "duplicate_management",
        "disconnected_book",
        "stale_book",
        "restore_then_later_fill",
        "limit_touch",
        "limit_cross",
        "stop_gap",
        "partial_stop_rebound",
    ),
    "source": (
        "missing_book",
        "future_bar",
        "duplicate_flow",
        "stale_flow",
        "flow_before_dispatch_reject",
        "revised_flow",
        "missing_gex",
        "wrong_gex_expiry",
        "conflicting_gex_revision",
        "late_gex",
        "current_quote_skew",
        "dst_spring",
        "dst_fall",
        "early_close",
        "holiday",
        "old_cramer",
        "reposted_cramer",
        "ambiguous_mapping",
        "instructions_in_source",
        "empty_inputs",
    ),
    "portfolio": (
        "simultaneous_agents",
        "flow_btc_collision",
        "cramer_btc_collision",
        "foreign_exposure",
        "unknown_reservation",
        "scarce_collateral",
        "full_sleeve",
        "correlated_selloff",
        "margin_tier_boundary",
        "isolated_funding_drain",
        "cross_margin_pressure",
        "partial_fill_reservation",
        "halt_pending_entries",
        "native_stop_during_halt",
        "day_rollover",
        "epoch_before_dispatch_reject",
        "epoch_closed_position",
        "cramer_deadline",
        "cramer_opposing_signal",
        "capital_released_later_entry",
    ),
    "jev": (
        "keep",
        "tighten",
        "bounded_loosen",
        "excess_risk_loosen",
        "full_exit",
        "abstain",
        "long_short_symmetry",
        "intact_consolidation",
        "thesis_failure",
        "crossed_stop",
        "prohibited_partial",
        "repeated_review",
        "model_outage",
        "budget_exhausted",
        "malformed_response",
        "stale_response",
    ),
}
TARGETS = {"market": 100, "execution": 120, "source": 100, "portfolio": 100, "jev": 80}
INVARIANTS = (
    "cash_plus_unrealized_equals_equity",
    "price_minus_fees_plus_funding_reconciles",
    "available_time_precedes_decision",
    "no_same_observation_fill",
    "one_owner_per_market",
    "unknown_write_blocks_new_admission",
    "native_stop_survives_application_outage",
    "original_risk_envelope_bounds_stop",
    "restart_reproduces_core",
    "no_live_capability",
)


@dataclass(frozen=True)
class Scenario:
    case_id: str
    family: str
    archetype: str
    variant: int
    seed: int
    direction: str
    shock_scale: str
    spread_bps: str
    latency_us: int
    depth_fraction: str
    evidence_mode: str = "synthetic_stress"

    def __post_init__(self):
        if self.family not in ARCHETYPES or self.archetype not in ARCHETYPES[self.family]:
            raise ValueError("undeclared_scenario_family_or_archetype")
        if not 0 <= self.variant < 5 or self.seed < 0 or self.latency_us < 1:
            raise ValueError("invalid_scenario_variant_or_seed")
        if self.direction not in {"long", "short"} or self.evidence_mode != "synthetic_stress":
            raise ValueError("scenario_is_conditional_not_historical")


def declaration():
    cases = []
    for family, archetypes in ARCHETYPES.items():
        for archetype in archetypes:
            for variant in range(5):
                key = f"{family}/{archetype}/{variant + 1:02d}"
                cases.append(
                    Scenario(
                        key,
                        family,
                        archetype,
                        variant,
                        int(digest(key)[:8], 16),
                        "long" if variant % 2 == 0 else "short",
                        ("0.5", "1", "1.5", "2", "3")[variant],
                        ("1", "3", "8", "12", "25")[variant],
                        (1, 250000, 1000000, 2000000, 5000000)[variant],
                        ("1", "0.5", "0.2", "0.1", "0.01")[variant],
                    )
                )
    result = {
        "schema": "liquid-scenario-declaration-v1",
        "cases": [asdict(c) for c in cases],
        "targets": TARGETS,
        "invariants": list(INVARIANTS),
        "independent_historical_datasets": 0,
        "interpretation": "500 conditional behavior cases; not 500 independent histories "
        "or parameter-selection trials",
        "market_sampling": "joint contiguous development-data blocks plus labeled "
        "adversarial shocks; no final reserve access",
        "resources": {
            "workers": 1,
            "wall_seconds": 1800,
            "rss_bytes": 2 * 1024**3,
            "output_bytes": 4 * 1024**3,
            "inference_spend_usd": "0",
        },
    }
    if len({c.case_id for c in cases}) != 500:
        raise ValueError("scenario_count_or_identity_error")
    return {**result, "declaration_sha256": digest(result)}


def freeze(path: Path):
    encoded = canonical(declaration()) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise ValueError("frozen_scenario_declaration_changed")
        return False
    path.write_text(encoded, encoding="utf-8")
    return True
