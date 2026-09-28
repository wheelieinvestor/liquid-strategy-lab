# What the cost model calculates

The starter `sandbox` excludes fees, spread, slippage and funding entirely. Its equity is starting cash plus realized and unrealized price profit/loss; see [the synthetic guide](SYNTHETIC.md). The cost assumptions below apply only to the separate advanced commands.

For BTC perps, each filled side pays `abs(quantity × execution price) × fee rate`. The community default is `0.00095`, or 0.095%, matching Liquid's published tier-0 crypto-perpetual taker rate checked September 28, 2026. This is the all-in rate: do not add the Liquid builder fee again. Account tiers, referrals, venue routing and time can change the applicable rate. [Liquid fee schedule](https://docs.tryliquid.xyz/trading/fees).

Fees apply to notional, not collateral. A $2,000 fill at this rate costs $1.90; an equal-notional exit costs another $1.90. Leverage changes collateral and exposure; it does not multiply an already notional-based fee again. The BTC template requests $2,000 notional at 40x, subject to admission and precision checks.

For candle simulations, each side pays half the configured full spread plus the configured extra slippage. Buys round up and sells round down to the price step. This applies to entries, software exits, intrabar stops, and modeled target exits. A stop gap uses the adverse opening price before applying these costs. Candle exits assume sufficient depth; exact intrabar execution is not observed.

The detailed report's informational slippage is the combined price difference from spread, extra slippage, and tick rounding. Book fills use the supplied mark (otherwise midpoint) as reference; candle exits use the pre-cost trigger/gap price. It includes entries and exits when a reference is present. It is already in execution-price profit/loss, so subtracting it again would double-charge it. Old saved runs may have incomplete reference coverage; rerun them with the corrected release.

Signed funding is `-position quantity × oracle price × hourly settlement rate`. Positive rates debit longs and credit shorts; negative rates reverse that. Duplicate delivery of one settlement must not charge it twice or inflate trade statistics. The ledger and trade summaries follow the same settlement identity. Hyperliquid settles hourly using oracle notional; an eight-hour quoted rate must be converted to the applicable hourly settlement rate. [Hyperliquid funding documentation](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding).

The demo and simple CSV import assume zero funding and say so in their metadata. The advanced archive path uses cached settlement rates and approximates oracle price with the proxy minute open. Intraminute funding/stop ordering remains ambiguous. The four-agent fixture is synthetic and does not calibrate fees for every Liquid venue.

Account equity equals starting cash plus realized and unrealized price profit/loss, minus fees, plus signed funding. Open positions remain marked at the endpoint; a hypothetical exit fee is not silently charged. Reported drawdown is peak-to-trough decline across saved equity observations, not a claim to know every intraminute account extreme. Cross and isolated margin, reservations, leverage tiers and maintenance thresholds are tracked separately; exact liquidation fills are unsupported.

## September 2026 audit corrections

Version 0.1.1 corrects four issues found by tracing calculations and reproducing them:

- Candle-triggered exits now apply the configured spread/slippage before adverse tick rounding.
- Informational execution friction now includes exits; it is never deducted twice from equity.
- Repeated delivery of a funding settlement no longer duplicates it in trade-level statistics. The account ledger already deduplicated it.
- An entry whose stop cannot be installed exits against opposite-side depth, and retains any unfilled emergency exit through restart. It no longer assumes an immediate full close at the entry price.

A zero-spread synthetic control can use an equal bid and ask; observed books still require a valid spread. A zero-cost control can retain rounding friction when prices fall between allowed ticks.

The arithmetic checker in `scripts/verify_calculations.py` uses an independent signed-cash-flow reconstruction. The repository tests also include hand-worked long/short trades, fee rebates, partial exits, signed funding, gap stops, insufficient depth, maintenance tiers, and restart behavior.
