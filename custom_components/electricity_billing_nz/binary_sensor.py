"""Support for Electricity Billing NZ binary sensors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import BinarySensorEntity

from .entity import TariffEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import ElectricityBillingNzConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: ElectricityBillingNzConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the peak period binary sensor."""
    async_add_entities([PeakBinarySensor(entry, "peak")])


class PeakBinarySensor(TariffEntity, BinarySensorEntity):
    """On while the current time-of-use band is peak."""

    @property
    def is_on(self) -> bool:
        """Return True during a band the schedule marks as peak."""
        return self.engine.is_peak
