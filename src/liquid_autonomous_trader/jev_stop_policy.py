"""Stop-focused v5: retain swings, approve protection, exit only failed theses."""

import hashlib
import json
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from zoneinfo import ZoneInfo

from liquid_autonomous_trader import jev_exit_policy as previous
from liquid_autonomous_trader.cash_calendar import xnys_session

MODEL = previous.MODEL
VERSION = "liquid-stops-v5"
STOP_FOCUSED = True
THRESHOLDS = previous.THRESHOLDS
REDUCTION_POLICY = {
    "partials": "disabled",
    "quantity": "hold_or_clear_thesis_invalidation_full_exit",
    "fallback": "preserve_verified_stop_and_quantity",
    "loosening": "affirmative_evidence_within_original_risk",
    "price_selection": "direct_approval_then_widest_tightening_or_smallest_loosening",
}
INSTRUCTIONS = (
    previous.INSTRUCTIONS
    + " Primary mandate: let winners run at their remaining size and manage downside with stops. "
    "Do not take routine partial profits or chip away at exposure. Review HOLD versus clear "
    "thesis invalidation independently from protection. A weak 15m bar is not a failed swing. "
    "Flow positions are multi-session swing trades: overnight, weekends, low volume, silence "
    "from the original flow source and ordinary pullbacks do not invalidate them. Use hourly "
    "structure and volatility when available; missing hourly evidence is unknown, never proof "
    "of failure. Perpetual markets can trade while the underlying cash session is closed. "
    "Do not assume their overnight activity confirms a cash-market reversal. "
)
ACTION = {
    "HOLD": "Keep all remaining size while the defining thesis survives, including normal "
    "consolidation, pullbacks and overnight swing holding. Protect progress with the stop.",
    "CLOSE_ALL": "Exceptional exit: affirmative sustained opposing evidence invalidates the "
    "defining trade thesis. Mild weakening, uncertain continuation, profit taking, age, a "
    "legacy target or quiet overnight conditions alone are insufficient.",
    "ABSTAIN": "Insufficient evidence to judge the thesis. Use this rather than claiming HOLD is "
    "supported when critical observations are explicitly missing. Keep quantity and protection.",
}
STOP = {
    "KEEP": "Existing stop already protects meaningful progress with suitable pullback room, "
    "or no offered replacement improves that balance. A fresh entry or ordinary swing noise "
    "does not require adjustment. Do not demand a new breakout if established progress is "
    "still unprotected by a distant original stop.",
    "TIGHTEN": "The trade has established favorable progress and an offered stop can protect "
    "some of it or reduce remaining downside while leaving normal pullback room. Consider "
    "current R, observed peak, giveback, confirmed structure and volatility together. Sustained "
    "progress with volatility-supported pullback room can justify improvement even without a "
    "new named structural level. Original "
    "stops should not remain distant solely because the entry thesis still holds. A tiny gain "
    "alone does not justify choking the trade. Flow swings need hourly-scale room.",
    "LOOSEN": "Affirmative structure or volatility evidence shows the existing stop is too "
    "tight for an intact thesis. A wider offered price preserves the original admitted risk "
    "limit. Never loosen merely to avoid realizing a loss or revive a triggered stop.",
    "ABSTAIN": "Missing or contradictory evidence prevents a supported change. "
    "Preserve protection.",
}
PRICE_INSTRUCTIONS = (
    "Decide whether to REPLACE THE CURRENT STOP NOW with proposed {name} = {price}. "
    "This is a complete decision, not conditional on another question. The proposed direction "
    "is stated below. Compare current_stop_protected_r to this candidate's protected_price_r "
    "and improvement_from_current_r. For a winning trade, protect sustained favorable progress "
    "rather than leaving its full original downside exposed. A stop with ordinary volatility "
    "room can be appropriate without a newly named support/resistance level. If locking a "
    "profit would be too tight, reducing the remaining downside can be a useful first step. "
    "For a Flow swing, use hourly pullback room; overnight quiet alone is never a reason to "
    "tighten. For loosening, require affirmative structural/volatility evidence with an intact "
    "thesis, never simply a loss or fear of being stopped. Approve only if THIS replacement "
    "improves the current stop's balance of protection and continuation room. Similar other "
    "prices do not compete with this one. Reject if keeping the current stop is preferable or "
    "evidence is insufficient. Your approval can authorize this exact replacement."
)
PROTECTED_PRICE_INSTRUCTIONS = (
    " The current stop ALREADY protects breakeven or profit. If it sits beyond an established "
    "consolidation/pullback zone, KEEP it unless fresh favorable structure or renewed directional "
    "expansion warrants another adjustment. Merely finding a closer ATR price is not an "
    "improvement. Static ranges and quiet overnight holding do not justify taking away room."
)
INITIAL_PRICE_INSTRUCTIONS = (
    " The current stop STILL ALLOWS A PRICE LOSS. Judge whether the winning trade's progress "
    "and this proposal's pullback room warrant its FIRST protection improvement. You need not "
    "wait for a new breakout if sustained favorable progress already supports less downside."
)

PRICE_CRITERIA = {
    "approve": "Replace the current stop with this price now: the protection improvement and "
    "pullback room justify the change for this trade horizon.",
    "reject": "Keep the current stop instead: this replacement is premature, too tight, "
    "unnecessarily wide, or insufficiently supported.",
}


def stop_options(state, direction):
    return previous.stop_options(state, direction)


def stop_directions(state):
    return {k: v for k, v in STOP.items() if k in {"KEEP", "ABSTAIN"} or stop_options(state, k)}


