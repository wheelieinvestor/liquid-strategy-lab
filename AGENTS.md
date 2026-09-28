# Agent guidance

This repository distributes local Liquid strategy research. Keep simulation commands independent of live brokers, account credentials, notifications, and inference calls. Preserve original strategy semantics, causal timing, exact accounting, and explicit evidence limitations.

The member-facing entry point is `00_START_HERE.txt`, a prompt they can paste into their AI. For a member asking to test a strategy, follow START_HERE.md and docs/SYNTHETIC.md. Clarify only missing rules, at most three questions at a time, then handle available setup/execution and explain the results in the conversation. In the starter ZIP, run commands from `engine/`, the directory containing pyproject.toml. Default to `liquid-lab sandbox`: generated scenarios with fees, spread, slippage and funding excluded. Support custom rules through the small `decide(bars, position)` interface. Preserve the separate advanced cost-aware workflows and label their assumptions clearly. Handle setup and execution through available tools, then explain the saved results in the conversation. Verify that the executed strategy matches the request; the included demo alone does not validate a new strategy.

Use the checks in CONTRIBUTING.md. Changes to execution/sizing/exit behavior need relevant tests. Verify quickstart and installed-package behavior before releasing. Do not copy operational databases or private research archives into the distribution.

On the maintainer's machine, follow the user's external-development-storage policy. Portable output directories for community users do not authorize moving local runtimes or bypassing that storage policy during development.
