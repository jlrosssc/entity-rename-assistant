"""
Entity Rename Assistant — Home Assistant Custom Integration.

Scans all YAML config files and .storage JSON blobs for references
to an entity ID and replaces them throughout, with automatic backup.
"""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.components import frontend, persistent_notification
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv

from .api import EntityApplyView, EntityScanView
from .replacer import EntityReplacer
from .scanner import EntityScanner

_LOGGER = logging.getLogger(__name__)

DOMAIN = "entity_rename_assistant"

# Service schemas
SCAN_SCHEMA = vol.Schema({
    vol.Required("entity_id"): cv.entity_id,
})

APPLY_SCHEMA = vol.Schema({
    vol.Required("old_entity_id"): cv.entity_id,
    vol.Required("new_entity_id"): cv.string,
    vol.Optional("rename_in_registry", default=True): cv.boolean,
})


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Entity Rename Assistant integration."""
    _LOGGER.info("Entity Rename Assistant: setting up")

    # Register REST API views
    hass.http.register_view(EntityScanView(hass))
    hass.http.register_view(EntityApplyView(hass))

    # Register static files for the frontend panel
    await hass.http.async_register_static_paths([
        StaticPathConfig(
            url_path="/entity_rename_assistant_panel",
            path=__file__.replace("__init__.py", "frontend"),
            cache_headers=False,
        )
    ])

    # Register the sidebar panel
    frontend.async_register_built_in_panel(
        hass,
        component_name="iframe",
        sidebar_title="Rename Entity",
        sidebar_icon="mdi:rename-box",
        frontend_url_path="entity-rename-assistant",
        config={
            "url": "/entity_rename_assistant_panel/panel.html?v=20260624-111735",
        },
        require_admin=True,
    )

    # Register HA services (callable from automations / Developer Tools)
    async def handle_scan(call: ServiceCall) -> None:
        entity_id = call.data["entity_id"]
        scanner = EntityScanner(hass.config.config_dir)
        result = await hass.async_add_executor_job(scanner.scan, entity_id)
        summary = f"Found {result.count} reference(s) to {entity_id}:\n"
        for file_path, refs in result.by_file().items():
            lines = ", ".join(
                str(r.line_number) for r in refs if r.line_number
            )
            summary += f"  {file_path}"
            if lines:
                summary += f" (lines {lines})"
            summary += "\n"
        persistent_notification.async_create(
            hass,
            summary,
            title=f"Rename Assistant: Scan Results for {entity_id}",
            notification_id=f"era_scan_{entity_id.replace('.', '_')}",
        )
        _LOGGER.info(summary)

    async def handle_apply(call: ServiceCall) -> None:
        old_id = call.data["old_entity_id"]
        new_id = call.data["new_entity_id"]
        rename_registry = call.data.get("rename_in_registry", True)
        replacer = EntityReplacer(hass.config.config_dir)
        result = await hass.async_add_executor_job(replacer.replace, old_id, new_id)

        if rename_registry:
            from homeassistant.helpers import entity_registry as er
            reg = er.async_get(hass)
            entry = reg.async_get(old_id)
            if entry:
                try:
                    reg.async_update_entity(entry.entity_id, new_entity_id=new_id)
                    _LOGGER.info("Entity registry updated: %s -> %s", old_id, new_id)
                except Exception as err:
                    _LOGGER.error("Registry rename failed: %s", err)

        msg = (
            f"Renamed {old_id} → {new_id}\n"
            f"Files changed: {len(result.files_changed)}\n"
            f"Replacements made: {result.replacements_made}\n"
            f"Backup: {result.backup_dir}\n"
        )
        if result.errors:
            msg += f"Errors: {'; '.join(result.errors)}\n"
        persistent_notification.async_create(
            hass,
            msg,
            title=f"Rename Assistant: Applied {old_id} → {new_id}",
            notification_id=f"era_apply_{old_id.replace('.', '_')}",
        )
        _LOGGER.info(msg)

    hass.services.async_register(DOMAIN, "scan", handle_scan, schema=SCAN_SCHEMA)
    hass.services.async_register(DOMAIN, "apply", handle_apply, schema=APPLY_SCHEMA)

    _LOGGER.info("Entity Rename Assistant: setup complete")
    return True
