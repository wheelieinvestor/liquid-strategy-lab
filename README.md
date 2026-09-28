# Liquid Strategy Lab

**Give your AI a strategy. Test its rules in five fictional markets.**

A simple, local strategy playground for Liquid users and the ATG community. The starter uses synthetic prices and simulated money, with **fees, spread, slippage and funding excluded**. No wallet, account, paid data or model API key is needed.

It helps you see when your rules trade, how they handle changing conditions, and where they lose money before costs. Synthetic gains do not establish that a strategy will make money on Liquid. This is independent community software, not an official Liquid product, and cannot place live orders.

## Give it to your AI

1. [Download the starter ZIP](https://github.com/wheelieinvestor/liquid-strategy-lab/releases/latest/download/liquid-strategy-lab-ai-starter.zip) and extract it.
2. Open the folder in an AI coding tool with file access and Python execution.
3. Paste this and describe your idea:

> Read START_HERE.md. Use this project to test my strategy on synthetic data, with all trading costs excluded. Handle setup and execution, check that the rules match my idea, and explain the results here. My strategy is: [your rules].

Your AI runs the engine and explains the results in your conversation. The HTML report is an optional saved view. A chat that only reads uploaded files cannot execute the engine; there is no hosted service or required localhost website.

[START_HERE.md](START_HERE.md) contains the instructions for your AI.

## Try it yourself

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), open a terminal in the extracted folder, and run:

```sh
uv sync --frozen
uv run --frozen liquid-lab sandbox --open
```

Setup downloads Python 3.12 and locked dependencies. The included strategies then run offline. The result is `outputs/sandbox/report.html`; double-click it if it does not open automatically. Use a new `--output` folder for later runs.

The starter applies a 20/50 moving-average trend rule to rising, falling, sideways, choppy and sudden-drop markets. Each scenario starts with $1,000 of simulated cash and targets a $200 position without leveraged sizing. It shows:

- Simulated gain or loss **before costs** and the largest account decline.
- Account-value curves, completed trades and positions still open.
- Every decision, price bar and fill, with an independent arithmetic checker.

[Open the included example report](examples/sandbox/report.html) locally to preview it without installing anything. GitHub's file view shows HTML source.

## Test your own rules

Change the settings in `examples/strategy.json`, then run:

```sh
uv run --frozen liquid-lab sandbox --config examples/strategy.json --output outputs/my-strategy
```

An AI can implement a different rule in a small Python file using `decide(bars, position)`. The included [custom example](examples/custom_strategy.py) demonstrates a breakout entry and moving-average exit. The engine supplies only completed bars; signals fill at the next candle's opening price. It supports long, short, flat and hold. It does not model intrabar stops or exchange execution in this mode.

[Strategy guide](docs/SYNTHETIC.md) · [Try it and verify the numbers](docs/TEST_IT.md) · [Host walkthrough](docs/WALKTHROUGH.md)

## Optional advanced tools

The existing cost-aware BTC, shared-account and replay tools remain available through `liquid-lab demo`, `run`, `portfolio`, `stress` and `liquid-research`. Their configured fees and execution assumptions still apply. Use them when those details are part of your research question; the starter does not require them.

[Advanced settings and CSV data](docs/SETTINGS.md) · [Calculation details](docs/CALCULATIONS.md) · [Replay tools](docs/ADVANCED.md) · [Reading results](docs/RESULTS.md) · [Troubleshooting](docs/TROUBLESHOOTING.md)

Runs stay on your computer. No automatic posting, telemetry or account connection. Review imported data before sharing it. Code and authored synthetic data use the [MIT license](LICENSE); third-party packages retain their licenses. See [data provenance](docs/DATA.md), [architecture](docs/ARCHITECTURE.md) and [contributing](CONTRIBUTING.md). Release checks run on Windows, macOS and Linux in [Actions](https://github.com/wheelieinvestor/liquid-strategy-lab/actions).
