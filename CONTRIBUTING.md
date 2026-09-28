# Contributing

Use Python 3.12 and the committed uv lockfile. Run:

```sh
uv sync --frozen
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen python -m pytest -q
```

No separate type checker is configured. The release workflow also checks a fresh ZIP-style install on Windows, macOS, and Linux and an installed wheel outside the source folder.

Build the member download from a committed revision with `uv run --frozen python scripts/build_starter.py --output outputs/liquid-strategy-lab-ai-starter.zip`. Use a new output path; existing archives are preserved. Its top level contains only `00_START_HERE.txt` and `engine/`. The engine retains the full source tree, including a reference copy of the prompt. `scripts/verify_install.py` checks this exact bundle layout and runs the commands from its engine directory. Commit intended changes before that check, since the archive reads HEAD rather than working files.

The prompt pins its download URL to the package release. Update that URL with the version metadata for a release, and upload the ZIP as `liquid-strategy-lab-ai-starter.zip`. Keep its first file entirely prompt text. Verify the public download after publishing.

Keep tests tied to real behavior: accounting reconciliation, causal timing, sizing and exits, malformed inputs, reproducibility, and offline isolation. Do not add broker/account capabilities to the research package. Preserve evidence labels. Changes to simulation behavior must include affected failure cases.

Reports and outputs are ignored by Git except the explicitly authored example report. Do not commit credentials, personal paths, private data, environment files, or operational account state. Use issues for bugs and pull requests for changes.
