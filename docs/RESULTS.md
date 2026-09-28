# Reading your results

The starter `sandbox` report applies the **same strategy** to five different fictional markets. Its result is **before all trading costs**: fees, spread, slippage and funding are zero.

- **Simulated result before costs:** ending account equity minus starting cash, including unrealized profit/loss on positions still open.
- **Largest account decline:** biggest dollar fall from an earlier equity peak, measured at saved candle closes. It does not capture every intrabar extreme.
- **Completed trades:** positions fully closed. A reversal closes one trade and opens another.
- **Positions still open:** remaining exposure marked at the final close. There is no assumed final exit.

The curves show separate accounts under different scenarios. A rising-market result is not a baseline against which a falling-market result represents a strategy improvement. Compare rule changes on matching scenarios and seeds. A report with no trades may reflect the rules never triggering, rather than a failure.

Open `comparison.json` for settings, metrics and run hashes. Each scenario JSON includes every generated bar, decision, fill and ledger transition. The arithmetic checker independently checks recorded results; it cannot certify that user-written rules match an idea or never access outside data.

Synthetic tests show behavior under authored conditions. Positive results do not establish profitability on Liquid; synthetic paths do not eliminate look-ahead bugs or selection overfitting. [The strategy guide](SYNTHETIC.md) explains the timing and scope.

## Advanced cost-aware results

The older `demo`, `run` and `portfolio` reports use their configured cost model. Their “Net simulated result” includes fees, modeled funding and execution prices with modeled friction. Informational slippage is already included in fill prices and must not be deducted again. Refer to [calculation details](CALCULATIONS.md).

Advanced comparisons also expose missing original AI decisions, source coverage, breaches and other execution limitations. Missing inputs do not become exact historical evidence merely because a replay completes. Preserve these labels when sharing results.
