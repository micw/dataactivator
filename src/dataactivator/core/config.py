"""Configuration schema and storage backends.

The pydantic models are the single source of truth for configuration
structure, independent of where the data comes from. ``ConfigBackend``
is the storage abstraction: today a YAML file, later e.g. a per-user
database record behind a web UI.

Schema (see config.example.yaml):

    storage:
      type: file
      folder: data

    providers:
      - name: vw
        type: volkswagen-data-act-portal
        email: user@example.com
        password: s3cr3t!

Provider entries are flat: ``name`` and ``type`` are core fields, all
remaining keys are provider-specific and validated by the provider
implementation selected via ``type``.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ConfigError(Exception):
    """Configuration could not be loaded or is invalid."""


class ProviderConfig(BaseModel):
    """One configured data source.

    ``name`` is a stable, user-chosen identifier (several entries may
    share the same ``type``, e.g. two VW logins in one household).
    ``type`` selects the provider implementation. All other keys are
    kept as-is and validated by that implementation.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    type: str

    @property
    def settings(self) -> dict[str, Any]:
        """Provider-specific keys (everything except name/type)."""
        return dict(self.model_extra or {})


class StorageConfig(BaseModel):
    """Where fetched data ends up. ``type`` selects the backend."""

    model_config = ConfigDict(extra="allow")

    type: str = "file"
    folder: Path = Path("data")

    @property
    def settings(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class WebConfig(BaseModel):
    """Web server (served in ``serve`` mode).

    The public statistics pages are always on. The internal vehicle-data
    pages are only active when ``data_password`` is set — they show real
    telemetry (and the VIN) and are protected by HTTP Basic auth.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    data_username: str = "admin"
    data_password: str = ""

    @property
    def data_enabled(self) -> bool:
        return bool(self.data_password)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage: StorageConfig = Field(default_factory=StorageConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    providers: list[ProviderConfig] = Field(default_factory=list)

    def provider(self, name: str) -> ProviderConfig:
        for prov in self.providers:
            if prov.name == name:
                return prov
        raise ConfigError(f"no provider with name {name!r}")


class ConfigBackend(Protocol):
    def load(self) -> AppConfig: ...

    def save(self, config: AppConfig) -> None: ...


DEFAULT_CONFIG_PATH = Path("config.yaml")


class YamlConfigBackend:
    """Stores the whole AppConfig as one YAML file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or DEFAULT_CONFIG_PATH).expanduser()

    def load(self) -> AppConfig:
        if not self.path.exists():
            raise ConfigError(
                f"config file not found: {self.path} "
                f"(create it or pass --config)"
            )
        self._warn_if_world_readable()
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {self.path}: {exc}") from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{self.path}: top level must be a mapping")
        try:
            return AppConfig.model_validate(raw)
        except ValueError as exc:
            raise ConfigError(f"{self.path}: {exc}") from exc

    def save(self, config: AppConfig) -> None:
        if self.path.parent != Path("."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        data = config.model_dump(mode="json", exclude_defaults=True)
        tmp = self.path.with_suffix(".yaml.tmp")
        tmp.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def _warn_if_world_readable(self) -> None:
        if os.name != "posix":
            return
        mode = self.path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            print(
                f"warning: {self.path} is readable by other users; "
                f"it contains credentials, consider: chmod 600 {self.path}",
                file=sys.stderr,
            )
