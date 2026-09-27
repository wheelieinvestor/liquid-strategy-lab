# Contributing

Use Python 3.12 and the committed uv lockfile. Run:

```sh
uv sync --frozen
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen python -m pytest -q
```

No separate type checker is configured. The release workflow also checks a fresh ZIP-style install on Windows, macOS, and Linux and an installed wheel outside the source folder.

Keep tests tied to real behavior: accounting reconciliation, causal timing, sizing and exits, malformed inputs, reproducibility, and offline isolation. Do not add broker/account capabilities to the research package. Preserve evidence labels. Changes to simulation behavior must include affected failure cases.

Reports and outputs are ignored by Git except the explicitly authored example report. Do not commit credentials, personal paths, private data, environment files, or operational account state. Use issues for bugs and pull requests for changes.
