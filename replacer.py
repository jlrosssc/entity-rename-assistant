"""
Replacer module for Entity Rename Assistant.
Performs safe, token-aware replacement of entity IDs across
YAML config files and .storage JSON blobs, with automatic backup.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from .scanner import EntityScanner, ScanResult, _entity_pattern

_LOGGER = logging.getLogger(__name__)

BACKUP_DIR_NAME = "entity_rename_assistant_backups"


def _backup_file(path: Path, backup_root: Path, config_dir: Path) -> Path:
    """
    Copy path into backup_root preserving the relative structure.
    Returns the backup path.
    """
    try:
        rel = path.relative_to(config_dir)
    except ValueError:
        rel = Path(path.name)
    dest = backup_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    # If backup already exists for this session, don't overwrite
    if not dest.exists():
        shutil.copy2(path, dest)
    return dest


class ReplaceResult:
    """Tracks what was changed during a replace operation."""

    def __init__(self, old_id: str, new_id: str, backup_dir: str) -> None:
        self.old_id = old_id
        self.new_id = new_id
        self.backup_dir = backup_dir
        self.files_changed: list[str] = []
        self.replacements_made: int = 0
        self.errors: list[str] = []

    def to_dict(self) -> dict:
        return {
            "old_entity_id": self.old_id,
            "new_entity_id": self.new_id,
            "backup_dir": self.backup_dir,
            "files_changed": self.files_changed,
            "replacements_made": self.replacements_made,
            "errors": self.errors,
        }


class EntityReplacer:
    """
    Replaces all occurrences of old_entity_id with new_entity_id
    in the HA config directory, backing up every touched file first.
    """

    def __init__(self, config_dir: str) -> None:
        self.config_dir = Path(config_dir)
        self.storage_dir = self.config_dir / ".storage"

    def replace(self, old_id: str, new_id: str) -> ReplaceResult:
        """
        Full replace:
        1. Scan to find all files that need changes
        2. Back them all up
        3. Apply replacements
        Returns a ReplaceResult summary.
        """
        # Timestamp-stamped backup folder
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = self.config_dir / BACKUP_DIR_NAME / f"{ts}_{old_id}_to_{new_id}"
        backup_dir.mkdir(parents=True, exist_ok=True)

        result = ReplaceResult(old_id, new_id, str(backup_dir))

        # Scan first so we only touch files that actually have references
        scanner = EntityScanner(str(self.config_dir))
        scan = scanner.scan(old_id)

        if scan.count == 0:
            _LOGGER.info("No references found for %s — nothing to do.", old_id)
            return result

        # Collect unique file paths from scan results
        files_to_process: set[Path] = set()
        for ref in scan.references:
            abs_path = self.config_dir / ref.file_path
            if abs_path.exists():
                files_to_process.add(abs_path)

        # Back up all files before touching any of them
        for path in files_to_process:
            try:
                _backup_file(path, backup_dir, self.config_dir)
                _LOGGER.debug("Backed up %s", path)
            except OSError as err:
                msg = f"Backup failed for {path}: {err}"
                _LOGGER.error(msg)
                result.errors.append(msg)

        if result.errors:
            # Abort if backup failed — safety first
            result.errors.insert(0, "Aborting replace: backup step failed.")
            return result

        # Now apply replacements
        pattern = _entity_pattern(old_id)

        for path in files_to_process:
            if ".storage" in path.parts:
                count = self._replace_in_storage(path, pattern, new_id, result)
            else:
                count = self._replace_in_yaml(path, pattern, new_id, result)

            if count > 0:
                result.files_changed.append(str(path.relative_to(self.config_dir)))
                result.replacements_made += count

        _LOGGER.info(
            "Replace complete: %d replacements in %d files. Backup at %s",
            result.replacements_made,
            len(result.files_changed),
            backup_dir,
        )
        return result

    # ------------------------------------------------------------------
    # YAML replacement
    # ------------------------------------------------------------------

    def _replace_in_yaml(
        self, path: Path, pattern: re.Pattern, new_id: str, result: ReplaceResult
    ) -> int:
        try:
            original = path.read_text(encoding="utf-8", errors="replace")
        except OSError as err:
            result.errors.append(f"Read error {path}: {err}")
            return 0

        new_text, count = pattern.subn(new_id, original)
        if count == 0:
            return 0

        try:
            path.write_text(new_text, encoding="utf-8")
        except OSError as err:
            result.errors.append(f"Write error {path}: {err}")
            return 0

        return count

    # ------------------------------------------------------------------
    # .storage JSON replacement
    # ------------------------------------------------------------------

    def _replace_in_storage(
        self, path: Path, pattern: re.Pattern, new_id: str, result: ReplaceResult
    ) -> int:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            data = json.loads(text)
        except (OSError, json.JSONDecodeError) as err:
            result.errors.append(f"Parse error {path}: {err}")
            return 0

        count_holder = [0]
        new_data = _replace_in_obj(data, pattern, new_id, count_holder)

        if count_holder[0] == 0:
            return 0

        try:
            path.write_text(
                json.dumps(new_data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as err:
            result.errors.append(f"Write error {path}: {err}")
            return 0

        return count_holder[0]


def _replace_in_obj(obj, pattern: re.Pattern, new_id: str, count: list[int]):
    """Recursively replace in a JSON-decoded object. Returns rebuilt object."""
    if isinstance(obj, str):
        new_val, n = pattern.subn(new_id, obj)
        count[0] += n
        return new_val
    if isinstance(obj, dict):
        return {k: _replace_in_obj(v, pattern, new_id, count) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_replace_in_obj(item, pattern, new_id, count) for item in obj]
    return obj
