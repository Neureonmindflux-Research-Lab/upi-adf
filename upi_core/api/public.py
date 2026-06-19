from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

import yaml

from ..telemetry.logger import get_logger
from ..telemetry.metrics import MetricsRegistry
from ..telemetry.tracing import Tracer
from ..utils.yaml_tools import load_yaml
from ..config_system.spec_schema import normalize_pipeline_spec
from ..config_system.interpolation import interpolate_config
from ..config_system.validation import validate_pipeline_config
from ..plugin_system.discovery_fs import discover_plugins_fs
from ..plugin_system.discovery_entrypoints import discover_plugins_entrypoints
from ..plugin_system.validator import validate_manifests
from ..plugin_system.registry import PluginRegistry
from ..plugin_system.enablelist import load_enablelist
from ..plugin_system.plugin_paths import add_plugin_roots
from ..plugin_system.loader import PluginLoader
from ..runtime.context import RuntimeContext
from ..runtime.engine import Engine
from ..runtime.events import EventBus
from ..runtime.exceptions import ConfigError, UpiError
from ..contracts.base import ServiceContainer
from ..storage.paths import RunPaths
from ..telemetry.audit import AuditLog
from .types import PipelineSpec


log = get_logger("upi.api")


YamlLike = Union[str, Path, Dict[str, Any]]
PluginDirs = Optional[Sequence[Union[str, Path]]]


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------


def _resolve_repo_root(repo_root: Optional[Union[str, Path]]) -> Path:
    """
    Resolve the repository root path.
    """
    if repo_root is None:
        return Path.cwd().resolve()
    return Path(repo_root).expanduser().resolve()


def _resolve_path(value: Union[str, Path], *, base: Path) -> Path:
    """
    Resolve a path relative to a base directory.
    """
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _resolve_plugin_dirs(plugin_dirs: PluginDirs, *, repo_root: Path) -> Optional[List[Path]]:
    """
    Resolve plugin directories relative to the repository root.
    """
    if not plugin_dirs:
        return None
    return [_resolve_path(p, base=repo_root) for p in plugin_dirs]


def _resolve_yaml_source(source: YamlLike, *, repo_root: Path) -> YamlLike:
    """
    Resolve a pipeline source as a dict, file path, or YAML string.
    """
    if isinstance(source, dict):
        return source

    if isinstance(source, Path):
        path = _resolve_path(source, base=repo_root)
        if not path.exists():
            raise ConfigError(f"YAML file not found: {path}")
        return path

    if isinstance(source, str):
        text = source.strip()

        # One-line values that look like files are treated as paths.
        # Multiline values are treated as YAML content.
        candidate = _resolve_path(text, base=repo_root) if text else None
        has_yaml_mapping_colon = ":" in text and not (len(text) >= 2 and text[1] == ":")
        has_path_separator = ("/" in text or "\\" in text) and not has_yaml_mapping_colon

        looks_like_path = (
            bool(text)
            and "\n" not in text
            and len(text) < 512
            and (
                text.endswith(".yml")
                or text.endswith(".yaml")
                or has_path_separator
                or (candidate is not None and candidate.exists())
            )
        )

        if looks_like_path:
            path = candidate if candidate is not None else _resolve_path(text, base=repo_root)
            if not path.exists():
                raise ConfigError(f"YAML file not found: {path}")
            return path

        return source

    raise ConfigError(f"Unsupported pipeline source type: {type(source)}")


def _safe_run_name(run_name: Optional[str], fallback: str) -> str:
    """
    Validate a run name and return a safe value.
    """
    if run_name is None:
        return fallback

    name = str(run_name).strip()
    if not name:
        return fallback

    path = Path(name)

    if (
        path.is_absolute()
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or ".." in path.parts
    ):
        raise ConfigError(f"Invalid run_name: {run_name!r}")

    return name


def _validation_services() -> ServiceContainer:
    """
    Create minimal services for strict validation.
    """
    return ServiceContainer(
        artifact_store=None,
        cache=None,
        logger=log,
        events=EventBus(),
        metrics=MetricsRegistry(),
        tracing=Tracer(),
    )


