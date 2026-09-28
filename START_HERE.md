# Give this folder to your AI

Download and extract the starter ZIP, open the folder in an AI coding tool, and paste:

> Read START_HERE.md. Use this project to test my strategy on synthetic data, with all trading costs excluded. Handle setup and execution, check that the rules match my idea, and explain the results here. My strategy is: [your rules].

Your tool needs file access and Python execution. Uploading files to a chat that cannot run code will not run the engine. You do not need a wallet, Liquid login, hosted connector or localhost website. Your AI explains the results in the conversation; saved reports are optional to open.

## Instructions for the AI

1. Read AGENTS.md, docs/SYNTHETIC.md and docs/RESULTS.md. Use the synthetic sandbox by default. Locate the folder and quote paths containing spaces.
2. Use Python 3.12 and `uv sync --frozen` for setup. If execution or dependency installation is unavailable, state that specific limitation. Do not invent a result.
3. Translate the idea into explicit entry, exit, direction and position-size rules. Ask only about material missing rules. The default is a 20/50 moving-average trend strategy; it is not a substitute for an unrelated user strategy.
4. Use `examples/strategy.json` for the built-in rule. For other price-based rules, create a local Python file with `decide(bars, position)` and a custom settings file, following docs/SYNTHETIC.md. Verify entry/exit examples and timing with focused tests. Preserve the shared ledger. Do not silently approximate unsupported rules such as intrabar stops, limit fills or external news inputs.
5. Run all five scenarios with the same rules, seed and sizing. The starter excludes all fees, spread, slippage and funding. Use a new output directory for every run. Custom Python is trusted local code: inspect it for future-data access, external inputs and side effects. The engine's past-only callback is not a security boundary.
6. Read the saved JSON and run the independent arithmetic checker. Explain gains/losses before costs, largest decline, completed trades, open endpoint positions, rules and assumptions. A result includes unrealized profit/loss on positions still open.
7. For a rule change, compare against the original on identical scenarios and seeds. Try additional seeds without selecting only good results. Synthetic paths are deliberately designed exercises, not calibrated Liquid history, and do not establish profitability or eliminate overfitting.
8. Answer in plain language in the conversation and link the saved evidence. Keep all work scoped to research. Use the advanced cost-aware tools only when the user wants their additional assumptions.

Commands for the AI to run from this folder:

```sh
uv sync --frozen
uv run --frozen liquid-lab sandbox --config examples/strategy.json --output outputs/my-strategy
uv run --frozen python scripts/verify_calculations.py outputs/my-strategy --output outputs/my-strategy-arithmetic
```

For a custom rule, replace the config with your custom settings file; `examples/custom-strategy.json` is a working example. Choose unused output folders on repeat runs.

## What people receive

A downloadable folder containing the engine, synthetic price generator, editable strategy examples, instructions for an AI, readable reports and calculation checks. Everything runs locally after setup. A Python file holds the strategy; a small JSON file holds its settings. No service hosting or account connection is required.
