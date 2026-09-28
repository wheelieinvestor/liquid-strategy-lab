# The simple strategy playground

Run `uv run --frozen liquid-lab sandbox` for five fictional scenarios with no trading costs. The data and simulated returns are teaching exercises, not historical Liquid prices or calibrated forecasts.

## Settings

Use `--config examples/strategy.json --output outputs/new-run`. Unknown fields are rejected.

| Setting | Meaning |
|---|---|
| `title` | Report label |
| `strategy` | `sma_trend` (default) or `custom` |
| `fast`, `slow` | Built-in moving-average lengths; default 20 and 50 completed bars; fast < slow <= 200 |
| `direction` | `long_short` or `long_only` |
| `initial_cash` | Starting simulated dollars; default 1000 |
| `position_notional` | Dollar exposure at each new entry; default 200, capped by available cash |
| `seed` | Repeatable random variation; default 7 |
| `bars` | 15-minute bars per scenario; default 512, allowed 300–10000 |
| `strategy_file` | Required only for `custom`; local `.py` file relative to the config |

The built-in rule holds until it has `slow` completed bars. It targets long when the fast average is above the slow, short when below (flat in long-only mode), and holds on a tie. It enters on the first eligible trend observation, even if no crossover has occurred during the visible run. Same-side signals retain the existing position without resizing.

## Custom rules

See [custom_strategy.py](../examples/custom_strategy.py) and [custom-strategy.json](../examples/custom-strategy.json). Run:

```sh
uv run --frozen liquid-lab sandbox --config examples/custom-strategy.json --output outputs/custom
```

Your local file defines:

```python
def decide(bars, position):
    if len(bars) < 20:
        return "hold"
    average = sum(bar.close for bar in bars[-20:]) / 20
    return "long" if bars[-1].close > average else "flat"
```

`bars` is an immutable tuple of completed candles, oldest first. Each frozen bar contains `index`, `open_us`, `open`, `high`, `low`, `close`; prices use Decimal. The current candle is complete when the function runs. `position` is a frozen snapshot with `side` (long/short/flat), signed `quantity`, and `entry_price` (None when flat).

Return `long`, `short`, `flat` or `hold`. A different target closes the existing position then opens the requested side at the **next bar's open**. Flat closes; hold makes no change. Short in long-only mode is an error. No fill can occur after the final bar. Open positions remain marked at its close. The strategy module is freshly loaded for every scenario; keep its randomness seeded and its inputs local for reproducible runs.

The callback receives no future bars or live ledger. Tests change future prices and verify earlier decisions and fills remain unchanged. Custom Python still runs as trusted local code and could read outside data; inspect and test it. This interface cannot certify arbitrary code free of look-ahead bias.

## Accounting and assumptions

Each scenario starts a separate account. All fee, spread, slippage and funding charges are exactly zero. Entry and exit prices equal the next synthetic open. Size floors to eight decimals. The shared Decimal ledger tracks realized and unrealized profit/loss; account equity is starting cash plus both. A reversal is two fills at the same open, with no cost. Position sizing uses 1x accounting and caps new entry exposure to available cash. It does not keep exposure at a constant percentage after entry. A run fails explicitly if account equity reaches zero or becomes negative.

This mode assumes full liquidity. It does not simulate order books, partial fills, intrabar stops, limit orders, leverage or venue liquidation. Candle high/low values can inform a decision only after that candle closes; they never trigger a retrospective fill. Drawdown is measured at saved candle closes, not every intrabar extreme. Advanced execution tools are separate and retain their own cost assumptions.

## Scenarios and fair comparisons

Every run includes rising, falling, sideways, choppy and sudden-drop paths. Default length is 512 bars, or 128 fictional hours. The drop is a 25% opening gap at bar index 256. Scenario construction deliberately creates different conditions; its returns and their frequency are not calibrated to a venue. Timestamps provide ordering only, not historical provenance.

Keep the seed fixed to compare a rule change on identical data. Then vary the seed and retain all results. Do not tune against these five exercises and call the best result proof of an edge. Synthetic testing is useful for finding rule mistakes and scenario weaknesses; historical evidence and execution costs are separate questions.
