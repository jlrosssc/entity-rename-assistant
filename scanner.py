"""
Scanner module for Entity Rename Assistant.
Finds all references to an entity ID across YAML config files
and .storage JSON blobs.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator

_LOGGER = logging.getLogger(__name__)

# YAML files to scan (relative to config root)
YAML_SCAN_PATTERNS = [
    "automations.yaml",
    "scripts.yaml",
    "scenes.yaml",
    "groups.yaml",
    "configuration.yaml",
    "customize.yaml",
    "templates.yaml",
    "input_boolean.yaml",
    "input_select.yaml",
    "input_number.yaml",
    "input_text.yaml",
    "switches.yaml",
    "sensors.yaml",
    "binary_sensors.yaml",
    "lights.yaml",
    "covers.yaml",
    "fans.yaml",
    "climate.yaml",
    "notify.yaml",
]

# Glob patterns for recursive package scanning
YAML_GLOB_PATTERNS = [
    "packages/**/*.yaml",
    "packages/*.yaml",
]

# .storage keys to scan (filename prefix under .storage/)
STORAGE_SCAN_PREFIXES = [
    "lovelace",          # default dashboard
    "lovelace.",         # named dashboards (lovelace.mobile, etc.)
    "automation.",       # UI automations
    "script.",           # UI scripts
    "scene.",            # UI scenes
    "core.entity_registry",
    "frontend.themes",
]


@dataclass
class Reference:
    """A single found reference to an entity ID."""
    file_path: str          # relative to config root
    file_type: str          # "yaml" or "storage"
    line_number: int | None  # None for JSON storage blobs
    line_preview: str       # snippet of the matching line/value
    context: str | None     # e.g. dashboard name for lovelace


@dataclass
class ScanResult:
    """Complete results of a scan for one entity ID."""
    entity_id: str
    references: list[Reference] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.references)

    def by_file(self) -> dict[str, list[Reference]]:
        result: dict[str, list[Reference]] = {}
        for ref in self.references:
            result.setdefault(ref.file_path, []).append(ref)
        return result


def _entity_pattern(entity_id: str) -> re.Pattern:
    """
    Build a regex that matches entity_id only when surrounded by
    non-entity-id characters (prevents partial matches like
    switch.den matching inside switch.den_light).
    Entity ID valid chars: [a-z0-9_.]
    """
    escaped = re.escape(entity_id)
    return re.compile(
        r'(?<![a-zA-Z0-9_])' + escaped + r'(?![a-zA-Z0-9_])'
    )


def _scan_text_lines(
    text: str,
    entity_id: str,
    file_path: str,
    file_type: str,
    context: str | None = None,
) -> Generator[Reference, None, None]:
    """Yield a Reference for each line containing entity_id."""
    pattern = _entity_pattern(entity_id)
    for line_num, line in enumerate(text.splitlines(), start=1):
        if pattern.search(line):
            yield Reference(
                file_path=file_path,
                file_type=file_type,
                line_number=line_num,
                line_preview=line.strip()[:120],
                context=context,
            )


def _scan_json_value(
    obj,
    entity_id: str,
    file_path: str,
    context: str | None,
    results: list[Reference],
    _depth: int = 0,
) -> None:
    """Recursively walk a JSON object looking for entity_id in string values."""
    if _depth > 50:
        return
    pattern = _entity_pattern(entity_id)
    if isinstance(obj, str):
        if pattern.search(obj):
            results.append(Reference(
                file_path=file_path,
                file_type="storage",
                line_number=None,
                line_preview=obj[:120],
                context=context,
            ))
    elif isinstance(obj, dict):
        for v in obj.values():
            _scan_json_value(v, entity_id, file_path, context, results, _depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _scan_json_value(item, entity_id, file_path, context, results, _depth + 1)


class EntityScanner:
    """Scans the HA config directory for references to an entity ID."""

    def __init__(self, config_dir: str) -> None:
        self.config_dir = Path(config_dir)
        self.storage_dir = self.config_dir / ".storage"

    def scan(self, entity_id: str) -> ScanResult:
        result = ScanResult(entity_id=entity_id)
        self._scan_yaml_files(entity_id, result)
        self._scan_storage_files(entity_id, result)
        return result

    # ------------------------------------------------------------------
    # YAML scanning
    # ------------------------------------------------------------------

    def _scan_yaml_files(self, entity_id: str, result: ScanResult) -> None:
        scanned: set[Path] = set()

        # Explicit top-level files
        for filename in YAML_SCAN_PATTERNS:
            path = self.config_dir / filename
            if path.exists() and path not in scanned:
                self._scan_yaml_file(path, entity_id, result)
                scanned.add(path)

        # Recursive package globs
        for glob in YAML_GLOB_PATTERNS:
            for path in self.config_dir.glob(glob):
                if path.suffix in (".yaml", ".yml") and path not in scanned:
                    self._scan_yaml_file(path, entity_id, result)
                    scanned.add(path)

        # Also walk any directory referenced by !include_dir_* at top level
        # by scanning ALL yaml under config root (avoids needing to parse YAML)
        for path in self.config_dir.rglob("*.yaml"):
            if path not in scanned and ".storage" not in path.parts:
                self._scan_yaml_file(path, entity_id, result)
                scanned.add(path)
        for path in self.config_dir.rglob("*.yml"):
            if path not in scanned and ".storage" not in path.parts:
                self._scan_yaml_file(path, entity_id, result)
                scanned.add(path)

    def _scan_yaml_file(
        self, path: Path, entity_id: str, result: ScanResult
    ) -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as err:
            _LOGGER.warning("Could not read %s: %s", path, err)
            return
        rel = str(path.relative_to(self.config_dir))
        for ref in _scan_text_lines(text, entity_id, rel, "yaml"):
            result.references.append(ref)

    # ------------------------------------------------------------------
    # .storage scanning
    # ------------------------------------------------------------------

    def _scan_storage_files(self, entity_id: str, result: ScanResult) -> None:
        if not self.storage_dir.exists():
            return
        for storage_file in self.storage_dir.iterdir():
            if not storage_file.is_file():
                continue
            name = storage_file.name
            if any(
                name == prefix or name.startswith(prefix)
                for prefix in STORAGE_SCAN_PREFIXES
            ):
                self._scan_storage_file(storage_file, entity_id, result)

    def _scan_storage_file(
        self, path: Path, entity_id: str, result: ScanResult
    ) -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            data = json.loads(text)
        except (OSError, json.JSONDecodeError) as err:
            _LOGGER.warning("Could not parse storage file %s: %s", path, err)
            return

        rel = str(path.relative_to(self.config_dir))
        context = self._storage_context(path.name, data)
        refs: list[Reference] = []
        _scan_json_value(data, entity_id, rel, context, refs)
        result.references.extend(refs)

    @staticmethod
    def _storage_context(filename: str, data: dict) -> str | None:
        """Extract a human-readable context label from the storage blob."""
        try:
            if filename.startswith("lovelace"):
                title = data.get("data", {}).get("config", {}).get("title")
                return f"Dashboard: {title}" if title else "Dashboard"
            if filename == "core.entity_registry":
                return "Entity Registry"
        except Exception:
            pass
        return filename
