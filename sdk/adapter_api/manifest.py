"""Adapter manifest schema and loader.

Every adapter ships an adapter.yaml manifest describing its capabilities,
supported transports, device families, and connection templates.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class ManifestSupports(BaseModel):
    discovery: list[str] = Field(default_factory=list)
    auth: list[str] = Field(default_factory=list)
    inventory: bool = True
    read: bool = True
    write: bool = True
    subscribe: bool = False
    batch_commands: bool = False
    scenes: str | bool = False
    schedules: str | bool = False
    optimization_hints: bool = False


class ManifestConnectionTemplate(BaseModel):
    id: str
    display_name: str = ""
    required_fields: list[str] = Field(default_factory=list)
    optional_fields: list[str] = Field(default_factory=list)
    secret_fields: list[str] = Field(default_factory=list)
    files_to_upload: list[str] = Field(default_factory=list)
    discovery_methods: list[str] = Field(default_factory=list)
    physical_actions: list[str] = Field(default_factory=list)


class ManifestCompatibility(BaseModel):
    firmware_ranges: list[str] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)
    safety_notes: list[str] = Field(default_factory=list)
    transport_fallbacks: list[str] = Field(default_factory=list)


class AdapterManifest(BaseModel):
    adapter_api: str = "1.0"
    id: str
    display_name: str
    vendor: str = ""
    version: str = "0.1.0"
    adapter_class: str = "direct_device"
    runtime: str = "python"
    supports: ManifestSupports = Field(default_factory=ManifestSupports)
    transports: list[str] = Field(default_factory=list)
    device_families: list[str] = Field(default_factory=list)
    capability_families: list[str] = Field(default_factory=list)
    connection_templates: list[ManifestConnectionTemplate] = Field(default_factory=list)
    compatibility: ManifestCompatibility = Field(default_factory=ManifestCompatibility)


def load_manifest(path: str | Path) -> AdapterManifest:
    """Load and validate an adapter manifest from a YAML file."""
    path = Path(path)
    with path.open() as f:
        raw = yaml.safe_load(f)
    return AdapterManifest(**raw)


def load_manifest_dict(data: dict[str, Any]) -> AdapterManifest:
    """Load and validate an adapter manifest from a dictionary."""
    return AdapterManifest(**data)
