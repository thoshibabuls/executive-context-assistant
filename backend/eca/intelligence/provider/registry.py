"""Role registry from ``config/models.yaml`` (AI_PIPELINE.md §2-§3, BACKEND_DESIGN.md §5.5).

Code references roles only. Unknown and disabled roles fail before any provider call.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from eca.intelligence.provider.types import ConfigError, RoleDisabled, Thinking, UnknownRole

_INVENTORY_ID = re.compile(r"^AI-\d{2}$")


class RoleSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = ""
    inventory_id: str
    model: str
    fallback: str | None = None
    thinking: Thinking | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_output_tokens: int | None = Field(default=None, ge=1)
    output_dimensionality: int | None = Field(default=None, ge=1)
    enabled: bool = True

    @field_validator("inventory_id")
    @classmethod
    def _inventory(cls, v: str) -> str:
        if not _INVENTORY_ID.fullmatch(v):
            raise ValueError("inventory_id must look like AI-01")
        return v

    @property
    def is_embedding(self) -> bool:
        return self.output_dimensionality is not None


class _RegistryFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    verified: bool
    roles: dict[str, RoleSpec]

    @model_validator(mode="after")
    def _unique_inventory_ids(self) -> _RegistryFile:
        ids = [r.inventory_id for r in self.roles.values()]
        if len(ids) != len(set(ids)):
            raise ValueError("inventory_id values must be unique")
        return self


class RoleRegistry:
    def __init__(self, roles: Mapping[str, RoleSpec], *, verified: bool) -> None:
        self._roles = {name: spec.model_copy(update={"name": name}) for name, spec in roles.items()}
        self.verified = verified

    @classmethod
    def from_file(cls, path: Path) -> RoleRegistry:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            parsed = _RegistryFile.model_validate(raw)
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise ConfigError(f"Invalid role registry {path.name}: {exc}") from exc
        return cls(parsed.roles, verified=parsed.verified)

    def role(self, name: str) -> RoleSpec:
        """The enabled role ``name``; raises ``UnknownRole`` or ``RoleDisabled``."""
        spec = self._roles.get(name)
        if spec is None:
            raise UnknownRole(f"Unknown AI role {name!r}")
        if not spec.enabled:
            raise RoleDisabled(f"AI role {name!r} ({spec.inventory_id}) is disabled")
        return spec

    def roles(self) -> list[RoleSpec]:
        return [self._roles[n] for n in sorted(self._roles)]

    def model_ids(self) -> set[str]:
        """Every model ID the registry can call (primary and fallback, enabled or not)."""
        ids = {r.model for r in self._roles.values()}
        ids |= {r.fallback for r in self._roles.values() if r.fallback}
        return ids
