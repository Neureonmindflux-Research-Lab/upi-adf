"""
Public package exports for UPI core.
"""

from .version import __version__, API_LEVEL, CORE_VERSION

from .api import (
    CaseSpec,
    UseSelector,
    StageSpec,
    PipelineSpec,
    load_pipeline,
    scan_plugins,
    list_plugins,
    validate,
    explain,
    run,
    doctor,
)

__all__ = [
    "__version__",
    "API_LEVEL",
    "CORE_VERSION",
    "CaseSpec",
    "UseSelector",
    "StageSpec",
    "PipelineSpec",
    "load_pipeline",
    "scan_plugins",
    "list_plugins",
    "validate",
    "explain",
    "run",
    "doctor",
]