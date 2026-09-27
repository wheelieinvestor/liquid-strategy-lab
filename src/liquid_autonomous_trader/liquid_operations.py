"""Shared exception type; the live operation journal is not distributed."""


class LiquidOperationBlocked(RuntimeError):
    pass
