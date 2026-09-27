"""Exception types only. No broker, OAuth, or MCP transport is distributed."""


class ComputerMCPError(RuntimeError):
    pass


class PostOutcomeUnknown(ComputerMCPError):
    pass


class SessionRejected(ComputerMCPError):
    pass