def _error_to_dict(error: Exception) -> Dict[str, Any]:
    """
    Convert an exception into a public error dictionary.
    """
    if isinstance(error, UpiError):
        return error.to_dict()

    return {
        "error": error.__class__.__name__,
        "message": str(error),
        "code": None,
        "details": {},
        "cause": None,
    }


def _strict_validate_pipeline(
    spec: PipelineSpec,
    registry: PluginRegistry,
    *,
    repo_root: Path,
) -> Dict[str, Any]:
    """
    Validate that each stage can load and instantiate its plugin.
    """
    loader = PluginLoader()

    context = RuntimeContext(
        run_id=f"validate-{uuid.uuid4().hex}",
        seed=spec.case.seed,
        device=spec.case.device,
        workdir=_resolve_path(spec.case.workdir, base=repo_root) if spec.case.workdir else repo_root,
        rundir=None,
        tags={
            "mode": "validate",
            "strict": "true",
        },
    )

    services = _validation_services()

    errors: List[Dict[str, Any]] = []
    stage_reports: List[Dict[str, Any]] = []

    for stage in spec.stages():
        stage_error: Optional[Dict[str, Any]] = None
        manifest_id: Optional[str] = None
        manifest_version: Optional[str] = None

        try:
            manifest = registry.select(
                plugin_type=stage.uses.plugin_type,
                capability=stage.uses.capability,
                prefer=stage.uses.prefer,
                version_constraint=stage.uses.version,
            )

            manifest_id = manifest.id
            manifest_version = manifest.version

            loaded = loader.load_class(manifest)

            loader.instantiate(
                loaded,
                config=stage.config,
                context=context,
                services=services,
            )

            ok = True

        except Exception as e:
            ok = False
            stage_error = {
                "stage": stage.name,
                "plugin_type": stage.uses.plugin_type,
                "capability": stage.uses.capability,
                "plugin_id": manifest_id,
                "plugin_version": manifest_version,
                "error": _error_to_dict(e),
            }
            errors.append(stage_error)

        stage_reports.append(
            {
                "stage": stage.name,
                "plugin_type": stage.uses.plugin_type,
                "capability": stage.uses.capability,
                "plugin_id": manifest_id,
                "plugin_version": manifest_version,
                "ok": ok,
                "error": stage_error,
            }
        )

    return {
        "ok": not errors,
        "errors": errors,
        "stages": stage_reports,
    }


