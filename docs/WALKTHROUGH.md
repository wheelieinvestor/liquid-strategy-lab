# A community walkthrough for Liquid Strategy Lab v0.1.0

Audience: Liquid users and ATG Discord members. Suggested length: 15–20 minutes. This is the host's script; it does not send invitations or community messages.

## Before the session

Share the v0.1.0 release link and README. Ask participants to install uv and download/extract the release before the call. Keep an unmodified copy of `examples/btc.json` and the included `examples/report.html` available. A participant without a working installation can still follow the included report.

## 1. Explain the purpose

“This is a simulator. We can change a strategy rule and see what happens under the same conditions. These example prices are made up for teaching. The results are not live trades or a prediction.”

Explain that the strategy cannot see future bars. Costs, stops, order timing, and shared account limits affect the outcome.

## 2. Run the example together

From the downloaded folder:

```sh
uv sync --frozen
uv run --frozen liquid-lab demo --output outputs/workshop-original --open
```

Point out the synthetic-data label first. Then read the net result, largest decline, fees, and remaining open positions. Show both account-value curves and the trade rows. A smaller loss is still a loss.

“Preserve the original stop” is the missing-model fallback, not an exact replay of historical Jev decisions. “Move stop to entry at 1R” is a deterministic research variation. 1R is the original entry-to-stop distance; fees can make an entry-price stop lose money.

## 3. Make one change

Open `examples/btc.json`. Change `breakeven-1R` to `breakeven-1.5R`; leave every other setting unchanged. Save it, then run:

```sh
uv run --frozen liquid-lab run --config examples/btc.json --output outputs/workshop-variation --open
```

Compare the reports side by side. Ask: did net result, largest decline, trade count, and fees all move in the same direction? Explain that changing an exit also changes when capital is available for later entries. This is a whole-strategy comparison, not necessarily identical entry cohorts.

Do not repeatedly tune this small teaching dataset and call the best setting validated. A result needs sufficient unseen data and realistic execution evidence before it supports a performance claim.

## 4. Show the shared account

```sh
uv run --frozen liquid-lab portfolio --output outputs/workshop-portfolio --open
```

Show the BTC, Flow, XYZ100, and Cramer agents. Explain that this short synthetic event sequence proves how the machinery behaves together; it is not four strategies' historical returns. The linked run JSON retains accepted and rejected decisions.

## 5. Show an execution failure test

```sh
uv run --frozen liquid-lab stress --family execution --output outputs/workshop-stress
```

Open `outputs/workshop-stress/execution-summary.html`. These checks exercise order failures, delayed/partial fills, and accounting protections. A passing check is not a profitable trade.

## 6. Give members a small follow-up exercise

Choose one setting, write down the question it tests, and preserve both results. When sharing, include the release version, data type, settings, and limitations. Never share account credentials or private input files. Collect installation problems, confusing report labels, and useful research questions.

For the next session, restore the original settings from the downloaded release and choose new output names. The exact commands above are the same on Windows PowerShell, macOS Terminal, and Linux shells after uv is installed.
