# Advanced engine interfaces

Run `uv run --frozen liquid-research --help`, then a subcommand's `--help` for exact arguments.

| Command | Purpose |
|---|---|
| `historical` | Checksum-verified BTC proxy archive simulation with explicit assumptions |
| `portfolio` | Causal event catalog plus instrument/fee/engine manifest; checkpoint/resume |
| `replay` | BTC decision as of a specified event-availability time |
| `report` | Render a saved engine result |
| `batch` | Frozen stress campaign; market family requires the documented historical cache |
| `compare` | Original frozen five-policy research protocol; requires its historical cache |
| `record`, `record-stream` | Explicit bounded public market-data reads; source spools must be supplied |
| `import`, `export`, `health` | Versioned event catalogs, checksums, and coverage |
| `normalize-sources` | Convert supplied sanitized source records into replay events |
| `cache-models` | Audit supplied original model records; no inference |
| `calibrate` | Compare supplied native fee evidence with the model |

The original 120-run protocol and full 500-case campaign need historical datasets that are not bundled. They are expert interfaces, not first-run examples. Four standalone synthetic stress families run with `liquid-lab stress`: execution, source, portfolio, and Jev. The beginner demo and shared-account example are entirely self-contained.

For full custom portfolio input schemas, see `tests/backtesting/test_portfolio.py`, the event type in `backtesting/events.py`, and authored examples in `backtesting/fixtures.py`. Use `liquid-research export` to serialize a catalog and retain its manifest. Unknown/missing inputs remain unsupported; do not backfill current news or GEX into past decisions.

The account and execution facades in this distribution are simulation capabilities. There is no command to activate a live strategy or connect a trading account.
