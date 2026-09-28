# A simple community walkthrough

Give members the [starter ZIP](https://github.com/wheelieinvestor/liquid-strategy-lab/releases/latest/download/liquid-strategy-lab-ai-starter.zip) and the prompt in [START_HERE.md](../START_HERE.md).

1. **Describe a rule.** Start with the included 20/50 moving-average trend example, or give the AI explicit entry/exit rules. The AI checks what will run.
2. **Run five markets.** The engine generates rising, falling, sideways, choppy and sudden-drop prices. Each has a fresh $1,000 simulated account and $200 target exposure by default.
3. **Read the result.** The AI explains gain/loss before costs, biggest account decline, trades and open positions. The optional HTML report shows the same evidence.
4. **Inspect a trade.** Point out that a completed candle produces a decision and the next candle's open supplies the fill. The final decision has no future candle to trade on.
5. **Check the arithmetic.** Have the AI run `scripts/verify_calculations.py`; it independently reconstructs the recorded economics.
6. **Change one rule.** Keep the scenarios, seed and sizing fixed. Compare each market with its earlier result. Then try other seeds without hiding poor outcomes.

Say explicitly: “This tests how your rules behave under fictional conditions. All trading costs are excluded. It does not tell us what you would have made on Liquid.”

Members receive a folder they can reuse with their AI, not an account login or a hosted website. Their AI must be able to read files and execute Python. The HTML report can also be opened without a server.
