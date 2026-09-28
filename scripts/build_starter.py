"""Build the prompt-first member ZIP from a committed Git revision."""

import argparse
import io
import subprocess
import zipfile
from copy import copy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = "Liquid Backtesting Starter"
PROMPT = "00_START_HERE.txt"


def build(output: Path, ref: str = "HEAD"):
    # A committed tree excludes local outputs, credentials and untracked proposals.
    archive = subprocess.check_output(["git", "archive", "--format=zip", ref], cwd=ROOT)
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        first = source.getinfo(PROMPT)
        source.getinfo("START_HERE.md")
        source.getinfo("pyproject.toml")
        source.getinfo("uv.lock")
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
            entry = copy(first)
            entry.filename = f"{BUNDLE}/{PROMPT}"
            bundle.writestr(entry, source.read(first))
            for original in source.infolist():
                if original.is_dir():
                    continue
                entry = copy(original)
                entry.filename = f"{BUNDLE}/engine/{original.filename}"
                bundle.writestr(entry, source.read(original))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ref", default="HEAD", help="Committed revision; defaults to HEAD")
    args = parser.parse_args()
    print(build(args.output, args.ref))


if __name__ == "__main__":
    main()
