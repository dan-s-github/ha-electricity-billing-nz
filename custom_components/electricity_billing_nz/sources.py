"""
Source entity references that survive renames.

Sources are stored by entity registry id, which stays the same when the
user renames the entity. Entities without a registry entry can't be
renamed, so they're stored by entity id.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import CONF_FORECAST_ENTITY, CONF_LIVE_ENTITY, CONF_SETTLED_ENTITY

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

SOURCE_KEYS = (CONF_SETTLED_ENTITY, CONF_LIVE_ENTITY, CONF_FORECAST_ENTITY)


@callback
def source_id(hass: HomeAssistant, entity_id: str) -> str:
    """Return what to store for a source: its registry id if it has one."""
    entry = er.async_get(hass).async_get(entity_id)
    return entry.id if entry else entity_id


@callback
def resolve_source(hass: HomeAssistant, stored: str) -> str:
    """
    Return a stored source's current entity id.

    A registry id whose entity was deleted is returned unchanged: it has no
    state, so the sensors it feeds are unavailable.
    """
    return er.async_resolve_entity_id(er.async_get(hass), stored) or stored


@callback
def async_store_registry_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Switch sources stored by entity id to registry ids where possible."""
    data = dict(entry.data)
    for key in SOURCE_KEYS:
        if value := data.get(key):
            data[key] = source_id(hass, value)
    unique_id = entry.unique_id
    if unique_id and unique_id == entry.data.get(CONF_SETTLED_ENTITY):
        unique_id = data[CONF_SETTLED_ENTITY]
    if data != entry.data or unique_id != entry.unique_id:
        hass.config_entries.async_update_entry(entry, data=data, unique_id=unique_id)
