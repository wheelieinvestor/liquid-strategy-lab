# Test the setup

From the extracted folder, run:

```sh
uv sync --frozen
uv run --frozen liquid-lab sandbox --output outputs/first-test --open
uv run --frozen python scripts/verify_calculations.py outputs/first-test --output outputs/first-test-arithmetic
```

The report compares the same 20/50 moving-average trend rule in five fictional markets. Fees, spread, slippage and funding are all excluded. The checker independently reconstructs fills, inventory, cash flows, account value, drawdown and completed-trade statistics. A pass validates the saved arithmetic, not an investment conclusion.

Change `fast` from 20 to 10 in `examples/strategy.json`, then use a new output folder:

```sh
uv run --frozen liquid-lab sandbox --config examples/strategy.json --output outputs/changed-rule
```

Compare each scenario with the same scenario in the first run. Keep seed, size and other rules fixed for this comparison. Then try other seeds and keep their results too.

To test your own rule file, use `examples/custom-strategy.json` as a starting point and read [the strategy guide](SYNTHETIC.md). Check that the actual entries and exits match your intended rules; the included example alone cannot verify a different strategy.

Each run saves `report.html`, `comparison.json`, and a detailed JSON/HTML pair for each scenario. The checker saves `arithmetic.json` and `fills.csv`. An open endpoint position contributes unrealized profit/loss; it is not silently closed.

The optional cost-aware commands remain available. See [advanced settings](SETTINGS.md) and [calculation details](CALCULATIONS.md). No account connection is part of this setup.
