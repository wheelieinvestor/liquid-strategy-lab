"""Inert envelope helpers for captured research observations."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def envelope(**values: object) -> dict:
    return {
        "schema_version": "liquid-desk-v1",
        "observed_at": utcnow().isoformat(),
        "execution_mode": "DISABLED",
        "broker_writes_enabled": False,
        "execution_authorized": False,
        "broker_readiness": "NO_GO",
        **values,
    }