def _write_yaml_file(path: Path, data: Dict[str, Any]) -> None:
    """
    Write a dictionary to a YAML file.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    except Exception as e:
        raise ConfigError(f"Failed to write YAML file: {path}", cause=e)


def _write_resolved_pipeline(spec: PipelineSpec, *, run_paths: RunPaths) -> Path:
    """
    Write the resolved pipeline used for the run.
    """
    path = run_paths.run_root / "pipeline.resolved.yml"
    _write_yaml_file(path, spec.model_dump(mode="json"))
    return path


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------


def load_pipeline(
    source: YamlLike,
    *,
    repo_root: Optional[Union[str, Path]] = None,
) -> PipelineSpec:
    """
    Load, normalize, interpolate, and validate a pipeline spec.
    """
    root = _resolve_repo_root(repo_root)
    resolved_source = _resolve_yaml_source(source, repo_root=root)

    raw = load_yaml(resolved_source)
    raw = normalize_pipeline_spec(raw)
    raw = interpolate_config(raw, base_path=root)

    return PipelineSpec.model_validate(raw)


def scan_plugins(
    *,
    repo_root: Optional[Union[str, Path]] = None,
    plugin_dirs: PluginDirs = None,
    fs: bool = True,
    entrypoints: bool = True,
) -> PluginRegistry:
    """
    Discover, validate, and register available plugins.
    """
    root = _resolve_repo_root(repo_root)
    resolved_plugin_dirs = _resolve_plugin_dirs(plugin_dirs, repo_root=root)

    add_plugin_roots(repo_root=root, extra_roots=resolved_plugin_dirs)

    enablelist = load_enablelist(root)

    manifests = []

    if fs:
        manifests.extend(discover_plugins_fs(root, plugin_dirs=resolved_plugin_dirs))

    if entrypoints:
        manifests.extend(discover_plugins_entrypoints())

    valid = validate_manifests(manifests, enablelist=enablelist)

    registry = PluginRegistry(enablelist=enablelist)
    registry.register_all(valid)

    return registry


def list_plugins(
    registry: Optional[PluginRegistry] = None,
    *,
    repo_root: Optional[Union[str, Path]] = None,
    plugin_dirs: PluginDirs = None,
    plugin_type: Optional[str] = None,
    fs: bool = True,
    entrypoints: bool = True,
) -> List[Dict[str, Any]]:
    """
    Return registered plugins as dictionaries.
    """
    root = _resolve_repo_root(repo_root)

    reg = registry or scan_plugins(
        repo_root=root,
        plugin_dirs=plugin_dirs,
        fs=fs,
        entrypoints=entrypoints,
    )

    items = reg.list(plugin_type=plugin_type)
    items = sorted(items, key=lambda m: (m.plugin_type, m.id, m.version))

    return [m.model_dump() for m in items]


def validate(
    pipeline: Union[PipelineSpec, YamlLike],
    *,
    registry: Optional[PluginRegistry] = None,
    repo_root: Optional[Union[str, Path]] = None,
    plugin_dirs: PluginDirs = None,
    fs: bool = True,
    entrypoints: bool = True,
    strict: bool = False,
    raise_on_error: bool = False,
) -> Dict[str, Any]:
    """
    Validate a pipeline against available plugins.
    """
    root = _resolve_repo_root(repo_root)

    spec = pipeline if isinstance(pipeline, PipelineSpec) else load_pipeline(pipeline, repo_root=root)

    reg = registry or scan_plugins(
        repo_root=root,
        plugin_dirs=plugin_dirs,
        fs=fs,
        entrypoints=entrypoints,
    )

    report = validate_pipeline_config(spec, reg, raise_on_error=False)
    report["strict"] = None

    if strict and report.get("ok", False):
        strict_report = _strict_validate_pipeline(spec, reg, repo_root=root)

        report["strict"] = strict_report
        report["ok"] = bool(report.get("ok")) and strict_report["ok"]

        if strict_report["errors"]:
            report.setdefault("errors", []).extend(strict_report["errors"])

    if raise_on_error and not report.get("ok", False):
        raise ConfigError("Pipeline configuration is invalid", details=report)

    return report


def explain(
    pipeline: Union[PipelineSpec, YamlLike],
    *,
    registry: Optional[PluginRegistry] = None,
    repo_root: Optional[Union[str, Path]] = None,
    plugin_dirs: PluginDirs = None,
    fs: bool = True,
    entrypoints: bool = True,
) -> Dict[str, Any]:
    """
    Explain plugin selection for each pipeline stage.
    """
    root = _resolve_repo_root(repo_root)

    spec = pipeline if isinstance(pipeline, PipelineSpec) else load_pipeline(pipeline, repo_root=root)

    reg = registry or scan_plugins(
        repo_root=root,
        plugin_dirs=plugin_dirs,
        fs=fs,
        entrypoints=entrypoints,
    )

    out: Dict[str, Any] = {}

    for stage in spec.stages():
        out[stage.name] = reg.explain_selection(
            plugin_type=stage.uses.plugin_type,
            capability=stage.uses.capability,
            prefer=stage.uses.prefer,
            version_constraint=stage.uses.version,
        )

    return out


def run(
    pipeline: Union[PipelineSpec, YamlLike],
    *,
    registry: Optional[PluginRegistry] = None,
    repo_root: Optional[Union[str, Path]] = None,
    plugin_dirs: PluginDirs = None,
    fs: bool = True,
    entrypoints: bool = True,
    scheduler: str = "local",
    limits: Optional[Dict[str, Any]] = None,
    run_name: Optional[str] = None,
    strict_validate: bool = False,
) -> Dict[str, Any]:
    """
    Validate and run a pipeline.
    """
    root = _resolve_repo_root(repo_root)
    resolved_plugin_dirs = _resolve_plugin_dirs(plugin_dirs, repo_root=root)

    add_plugin_roots(repo_root=root, extra_roots=resolved_plugin_dirs)

    spec = pipeline if isinstance(pipeline, PipelineSpec) else load_pipeline(pipeline, repo_root=root)

    reg = registry or scan_plugins(
        repo_root=root,
        plugin_dirs=resolved_plugin_dirs,
        fs=fs,
        entrypoints=entrypoints,
    )

    # Validate before execution.
    validation_report = validate(
        spec,
        registry=reg,
        repo_root=root,
        strict=strict_validate,
        raise_on_error=True,
    )

    run_id = uuid.uuid4().hex

    workdir = _resolve_path(spec.case.workdir, base=root) if spec.case.workdir else root
    run_dir_name = _safe_run_name(run_name, fallback=run_id)

    run_paths = RunPaths.from_repo_root(workdir, run_dir_name)
    run_paths.ensure()

    resolved_pipeline_path = _write_resolved_pipeline(spec, run_paths=run_paths)

    tags: Dict[str, str] = {
        "repo_root": str(root),
        "scheduler": scheduler,
    }

    if spec.case.tags:
        tags["case_tags"] = ",".join(str(t) for t in spec.case.tags)

    ctx = RuntimeContext(
        run_id=run_id,
        seed=spec.case.seed,
        device=spec.case.device,
        workdir=workdir,
        rundir=run_paths.run_root,
        tags=tags,
    )

    audit = AuditLog(run_paths.run_root / "audit.jsonl")

    engine = Engine(
        registry=reg,
        context=ctx,
        audit=audit,
        scheduler_name=scheduler,
        limits=limits or {},
    )

    try:
        audit.write(
            {
                "event": "run.submit",
                "run_id": run_id,
                "run_dir": str(run_paths.run_root),
                "scheduler": scheduler,
                "pipeline_resolved": str(resolved_pipeline_path),
                "validation": validation_report,
            }
        )

        result = engine.run(spec)

        if isinstance(result, dict):
            result.setdefault("run_id", run_id)
            result.setdefault("run_dir", str(run_paths.run_root))
            result.setdefault("pipeline_resolved", str(resolved_pipeline_path))
            result.setdefault("status", "ok")

        return result

    except Exception as e:
        audit.write(
            {
                "event": "run.error",
                "run_id": run_id,
                "error": repr(e),
            }
        )
        raise

    finally:
        audit.close()


def doctor(
    *,
    repo_root: Optional[Union[str, Path]] = None,
    plugin_dirs: PluginDirs = None,
    fs: bool = True,
    entrypoints: bool = True,
) -> Dict[str, Any]:
    """
    Return environment and plugin diagnostics.
    """
    from ..utils.platform import inspect_platform

    root = _resolve_repo_root(repo_root)
    resolved_plugin_dirs = _resolve_plugin_dirs(plugin_dirs, repo_root=root)

    enablelist = load_enablelist(root)

    info = inspect_platform()
    info["repo_root"] = str(root)
    info["enablelist_found"] = enablelist is not None
    info["plugin_dirs"] = [str(p) for p in resolved_plugin_dirs or []]

    try:
        registry = scan_plugins(
            repo_root=root,
            plugin_dirs=resolved_plugin_dirs,
            fs=fs,
            entrypoints=entrypoints,
        )

        info["plugins_ok"] = True
        info["plugin_count"] = len(registry.list())

    except Exception as e:
        info["plugins_ok"] = False
        info["plugin_count"] = 0
        info["plugin_error"] = _error_to_dict(e)

    return info