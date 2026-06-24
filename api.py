"""
REST API views for Entity Rename Assistant.
Registers two endpoints:
  GET/POST /api/entity_rename_assistant/scan
  POST     /api/entity_rename_assistant/apply
"""
from __future__ import annotations

import logging
from http import HTTPStatus

import voluptuous as vol
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .replacer import EntityReplacer
from .scanner import EntityScanner

_LOGGER = logging.getLogger(__name__)


def _as_bool(value, default: bool = True) -> bool:
    """Parse bool-ish REST values without treating "false" as true."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


class EntityScanView(HomeAssistantView):
    """Handle scan requests: find all references to an entity ID."""

    url = "/api/entity_rename_assistant/scan"
    name = "api:entity_rename_assistant:scan"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request):
        """POST body: {"entity_id": "switch.den"}"""
        try:
            data = await request.json()
        except Exception:
            return self.json_message("Invalid JSON body", HTTPStatus.BAD_REQUEST)

        entity_id = (data.get("entity_id") or "").strip().lower()
        if not entity_id or "." not in entity_id:
            return self.json_message(
                "entity_id is required (format: domain.object_id)",
                HTTPStatus.BAD_REQUEST,
            )

        config_dir = self.hass.config.config_dir
        scanner = EntityScanner(config_dir)

        try:
            result = await self.hass.async_add_executor_job(scanner.scan, entity_id)
        except Exception as err:
            _LOGGER.exception("Scan error for %s", entity_id)
            return self.json_message(f"Scan error: {err}", HTTPStatus.INTERNAL_SERVER_ERROR)

        # Build response
        references = [
            {
                "file_path": ref.file_path,
                "file_type": ref.file_type,
                "line_number": ref.line_number,
                "line_preview": ref.line_preview,
                "context": ref.context,
            }
            for ref in result.references
        ]

        # Also check entity registry
        reg = er.async_get(self.hass)
        in_registry = reg.async_get(entity_id) is not None

        return self.json(
            {
                "entity_id": entity_id,
                "total_references": result.count,
                "in_entity_registry": in_registry,
                "references": references,
            }
        )


class EntityApplyView(HomeAssistantView):
    """Handle apply requests: rename entity ID everywhere."""

    url = "/api/entity_rename_assistant/apply"
    name = "api:entity_rename_assistant:apply"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request):
        """
        POST body:
        {
          "old_entity_id": "switch.den",
          "new_entity_id": "switch.new_den",
          "rename_in_registry": true
        }
        """
        try:
            data = await request.json()
        except Exception:
            return self.json_message("Invalid JSON body", HTTPStatus.BAD_REQUEST)

        old_id = (data.get("old_entity_id") or "").strip().lower()
        new_id = (data.get("new_entity_id") or "").strip().lower()
        rename_registry = _as_bool(data.get("rename_in_registry"), True)

        if not old_id or "." not in old_id:
            return self.json_message("old_entity_id required (domain.object_id)", HTTPStatus.BAD_REQUEST)
        if not new_id or "." not in new_id:
            return self.json_message("new_entity_id required (domain.object_id)", HTTPStatus.BAD_REQUEST)
        if old_id == new_id:
            return self.json_message("old and new entity IDs are identical", HTTPStatus.BAD_REQUEST)

        config_dir = self.hass.config.config_dir
        replacer = EntityReplacer(config_dir)

        try:
            replace_result = await self.hass.async_add_executor_job(
                replacer.replace, old_id, new_id
            )
        except Exception as err:
            _LOGGER.exception("Replace error %s -> %s", old_id, new_id)
            return self.json_message(f"Replace error: {err}", HTTPStatus.INTERNAL_SERVER_ERROR)

        # Optionally rename in entity registry
        registry_result = None
        if rename_registry:
            registry_result = await self._rename_in_registry(old_id, new_id)

        response = replace_result.to_dict()
        response["registry_rename"] = registry_result
        return self.json(response)

    async def _rename_in_registry(self, old_id: str, new_id: str) -> dict:
        """Rename in the entity registry if the old entity exists."""
        reg = er.async_get(self.hass)
        entry = reg.async_get(old_id)
        if not entry:
            return {"success": False, "reason": "Entity not found in registry"}
        try:
            reg.async_update_entity(entry.entity_id, new_entity_id=new_id)
            return {"success": True}
        except Exception as err:
            _LOGGER.error("Registry rename failed: %s", err)
            return {"success": False, "reason": str(err)}
