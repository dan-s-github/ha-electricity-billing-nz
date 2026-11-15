"""Support for Electricity Billing NZ sensors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import ElectricityBillingNzConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: ElectricityBillingNzConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the example sensor entity."""
    async_add_entities([ElectricityBillingNzSensor(entry)])


class ElectricityBillingNzSensor(SensorEntity):
    """Representation of a scaffold sensor entity."""

    _attr_has_entity_name = True
    _attr_name = "Status"
    _attr_icon = "mdi:check-circle-outline"

    def __init__(self, entry: ElectricityBillingNzConfigEntry) -> None:
        """Initialize entity."""
        self._attr_unique_id = f"{entry.entry_id}_status"

    @property
    def native_value(self) -> int:
        """Return a constant value for scaffold verification."""
        return 1
