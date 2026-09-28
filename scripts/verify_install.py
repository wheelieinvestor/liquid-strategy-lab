"""Exercise ZIP-style and installed-wheel workflows without Git or source-path imports."""

import argparse
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def call(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True)


def verify(folder):
    value = json.loads((folder / "comparison.json").read_text(encoding="utf-8"))
    assert value["dataset"]["kind"] == "synthetic"
    assert all(v["closed_trades"] > 0 for v in value["metrics"].values())
    assert (folder / "report.html").stat().st_size > 1000
    return value["run_hashes"]


def verify_sandbox(folder):
    value = json.loads((folder / "comparison.json").read_text(encoding="utf-8"))
    assert value["dataset"]["costs"] == "excluded"
    assert len(value["run_hashes"]) == 5
    assert (folder / "report.html").stat().st_size > 1000
    for metric in value["metrics"].values():
        assert float(metric["fees"]) == float(metric["funding"]) == 0
        assert float(metric["slippage_informational_already_in_prices"]) == 0
    return value["run_hashes"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fresh-install-", dir=args.work_dir.resolve()) as temp:
        base = Path(temp)
        archive = base / "starter.zip"
        call(
            sys.executable,
            str(ROOT / "scripts/build_starter.py"),
            "--output",
            str(archive),
            cwd=ROOT,
        )
        with zipfile.ZipFile(archive) as bundle:
            assert bundle.namelist()[0] == "Liquid Backtesting Starter/00_START_HERE.txt"
            bundle.extractall(base)
        starter = base / "Liquid Backtesting Starter"
        assert {p.name for p in starter.iterdir()} == {"00_START_HERE.txt", "engine"}
        checkout = starter / "engine"
        assert (starter / "00_START_HERE.txt").read_bytes() == (
            checkout / "00_START_HERE.txt"
        ).read_bytes()
        assert not (checkout / ".git").exists()
        call("uv", "sync", "--frozen", cwd=checkout)
        call("uv", "run", "--frozen", "liquid-lab", "sandbox", cwd=checkout)
        simple = verify_sandbox(checkout / "outputs/sandbox")
        call(
            "uv",
            "run",
            "--frozen",
            "liquid-lab",
            "sandbox",
            "--config",
            "examples/custom-strategy.json",
            "--output",
            "outputs/custom",
            cwd=checkout,
        )
        verify_sandbox(checkout / "outputs/custom")
        call(
            "uv",
            "run",
            "--frozen",
            "python",
            "scripts/verify_calculations.py",
            "outputs/sandbox",
            "--output",
            "outputs/sandbox-arithmetic",
            cwd=checkout,
        )
        call(
            "uv", "run", "--frozen", "liquid-lab", "demo", "--output", "outputs/fresh", cwd=checkout
        )
        first = verify(checkout / "outputs/fresh")
        call(
            "uv",
            "run",
            "--frozen",
            "liquid-lab",
            "portfolio",
            "--output",
            "outputs/portfolio",
            cwd=checkout,
        )
        call(
            "uv",
            "run",
            "--frozen",
            "liquid-lab",
            "stress",
            "--family",
            "execution",
            "--output",
            "outputs/stress",
            cwd=checkout,
        )
        call("uv", "build", "--out-dir", str(base / "packages"), cwd=checkout)
        wheel = next((base / "packages").glob("*.whl"))
        environment = base / "installed-env"
        call("uv", "venv", "--python", "3.12", str(environment), cwd=base)
        python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        requirements = base / "requirements.txt"
        call(
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--output-file",
            str(requirements),
            cwd=checkout,
        )
        call(
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--require-hashes",
            "-r",
            str(requirements),
            cwd=base,
        )
        call("uv", "pip", "install", "--python", str(python), "--no-deps", str(wheel), cwd=base)
        elsewhere = base / "elsewhere"
        elsewhere.mkdir()
        call(
            str(python),
            "-I",
            "-m",
            "liquid_strategy_lab.cli",
            "sandbox",
            "--output",
            "sandbox",
            cwd=elsewhere,
        )
        assert verify_sandbox(elsewhere / "sandbox") == simple
        call(
            str(python),
            "-I",
            "-m",
            "liquid_strategy_lab.cli",
            "demo",
            "--output",
            "demo",
            cwd=elsewhere,
        )
        assert verify(elsewhere / "demo") == first
        call(
            str(python),
            "-I",
            "-m",
            "liquid_strategy_lab.cli",
            "portfolio",
            "--output",
            "portfolio",
            cwd=elsewhere,
        )
        print(
            "PASS: fresh ZIP install, all community commands, installed wheel, "
            "custom strategy, identical sandbox and demo economics"
        )
        print("Demo run hashes: " + json.dumps(first, sort_keys=True))
        receipt = args.work_dir / ("install-" + sys.platform + ".json")
        receipt.write_text(
            json.dumps(
                {
                    "status": "passed",
                    "platform": sys.platform,
                    "run_hashes": first,
                    "sandbox_hashes": simple,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
