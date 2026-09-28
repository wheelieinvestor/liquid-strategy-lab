# Architecture and extraction

The primary `liquid-lab sandbox` workflow is a small strategy runner in `liquid_strategy_lab/sandbox.py` with generated bars from `synthetic.py`. It shares the Decimal ledger but supplies its own idealized zero-cost fills, 1x sizing and past-only custom callback. It does not invoke the advanced strategy-specific risk/stop/venue model described below. Signals execute at the next bar open. `scripts/verify_calculations.py` also checks this saved-run schema independently.

The public command `liquid-lab` loads local settings/data, invokes the causal simulator, and writes a self-contained HTML comparison plus JSON evidence. The `liquid-research` command exposes lower-level replay, recording, archive, and portfolio tools.

The inherited engine remains in the `liquid_autonomous_trader` Python namespace to preserve its tested imports. The beginner interface, dataset validation, portable provenance, and report live in `liquid_strategy_lab`.

The simulation uses Decimal accounting, delayed executable observations, shared risk admission, ownership, native-stop modeling, recovery state, and the BTC/Flow/XYZ/Cramer strategy code. Source revisions become visible only when available. An in-memory account facade drives source and management paths; it has no live transport.

This export deliberately removes the live controller, broker/MCP transport, OAuth implementation, Postgres classification reader, live inference function, and source polling workers. Required exception/value types and captured-input validators remain. Public market recording is a separate explicit capability. Source/account aliases were replaced by research identities. The original operational repository and history were not published.

`UPSTREAM.json` records the initial file-extraction hashes from upstream commit `c7082a5dece58a75829f2293f9905353c0cf25c2`. These are lineage records, not hashes of the final adapted distribution. Portability changes replace the personal external-volume guard, Unix-only memory measurement, Git-HEAD dependency, and current-directory lockfile dependency. Run receipts fingerprint installed source and dependency versions, which work for both Git and ZIP installations.

The beginner CSV path injects validated minute rows and explicit zero-assumed funding into the same historical driver. The archive path keeps its original readers. Included examples preserve the original signal/admission/exit behavior. Tests cover both paths and side-effect isolation.

Memory figures are observed RSS (Windows may expose peak working set), not a cross-platform guaranteed peak measure. Budget checks are cooperative safeguards for local use, not a hosted multiuser isolation boundary.
