# Agent guidance

This repository distributes local Liquid strategy research. Keep simulation commands independent of live brokers, account credentials, notifications, and inference calls. Preserve original strategy semantics, causal timing, exact accounting, and explicit evidence limitations.

Use the checks in CONTRIBUTING.md. Changes to execution/sizing/exit behavior need relevant tests. Verify quickstart and installed-package behavior before releasing. Do not copy operational databases or private research archives into the distribution.

On the maintainer's machine, follow the user's external-development-storage policy. Portable output directories for community users do not authorize moving local runtimes or bypassing that storage policy during development.
