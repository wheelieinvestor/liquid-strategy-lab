# A simple community walkthrough

Share the [starter ZIP](https://github.com/wheelieinvestor/liquid-strategy-lab/releases/latest/download/liquid-strategy-lab-ai-starter.zip) with the copy in [SHARE_WITH_COMMUNITY.md](SHARE_WITH_COMMUNITY.md).

The extracted folder contains:

```text
Liquid Backtesting Starter/
  00_START_HERE.txt   <- Copy all of this into your AI
  engine/            <- The AI uses these files
```

The first file contains only the prompt. Members do not need to choose settings files or read the technical guide before getting started. The prompt tells the AI how to find the engine and includes a versioned download link if the AI receives only the text.

1. **Paste the prompt.** The AI checks its file and execution tools, explains the process, and asks what strategy the member wants to test. It asks no more than three missing questions at once. An uncertain member can choose the included example.
2. **Describe the rules.** The AI clarifies entry, exit and direction, using the standard simulated account and position size unless otherwise requested. Complete instructions let it proceed directly.
3. **Run five markets.** The AI handles supported setup and runs rising, falling, sideways, choppy and sudden-drop scenarios. It checks the arithmetic and explains the result in the conversation.
4. **Try one change.** The AI suggests one useful next experiment. The member chooses it, then compares on the same scenarios and seeds.

A tool that cannot execute Python cannot run the engine. The prompt explains this and helps the member access an environment that can. There is no hosted service or required localhost website; the optional HTML report opens as a file.

Say explicitly: “This tests how your rules behave under fictional conditions. All trading costs are excluded. It does not tell us what you would have made on Liquid.”
