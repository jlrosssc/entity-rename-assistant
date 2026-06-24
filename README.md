# Entity Rename Assistant

Private Home Assistant custom integration that scans YAML and selected `.storage` files for entity ID references, then applies a rename with backups.

## Install

Copy this folder to:

```text
config/custom_components/entity_rename_assistant
```

Add this to `configuration.yaml`:

```yaml
entity_rename_assistant:
```

Restart Home Assistant.

## Notes

The integration registers a sidebar panel plus `entity_rename_assistant.scan` and `entity_rename_assistant.apply` services. Applying a rename writes backups under `config/entity_rename_assistant_backups/`.

