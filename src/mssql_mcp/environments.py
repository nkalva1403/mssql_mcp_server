"""Named SQL Server environments loaded from a JSON registry file.

Lets a single MCP server instance pivot between known servers (dev /
staging / prod / region-by-region) at runtime — but only those servers,
so an LLM can't be coerced into pointing the connection at arbitrary
internal hosts."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Environment(BaseModel):
    """One named SQL Server target."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        min_length=1,
        description="Short identifier used by switch_environment (e.g. 'dev').",
    )
    description: str | None = None
    server: str = Field(..., min_length=1)
    port: int = Field(default=1433, gt=0, lt=65536)
    default_database: str | None = Field(
        default=None,
        description="Database to use when switching to this env if none given.",
    )


class EnvironmentRegistry(BaseModel):
    """The full list of environments + which one to start in."""

    model_config = ConfigDict(extra="forbid")

    default: str | None = Field(
        default=None,
        description=(
            "Name of the environment to activate at startup. Must "
            "match one of environments[].name if set."
        ),
    )
    environments: list[Environment] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_unique_and_default(self) -> EnvironmentRegistry:
        seen: set[str] = set()
        for env in self.environments:
            if env.name in seen:
                raise ValueError(
                    f"duplicate environment name: {env.name!r}"
                )
            seen.add(env.name)
        if self.default is not None and self.default not in seen:
            raise ValueError(
                f"default environment {self.default!r} is not defined; "
                f"known: {sorted(seen)}"
            )
        return self

    def get(self, name: str) -> Environment:
        for env in self.environments:
            if env.name == name:
                return env
        names = [e.name for e in self.environments]
        raise KeyError(
            f"environment {name!r} not found; known: {names}"
        )

    def names(self) -> list[str]:
        return [e.name for e in self.environments]

    @classmethod
    def from_path(cls, path: Path) -> EnvironmentRegistry:
        """Load a registry from a JSON file."""
        if not path.exists():
            raise FileNotFoundError(
                f"environments file not found: {path}"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"environments file is not valid JSON: {path} ({exc})"
            ) from exc
        return cls.model_validate(data)


__all__ = ["Environment", "EnvironmentRegistry"]
