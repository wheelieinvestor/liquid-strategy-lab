# Advanced cost-aware settings and data

For the simple zero-cost starter, use [the synthetic strategy guide](SYNTHETIC.md).

`examples/btc.json` is the advanced BTC momentum template. Unknown fields are rejected rather than silently ignored.

| Setting | Meaning |
|---|---|
| `title` | Label printed in the report |
| `strategy` | `btc_momentum` for candle simulation |
| `dataset` | `bundled:btc-demo`, or a CSV path relative to the settings file |
| `data_kind` | `synthetic` for authored prices; `proxy` for observed prices with assumed execution |
| `data_description` | Describe the source, instrument, venue, and missing evidence |
| `initial_cash` | Starting simulated balance; default $1,000 |
| `fee_per_side` | Decimal fee on each fill; `0.00095` means 0.095%, not 0.00095% |
| `spread_bps` | Assumed total bid/ask spread; 1 basis point is 0.01% |
| `slippage_bps` | Additional adverse execution adjustment per side |
| `policies` | One to five distinct stop/exit variants |

Policies: `preserve-stop`, `legacy-exits`, `breakeven-1R`, `breakeven-1.25R`, and `breakeven-1.5R`. Preserve-stop retains the existing stop and quantity when the original AI response is absent. Legacy-exits calls the inherited deterministic management rules. Breakeven variants act on completed 15-minute management observations; commands execute only at a later executable observation.

The inherited BTC sizing template requests $2,000 notional at 40x ($50 initial collateral), subject to precision, risk, account, and instrument checks. Changing starting cash does not proportionally scale that order. These are simulation template parameters, not a recommendation to trade at that leverage. Editing sizing rules is an advanced code change requiring the affected tests.

The default fee is an explicit illustrative Liquid crypto-perpetual assumption. Route, account tier, referral discounts, and historical rates can differ. Check the [Liquid fee schedule](https://docs.tryliquid.xyz/trading/fees) and your applicable evidence. Do not apply this preset to every Liquid market.

## Your own BTC CSV

Use data you have permission to use. Create a UTF-8 file with exactly these columns:

```csv
timestamp,open,high,low,close,volume
2025-01-01T00:00:00+00:00,100000,100010,99990,100005,2
```

Rows must be contiguous one-minute candles, ascending and unique, with valid positive OHLC prices and nonnegative volume. Include 24 hours of warmup plus at least 30 minutes to test. Start and end on 15-minute boundaries. Maximum: 200,000 rows and 32 MiB. The warmup period does not contribute trades or reported profit.

Copy `examples/btc.json`, point `dataset` at your file, and use `data_kind: "proxy"` for observed market prices. Describe the venue and instrument honestly. This interface does not infer native execution from a candle CSV. Funding is zero by assumption in this simple CSV path, and reports label it that way; use advanced replay for explicit timestamped funding events. Running a larger file can reach the cooperative 10-minute / 2-GiB budget.

## Portfolio template

`examples/portfolio.json` selects enabled agents, initial cash, and the exit policy for the authored shared-account event sequence. Available agents are `btc_momentum`, `flow_show_mirror`, `xyz100_gex`, and `inverse_cramer`. Disabling an agent lets you inspect competition for capital. The fixture includes its own instrument precision, margin, and cost assumptions. More complete custom portfolios require the advanced event/manifest interface.