def enrich(state):
    swing = state["strategy"] == "flow_show_mirror"
    state["trade_horizon"] = "multi_session_swing" if swing else "original_strategy_horizon"
    state["quantity_policy"] = REDUCTION_POLICY
    if swing:
        now = datetime.fromisoformat(state["as_of"])
        try:
            session = xnys_session(now.astimezone(ZoneInfo("America/New_York")).date())
            state["underlying_cash_session"] = (
                "open" if session and session.open_at <= now < session.close_at else "closed"
            )
        except ValueError:
            state["underlying_cash_session"] = "unknown"
    side = Decimal(1 if state["position"]["side"] == "long" else -1)
    risk = Decimal(state["original"]["risk_price"])
    current_protected = (
        side * (Decimal(state["position"]["stop"]) - Decimal(state["position"]["entry"])) / risk
    )
    state["current_stop_protected_r"] = str(current_protected)
    for key, price in state["stop_candidates"].items():
        side = Decimal(1 if state["position"]["side"] == "long" else -1)
        state["stop_candidate_effects"][key]["protected_price_r"] = str(
            side
            * (Decimal(price) - Decimal(state["position"]["entry"]))
            / Decimal(state["original"]["risk_price"])
        )

        effect = state["stop_candidate_effects"][key]
        protected = Decimal(effect["protected_price_r"])
        effect["improvement_from_current_r"] = str(protected - current_protected)
        effect["giveback_from_observed_peak_r"] = str(Decimal(state["peak_r"]) - protected)


def candidates(position, market, original_stop, *, strategy=None):
    if market.get("return_4bar") is None:
        return {}, {}
    side = 1 if position["side"] == "long" else -1
    entry, current = (Decimal(position[k]) for k in ("entry", "stop"))
    price = Decimal(market["bid"] if side == 1 else market["ask"])
    step = Decimal(market["price_step"])
    swing = strategy == "flow_show_mirror"
    hourly = market.get("hourly", {})
    atr_raw = hourly.get("atr14_price") if swing else market["atr14_price"]
    proposed = {"original": Decimal(original_stop)}
    if atr_raw is not None:
        atr = Decimal(atr_raw)
        proposed.update(
            {"atr_" + str(n): price - side * atr * Decimal(str(n)) for n in (1, 1.5, 2)}
        )
        proposed["breakeven"] = entry
        structure = (
            hourly.get("support_prior20" if side == 1 else "resistance_prior20")
            if swing
            else market.get("support_prior20" if side == 1 else "resistance_prior20")
        )
        if structure is not None:
            proposed["structure"] = Decimal(structure) - side * max(
                step, Decimal(market["ask"]) - Decimal(market["bid"])
            )
    result = {}
    for key, candidate in proposed.items():
        candidate = (candidate / step).to_integral_value(
            rounding=ROUND_CEILING if side == 1 else ROUND_FLOOR
        ) * step
        tightening = side * (candidate - current) > 0
        if (
            candidate > 0
            and side * (candidate - Decimal(original_stop)) >= 0
            and side * (price - candidate) > 0
            and candidate != current
            and (not tightening or side * (price - entry) > 0)
            and str(candidate) not in result.values()
        ):
            result[key] = str(candidate)
    return result, {}


def questions(state):
    from typesafe_sdk import Choice

    common = INSTRUCTIONS + " Original strategy: " + previous.RUBRICS[state["strategy"]]
    result = {
        "action": Choice(
            instructions=common + " Decide retention or exceptional full exit.", criteria=ACTION
        ),
    }
    for direction in ("TIGHTEN", "LOOSEN"):
        for name, effects in stop_options(state, direction).items():
            result["price_" + name] = Choice(
                instructions=common
                + " Replacement direction: "
                + direction
                + ". "
                + PRICE_INSTRUCTIONS.format(name=name, price=effects["price"])
                + (
                    PROTECTED_PRICE_INSTRUCTIONS
                    if Decimal(state["current_stop_protected_r"]) >= 0
                    else INITIAL_PRICE_INSTRUCTIONS
                ),
                criteria=PRICE_CRITERIA,
            )
    return result


def select_exposure(state, answers, *, probability=None, lead=None):
    answer = answers.get("action", {})
    if not previous.confident(answer, ACTION, probability=probability, lead=lead):
        return None, "uncertain_exposure"
    if answer["choice"] == "ABSTAIN":
        return None, "exposure_abstained"
    return answer["choice"], "clear_full_exit" if answer[
        "choice"
    ] == "CLOSE_ALL" else "retain_position"


def select_stop(state, answers, *, probability=None, lead=None):
    gates = {"probability": probability, "lead": lead}
    side = 1 if state["position"]["side"] == "long" else -1
    for direction in ("TIGHTEN", "LOOSEN"):
        approved = [
            (key, Decimal(value["price"]))
            for key, value in stop_options(state, direction).items()
            if previous.confident(answers.get("price_" + key, {}), PRICE_CRITERIA, **gates)
            and answers["price_" + key]["choice"] == "approve"
        ]
        if approved:
            # Each approval is an actual replacement judgment. Prefer protection;
            # preserve room within that direction, or loosen by the least amount.
            return min(
                approved, key=lambda x: side * x[1] if direction == "TIGHTEN" else -side * x[1]
            )[0], "stop_selected"
    return None, "no_suitable_stop_price"


POLICY_HASH = hashlib.sha256(
    json.dumps(
        [
            VERSION,
            MODEL,
            INSTRUCTIONS,
            ACTION,
            STOP,
            PRICE_INSTRUCTIONS,
            PRICE_CRITERIA,
            PROTECTED_PRICE_INSTRUCTIONS,
            INITIAL_PRICE_INSTRUCTIONS,
            THRESHOLDS,
            REDUCTION_POLICY,
            previous.RUBRICS,
        ],
        sort_keys=True,
    ).encode()
).hexdigest()
