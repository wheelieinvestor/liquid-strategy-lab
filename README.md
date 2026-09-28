# Liquid Strategy Lab

**Test Liquid trading strategies on your own computer, using simulated money.**

Built for Liquid users and the ATG community. Download it, run the included example, change one setting, and compare the results in your browser. No wallet, Liquid login, API key, or paid data is needed for the examples.

This is independent community software, not an official Liquid product. It simulates trading; it cannot place live orders.

**Want your AI to handle it?** Download the [AI starter ZIP](https://github.com/wheelieinvestor/liquid-strategy-lab/releases/latest/download/liquid-strategy-lab-ai-starter.zip), open its folder in an AI coding tool, and copy the prompt from [START_HERE.md](START_HERE.md). Your AI runs the local engine and explains the results in the conversation. It needs file access and Python execution; new strategies may require it to add and test their rules.

## Start here

Use **Python 3.12**, managed automatically by [uv](https://docs.astral.sh/uv/getting-started/installation/). The release workflow checks Windows, macOS, and Linux; its result is visible in [Actions](https://github.com/wheelieinvestor/liquid-strategy-lab/actions).

1. Install uv using its [official instructions](https://docs.astral.sh/uv/getting-started/installation/). On Windows, `winget install --id=astral-sh.uv -e` is an option; on a Mac with Homebrew, `brew install uv` is an option. Close and reopen your terminal afterward.
2. Download and extract **Source code (zip)** from the [latest release](https://github.com/wheelieinvestor/liquid-strategy-lab/releases/latest). Open a terminal in that extracted folder. On Windows, right-click inside the folder and select **Open in Terminal**. On macOS, type `cd ` in Terminal, drag the extracted folder into Terminal, and press Return.
3. Run these two commands:

```sh
uv sync --frozen
uv run --frozen liquid-lab demo --open
```

The first setup downloads Python and the locked dependencies. The demonstration then runs locally without network access. Your report is `outputs/demo/report.html`; double-click that file if it does not open automatically. Each run keeps its full data beside the report.

Already use Git? Clone `https://github.com/wheelieinvestor/liquid-strategy-lab.git`, enter the folder, and use the same commands.

## What you will see

The example compares the original-stop fallback with moving the stop to entry after a favorable move of **1R**. Here, 1R is the original distance between entry and stop. Moving to entry can still lose money after fees and slippage.

- Net simulated result after modeled costs.
- The largest decline from an earlier account-value peak.
- Trading fees, open positions, and completed trades.
- Both account-value curves and the simulated fills.
- The data assumptions and detailed decision records.

**The included prices are synthetic, deliberately authored for teaching. They are not historical Liquid prices and do not establish profitability.** A higher result on this example does not identify a winning strategy.

An [example report](examples/report.html) is included in the download; open it locally without installing anything. GitHub's normal file view shows the HTML source.

## Make your first change

Open `examples/btc.json` in a text editor. Change `breakeven-1R` to `breakeven-1.5R`, save it, and run:

```sh
uv run --frozen liquid-lab run --config examples/btc.json --output outputs/my-first-test --open
```

Use a **new output folder** for each comparison. Existing results are preserved. [Settings guide](docs/SETTINGS.md) explains every control, the fixed BTC sizing template, and the CSV format for your own permitted data.

## Explore the other Liquid agents

```sh
uv run --frozen liquid-lab templates
uv run --frozen liquid-lab portfolio --config examples/portfolio.json --output outputs/portfolio --open
uv run --frozen liquid-lab stress --family execution --output outputs/stress
```

| Strategy | Included workflow | Evidence limit |
|---|---|---|
| BTC Momentum | Synthetic/proxy candle tests and stop comparisons | Assumed execution; no reconstructed historical AI decisions |
| Flow Show Mirror | Synthetic shared-account example; captured-event replay | No bundled historical Flow source archive |
| XYZ100 GEX | Synthetic shared-account example; captured-event replay | No bundled historical GEX source archive |
| Inverse Cramer | Synthetic shared-account example; captured-event replay | No bundled historical classifications/news archive |

The portfolio example demonstrates four agents sharing capital. It is a short behavior example, not a historical performance study. Stress families also include `source`, `portfolio`, and `jev`.

## Walkthrough and reference

- [Test the setup yourself](docs/TEST_IT.md): first run, independent arithmetic checks, and higher costs.
- [Fees, slippage, funding and calculation details](docs/CALCULATIONS.md).
- [Host's walkthrough](docs/WALKTHROUGH.md): a repeatable community demonstration.
- [Settings and your own CSV data](docs/SETTINGS.md).
- [Reading results and limitations](docs/RESULTS.md).
- [Troubleshooting](docs/TROUBLESHOOTING.md).
- [Advanced replay, recording, and experiments](docs/ADVANCED.md).
- [Architecture and upstream adaptations](docs/ARCHITECTURE.md).
- [Contributing and checks](CONTRIBUTING.md).

Reports and inputs stay on your computer. Review them before voluntarily sharing: a report made from your own imported data may contain private information. There is no automatic Discord posting, telemetry, or account connection.

Code and authored demo data are available under the [MIT license](LICENSE). Third-party packages retain their own licenses. See [data provenance](docs/DATA.md).
