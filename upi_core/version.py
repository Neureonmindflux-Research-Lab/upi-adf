from __future__ import annotations

from dataclasses import dataclass

# Core semantic version (plugin compat uses this)
__version__ = "0.6.0"

# Public API compatibility level (bump only when public API breaks)
API_LEVEL = 1


@dataclass(frozen=True)
class CoreVersion:
    version: str
    api_level: int


CORE_VERSION = CoreVersion(__version__, API_LEVEL)
