# Give this folder to your AI

1. Download and extract the AI starter ZIP.
2. Open the extracted folder in an AI coding tool that can read files and run Python, such as Codex or Claude Code.
3. Paste this prompt and describe your strategy:

> Read START_HERE.md and use this Liquid Strategy Lab project to test my strategy with simulated money. Handle setup and run the tests for me. Ask about important missing rules, explain the data and cost assumptions, and give me the results here in the conversation. Here is my strategy: [describe your idea].

You do not need to type terminal commands or open the HTML report yourself. Your AI handles the commands and explains the saved results. The HTML and trade files remain available if you want to inspect them.

The AI needs file access and a Python execution environment with the required dependencies. A chat that can only read an uploaded file cannot execute the backtester. Check your chosen tool's capabilities before assuming an upload will run.

## Instructions for the AI

1. Read AGENTS.md, docs/SETTINGS.md and docs/RESULTS.md. Locate this folder; quote paths containing spaces. Explain the plan in a few sentences and ask only for material missing choices.
2. Check whether execution tools and Python 3.12 are available. If uv is available, run `uv sync --frozen` from this folder. Follow its official installation instructions if needed and permitted. If execution or setup is unavailable, explain the specific missing capability; do not invent a result.
3. Run the included demo once into a new folder, without `--open`. This is a setup check using authored synthetic data, not a test of the user's strategy. Read the resulting JSON and run the independent arithmetic checker.
4. Translate the user's idea into explicit market, timeframe, entry, exit, sizing, leverage and data requirements. Check that the executable rules match the request. Do not silently substitute a built-in strategy for a different idea.
5. For a supported template, create a settings file and run it. For a new strategy, implement and test an isolated research adapter when your coding tools permit it, reusing the engine's ledger/execution model and following CONTRIBUTING.md. Preserve accounting and existing templates. If a rule or required input cannot be implemented or verified, report it as unsupported.
6. Use available, permitted data and identify its provenance. The bundled candles are synthetic; the simple CSV path assumes zero funding. Exact news, Flow, GEX and historical AI strategies require their timestamped inputs. State all missing data and assumptions. Do not fabricate a historical series or call synthetic gains evidence of profitability.
7. Keep results in a new project-local `outputs/` folder for each run. Compare a baseline with the proposed change and higher costs where applicable. Use the independent checker for its supported BTC/portfolio result formats; add appropriate reconciliation checks for any new adapter. A passing demo does not verify a new strategy implementation.
8. Explain the outcome in the conversation: what rules ran, data coverage, net result after costs, fees/funding/execution assumptions, largest decline, completed trades, open positions and important limitations. Link the saved evidence. Clearly separate software working from a strategy showing evidence of an edge.

Useful setup commands, for the AI to run from the extracted folder:

```sh
uv sync --frozen
uv run --frozen liquid-lab demo --output outputs/setup-check
uv run --frozen python scripts/verify_calculations.py outputs/setup-check --output outputs/setup-arithmetic
```

Choose unused output folders on later runs. For a built-in BTC test, use `liquid-lab run --config <settings.json> --output <new-folder>`; the settings guide documents its actual controls. The supplied BTC template has fixed sizing, so changing initial cash alone does not implement a requested risk-per-trade rule.

## What this download includes

The audited v0.1.1 simulation engine, existing BTC/portfolio examples, cost settings, synthetic data, arithmetic checker and documentation. New user strategies may require the coding AI to add and test their rules. This is a local AI-assisted workflow; it does not install a hosted connector or make every chat client executable.

No wallet, trading-account connection or paid model call is required by the examples. Keep all work scoped to research. See [settings](docs/SETTINGS.md), [calculation details](docs/CALCULATIONS.md) and [testing walkthrough](docs/TEST_IT.md).
