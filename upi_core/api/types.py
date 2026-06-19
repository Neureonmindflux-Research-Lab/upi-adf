from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


_STAGE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


class CaseSpec(BaseModel):
    """
    Global pipeline run settings.
    """
    model_config = ConfigDict(extra="forbid")

    seed: int = Field(default=0, ge=0)
    device: str = "cpu"
    tags: List[str] = Field(default_factory=list)
    workdir: Optional[Path] = None

    @field_validator("device")
    @classmethod
    def _validate_device(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("case.device must not be empty")
        return value

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, value: List[str]) -> List[str]:
        clean: List[str] = []

        for tag in value:
            if not isinstance(tag, str):
                raise ValueError("case.tags must contain only strings")

            tag = tag.strip()
            if tag:
                clean.append(tag)

        return clean

    @field_validator("workdir", mode="before")
    @classmethod
    def _validate_workdir(cls, value: Any) -> Any:
        if value == "":
            return None
        return value


class UseSelector(BaseModel):
    """
    Plugin selection settings.
    """
    model_config = ConfigDict(extra="forbid")

    plugin_type: str
    capability: Optional[str] = None
    prefer: Optional[str] = None
    version: Optional[str] = None

    @field_validator("plugin_type")
    @classmethod
    def _validate_plugin_type(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("uses.plugin_type must not be empty")
        return value

    @field_validator("capability", "prefer", "version", mode="before")
    @classmethod
    def _clean_optional_string(cls, value: Any) -> Any:
        if value is None:
            return None

        if isinstance(value, str):
            value = value.strip()
            return value or None

        return value


class StageSpec(BaseModel):
    """
    Pipeline stage definition.
    """
    model_config = ConfigDict(extra="forbid")

    name: str
    uses: UseSelector
    config: Dict[str, Any] = Field(default_factory=dict)
    outputs: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        value = value.strip()

        if not value:
            raise ValueError("stage.name must not be empty")

        if not _STAGE_NAME_RE.match(value):
            raise ValueError(
                "stage.name must start with a letter or underscore and contain only "
                "letters, numbers, underscores, dots, or dashes"
            )

        return value


class PipelineSpec(BaseModel):
    """
    Root pipeline specification.
    """
    model_config = ConfigDict(extra="forbid")

    case: CaseSpec = Field(default_factory=CaseSpec)
    pipeline: Dict[str, Any]

    @field_validator("pipeline")
    @classmethod
    def _validate_pipeline(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        stages = value.get("stages")

        if not isinstance(stages, list) or not stages:
            raise ValueError("pipeline.stages must be a non-empty list")

        validated_stages = [StageSpec.model_validate(stage) for stage in stages]
        stage_names = [stage.name for stage in validated_stages]

        seen = set()
        duplicates = []

        for name in stage_names:
            if name in seen and name not in duplicates:
                duplicates.append(name)
            seen.add(name)

        if duplicates:
            raise ValueError(f"pipeline.stages contains duplicate stage names: {duplicates}")

        normalized = dict(value)
        normalized["stages"] = [
            stage.model_dump(mode="python") for stage in validated_stages
        ]

        return normalized

    def stages(self) -> List[StageSpec]:
        """
        Return validated pipeline stages.
        """
        return [StageSpec.model_validate(stage) for stage in self.pipeline["stages"]]

    def stage_names(self) -> List[str]:
        """
        Return pipeline stage names.
        """
        return [stage.name for stage in self.stages()]