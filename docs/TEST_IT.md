# Test the setup yourself

Start with the corrected v0.1.1 release or newer. Install uv and open a terminal in the extracted repository folder as described in the README.

## 1. Run the complete beginner example

```sh
uv sync --frozen
uv run --frozen liquid-lab demo --output outputs/my-check --open
```

Read the two account curves, net result, largest decline, fees, and open positions. Expand the fills and click a detailed report. With the unchanged included data and defaults, preserve-stop finishes at **-$96.72**, and moving the stop to entry at 1R finishes at **-$43.91**. Both still have one position open. These are synthetic examples, not predictions. Different inputs should give different results.

The first simulated entry buys 0.01988 BTC at $100,642. Its notional is $2,000.76296, and its 0.095% fee is $1.900724812. The reference price is $100,621.86; combined spread, slippage and price rounding cost $0.4003832 on this fill. That cost is already reflected in the execution price.

## 2. Check the numbers independently

```sh
uv run --frozen python scripts/verify_calculations.py outputs/my-check --output outputs/my-check-arithmetic
```

This checks fees on each fill, signed funding, inventory, average entry, realized profit/loss, ending equity, every saved account-value point, drawdown, and reported trade statistics. It uses signed trade cash flows without importing the engine's accounting or report calculations. The output includes `arithmetic.json` and a complete `fills.csv`.

It supports the saved all-taker BTC simulations and the authored portfolio fixtures. It verifies arithmetic under the saved assumptions, not whether those assumptions reproduce a live account.

## 3. Make costs worse

```sh
uv run --frozen liquid-lab run --config examples/btc-high-costs.json --output outputs/my-high-cost-check --open
```

This uses the same two policies and candles with 0.12% fee per side, an 8-basis-point full spread, and 5 basis points of extra slippage per fill. Compare the net result and drawdown with step 1. Larger costs can also change later trades through available capital and stops, so the difference need not equal a simple fee adjustment.

Use a new output folder each time. To test one cause at a time, copy `examples/btc.json`, change only one setting, and pass that file with `--config`.

## 4. Exercise the other agents and failure handling

```sh
uv run --frozen liquid-lab portfolio --output outputs/my-portfolio-check --open
uv run --frozen liquid-lab stress --family execution --output outputs/my-execution-check
uv run --frozen liquid-lab stress --family source --output outputs/my-source-check
uv run --frozen liquid-lab stress --family portfolio --output outputs/my-portfolio-stress
uv run --frozen liquid-lab stress --family jev --output outputs/my-jev-check
```

These four standalone stress families contain 400 cases. The additional 100 market cases and the original 120-run research study require historical archives that are not bundled. The short portfolio example checks four strategies sharing capital; it uses authored events and a uniform assumed fee, not venue-specific stock or commodity fees.

## 5. Decide whether it is useful

A useful setup must reproduce unchanged inputs, make its calculations inspectable, and expose missing data. The examples establish those properties. They do not establish a profitable strategy.

For performance research, import permitted minute data using the settings guide, freeze a few candidate rules before looking at their outcomes, and retain all results. Evaluate on later periods that were not used to choose the rules, across different market conditions, at the actual route/account fees and adverse execution assumptions. Compare net return and drawdown, not win rate alone. Ensure there are enough independent trades and periods to support any conclusion.

The simple CSV path assumes zero funding. Use the advanced historical/event interfaces when funding matters. Historical candles cannot establish exact fills, order-book depth, oracle prices, liquidation execution, or missing AI decisions. All supplied comparisons retain that limitation.

See [calculation details](CALCULATIONS.md), [settings](SETTINGS.md), and [advanced interfaces](ADVANCED.md).
