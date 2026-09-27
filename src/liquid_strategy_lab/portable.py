"""Portable measurements and immutable source/dependency fingerprints."""

import hashlib
import importlib.metadata
import json
from pathlib import Path

import psutil


def rss_bytes():
    info = psutil.Process().memory_info()
    return getattr(info, "peak_wset", info.rss)


def source_hash():
    root = Path(__file__).resolve().parents[1]
    files = [
        *root.joinpath("liquid_autonomous_trader").rglob("*.py"),
        *root.joinpath("liquid_strategy_lab").rglob("*.py"),
    ]
    values = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(files)
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def dependency_hash():
    names = ("pydantic", "exchange-calendars", "typesafe-sdk", "psutil", "websockets", "tzdata")
    values = {name: importlib.metadata.version(name) for name in names}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
