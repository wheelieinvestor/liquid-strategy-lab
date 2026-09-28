# AI guide: take the member from an idea to a first test

The member begins by pasting the entire `00_START_HERE.txt` prompt. In the starter ZIP it sits beside an `engine/` folder; this guide is inside `engine/`. In a source checkout all of these files are at the repository root. The prompt is also available inside the engine for reference.

## First interaction

Check available tools and locate the files. If the member supplied only the prompt, use its versioned public download link when file download is supported. Otherwise ask for the ZIP or access to the extracted folder. If you cannot execute Python or install dependencies, explain the precise missing capability and help the member open the package in an environment that can. Clarifying rules is still useful; do not invent execution or results.

In the first reply, explain in two short sentences that you will turn their rules into a test and compare their behavior across five fictional markets, before costs. Ask at most three questions at once, only for missing information:

- What strategy would they like to try? If unsure, offer the included moving-average example.
- What should trigger entry and exit?
- Should it trade long, short, or both, if that is not already clear?

Do not make the member fill in a long technical questionnaire. The defaults are 15-minute candles, a $1,000 simulated account, $200 target exposure, 512 bars and seed 7. State defaults briefly and use them unless the requested strategy needs a material choice. If they already described complete supported rules, summarize them and proceed.

## Run the strategy

1. Read AGENTS.md, docs/SYNTHETIC.md and docs/RESULTS.md. Keep the work scoped to local research.
2. Work from the directory containing `pyproject.toml`: `engine/` in the starter ZIP, or the source checkout root. Quote paths with spaces. Use Python 3.12 and `uv sync --frozen` for setup. Initial setup needs dependency downloads; the included strategy runs are offline. Use the official uv installation instructions when needed and allowed. Do not tell the member to run commands you can handle yourself.
3. Translate the idea into explicit entry, exit, direction and sizing rules. Briefly show the member what will run. Ask for a material missing decision or an unsupported rule; do not repeatedly request confirmation for ordinary authorized setup and tests. The built-in 20/50 moving-average trend rule is not a substitute for an unrelated strategy.
4. Use `examples/strategy.json` for the built-in rule. For other supported price-based rules, create a local Python file defining `decide(bars, position)` and a custom JSON settings file as documented in docs/SYNTHETIC.md. Verify relevant entry/exit examples and timing with focused tests; preserve the shared ledger. Do not silently approximate intrabar stops, limit fills, unsupported timeframes or missing external inputs.
5. Inspect custom Python for future-data access, unseeded randomness, external inputs and side effects. Only completed bars reach the callback, with next-open execution, but trusted custom Python is not isolated from external data.
6. Run all five scenarios with identical rules and sizing. Fees, spread, slippage and funding are exactly zero in this mode. Use a new output directory for every run. A successful setup example does not verify a different strategy.
7. Read the saved JSON and run the independent arithmetic checker. If an operation fails, explain the actual error and the next practical step. Do not present an earlier bundled report as a newly executed result.

Example commands for you to run from the engine directory:

```sh
uv sync --frozen
uv run --frozen liquid-lab sandbox --config examples/strategy.json --output outputs/my-strategy
uv run --frozen python scripts/verify_calculations.py outputs/my-strategy --output outputs/my-strategy-arithmetic
```

For a custom rule, replace the config with your custom settings file; `examples/custom-strategy.json` is a working example. Choose unused output folders for later runs. Refer to docs/TROUBLESHOOTING.md for common errors.

## Explain the result and lead into the next step

Answer in the conversation; opening HTML is optional. Explain the rules tested, gains/losses before costs, largest account decline, completed trades, positions still open and the most useful scenario weakness. Open positions contribute unrealized profit/loss. Include links to the saved report and calculation evidence.

Say plainly that authored synthetic scenarios are not calibrated Liquid history and do not establish profitability or eliminate overfitting. Separate software working from evidence of a strategy edge. Use the advanced cost-aware tools only when the member asks for their additional assumptions.

Suggest one useful next experiment based on what happened, such as changing one entry/exit rule. Compare that change on matching scenarios and seeds. Additional seeds are useful, but retain their poor results too. Let the member choose the experiment instead of searching indefinitely for a flattering result.
