"""Support for Electricity Billing NZ sensors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

from .const import CURRENCY, DAILY_UNIT, DOMAIN, PRICE_PRECISION, PRICE_UNIT
from .entity import BillingEntity, TariffEntity
from .tariff import PriceBreakdown, all_bands, nz_midnight

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import ElectricityBillingNzConfigEntry
    from .billing import Bill
    from .engine import PricePair
    from .tariff import LossVersion, NetworkVersion, Rates, RetailerVersion

type Source = Literal["settled", "live"]
type Side = Literal["import", "export"]

# Restored attributes: the price breakdown only, not HA's own (unit, name...).
RESTORED_ATTRIBUTES = PriceBreakdown.attribute_keys() | {
    "period_start",
    "trading_period",
}
BILLING_KEYS = (
    "billing_cycle_import_energy",
    "billing_cycle_export_energy",
    "billing_cycle_import_energy_rate",
    "billing_cycle_export_energy_rate",
    "billing_cycle_estimated_total",
    "import_cost_total",
    "export_earnings_total",
    "metering_since",
)


@dataclass(frozen=True, kw_only=True)
class RateDescription(SensorEntityDescription):
    """A configured rate in force now, shown as a diagnostic sensor (ex GST)."""

    # None while the rate doesn't apply (e.g. no export floor, or no rate
    # for the band now).
    value: Callable[[Rates], float | None]
    # The version the rate comes from, for its effective date and source.
    version: Callable[[Rates], NetworkVersion | LossVersion | RetailerVersion]
    # Every band's rate, for a rate that depends on the band.
    per_band: Callable[[Rates], Mapping[str, float]] | None = None
    entity_category: EntityCategory | None = EntityCategory.DIAGNOSTIC
    native_unit_of_measurement: str | None = PRICE_UNIT
    # No suggested precision: the rate as configured, without padded zeros.


def _retailer(rates: Rates) -> RetailerVersion:
    return rates.retailer


def _network(rates: Rates) -> NetworkVersion:
    return rates.network


RATE_SENSORS = (
    RateDescription(
        key="retailer_import_margin",
        value=lambda r: r.retailer.import_margin,
        version=_retailer,
    ),
    RateDescription(
        key="retailer_export_margin",
        value=lambda r: r.retailer.export_margin,
        version=_retailer,
    ),
    RateDescription(
        key="retailer_export_floor",
        value=lambda r: r.retailer.export_floor,
        version=_retailer,
    ),
    RateDescription(
        key="transmission_rate",
        value=lambda r: r.retailer.transmission,
        version=_retailer,
    ),
    RateDescription(
        key="levy_rate", value=lambda r: r.retailer.levy, version=_retailer
    ),
    RateDescription(
        key="metering_daily_charge",
        value=lambda r: r.retailer.metering_daily,
        version=_retailer,
        native_unit_of_measurement=DAILY_UNIT,
    ),
    RateDescription(
        key="retailer_daily_charge",
        value=lambda r: r.retailer.retailer_daily,
        version=_retailer,
        native_unit_of_measurement=DAILY_UNIT,
    ),
    RateDescription(
        key="network_import_rate",
        value=lambda r: r.network.import_rates.get(r.band),
        version=_network,
        per_band=lambda r: r.network.import_rates,
    ),
    RateDescription(
        key="network_export_credit",
        value=lambda r: r.network.export_credit.get(r.band, 0.0),
        version=_network,
        per_band=lambda r: {
            band: r.network.export_credit.get(band, 0.0)
            for band in r.network.import_rates
        },
    ),
    RateDescription(
        key="network_daily_charge",
        value=lambda r: r.network.daily,
        version=_network,
        native_unit_of_measurement=DAILY_UNIT,
    ),
    RateDescription(
        key="loss_factor",
        value=lambda r: r.loss.factor,
        version=lambda r: r.loss,
        native_unit_of_measurement=None,
    ),
)
OPTIONAL_KEYS = (
    "import_price_live",
    "export_price_live",
    "import_price_forecast",
    "export_price_forecast",
    *BILLING_KEYS,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ElectricityBillingNzConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the price, band and daily charge sensors."""
    engine = entry.runtime_data.engine
    entities: list[SensorEntity] = [
        PriceSensor(entry, "import_price", "settled", "import"),
        PriceSensor(entry, "export_price", "settled", "export"),
        DailyChargeSensor(entry, "daily_charge"),
        TouBandSensor(entry, "tou_band"),
        NextBandChangeSensor(entry, "next_band_change"),
        *(
            RateSensor(entry, description)
            for description in RATE_SENSORS
            # The export floor only for a plan that has one.
            if description.key != "retailer_export_floor"
            or any(v.export_floor is not None for v in engine.tariff.retailer)
        ),
    ]
    if engine.live_entity:
        entities += [
            PriceSensor(entry, "import_price_live", "live", "import"),
            PriceSensor(entry, "export_price_live", "live", "export"),
        ]
    if engine.forecast_entity:
        entities += [
            ForecastPriceSensor(entry, "import_price_forecast", "import"),
            ForecastPriceSensor(entry, "export_price_forecast", "export"),
        ]
    if engine.meter is not None:
        entities += [
            CycleEnergySensor(entry, "billing_cycle_import_energy", "import"),
            CycleRateSensor(entry, "billing_cycle_import_energy_rate", "import"),
            CycleTotalSensor(entry, "billing_cycle_estimated_total"),
            LifetimeTotalSensor(entry, "import_cost_total", "import"),
            MeteringSinceSensor(entry, "metering_since"),
        ]
        if "export" in engine.meter.sides:
            entities += [
                CycleEnergySensor(entry, "billing_cycle_export_energy", "export"),
                CycleRateSensor(entry, "billing_cycle_export_energy_rate", "export"),
                LifetimeTotalSensor(entry, "export_earnings_total", "export"),
            ]
    async_add_entities(entities)

    # Drop sensors whose optional source was removed in the options.
    registry = er.async_get(hass)
    created = {entity.unique_id for entity in entities}
    for key in OPTIONAL_KEYS:
        unique_id = f"{entry.entry_id}_{key}"
        if unique_id not in created and (
            entity_id := registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        ):
            registry.async_remove(entity_id)


class PriceSensor(BillingEntity, RestoreSensor):
    """Landed import or export price for the settled or live period."""

    _attr_native_unit_of_measurement = PRICE_UNIT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4

    def __init__(
        self,
        entry: ElectricityBillingNzConfigEntry,
        key: str,
        source: Source,
        side: Side,
    ) -> None:
        """Initialise the price sensor."""
        super().__init__(entry, key)
        self._source = source
        self._side = side
        self._restored_value: float | None = None
        self._restored_attributes: dict[str, Any] = {}

    async def async_added_to_hass(self) -> None:
        """Restore the last price until the source reports."""
        await super().async_added_to_hass()
        data = await self.async_get_last_sensor_data()
        if data is None or not isinstance(data.native_value, (int, float)):
            return
        state = await self.async_get_last_state()
        attributes = state.attributes if state is not None else {}
        # A price from before the GST option changed is on the wrong basis.
        if attributes.get("gst", self.engine.tariff.gst) != self.engine.tariff.gst:
            return
        self._restored_value = float(data.native_value)
        self._restored_attributes = {
            k: v for k, v in attributes.items() if k in RESTORED_ATTRIBUTES
        }

    @property
    def _pair(self) -> PricePair | None:
        return self.engine.settled if self._source == "settled" else self.engine.live

    @property
    def _source_entity(self) -> str | None:
        return (
            self.engine.settled_entity
            if self._source == "settled"
            else self.engine.live_entity
        )

    @property
    def _use_restored(self) -> bool:
        return (
            self._pair is None
            and self._restored_value is not None
            and self._source_entity not in self.engine.source_reported
        )

    @property
    def available(self) -> bool:
        """Return False when the spot price is unavailable."""
        return self._pair is not None or self._use_restored

    @property
    def native_value(self) -> float | None:
        """Return the landed price in NZD/kWh."""
        if (pair := self._pair) is not None:
            breakdown = (
                pair.import_price if self._side == "import" else pair.export_price
            )
            return round(breakdown.price, PRICE_PRECISION)
        return self._restored_value if self._use_restored else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the price components (ex GST) and the period priced."""
        if (pair := self._pair) is None:
            return self._restored_attributes if self._use_restored else None
        breakdown = pair.import_price if self._side == "import" else pair.export_price
        return {
            **breakdown.as_attributes(),
            "period_start": pair.period_start.isoformat(),
            "trading_period": pair.trading_period,
        }


class ForecastPriceSensor(BillingEntity, SensorEntity):
    """Current or next forecast price, with every forecast period as a list."""

    _attr_native_unit_of_measurement = PRICE_UNIT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4
    # The list changes every update and would bloat the recorder.
    _unrecorded_attributes = frozenset({"forecast"})

    def __init__(
        self, entry: ElectricityBillingNzConfigEntry, key: str, side: Side
    ) -> None:
        """Initialise the forecast sensor."""
        super().__init__(entry, key)
        self._side = side

    def _price(self, row: Any) -> float:
        value = row.import_price if self._side == "import" else row.export_price
        return round(value, PRICE_PRECISION)

    @property
    def available(self) -> bool:
        """
        Return False when no forecast period can be priced.

        With priced later periods but none for now, it stays available with
        an unknown state, so the forecast list can still be read.
        """
        return self.engine.forecast_available

    @property
    def native_value(self) -> float | None:
        """Return the price for the current period (or the next, if not listed)."""
        row = self.engine.forecast_current
        return self._price(row) if row is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return one row per trading period."""
        return {
            "forecast": [
                {
                    "period_start": row.period_start.isoformat(),
                    "trading_period": row.trading_period,
                    "price": self._price(row),
                    "tou_band": row.tou_band,
                }
                for row in self.engine.forecast
            ]
        }


class DailyChargeSensor(TariffEntity, SensorEntity):
    """Fixed daily charges: network, metering and retailer."""

    _attr_native_unit_of_measurement = DAILY_UNIT
    _attr_suggested_display_precision = 4

    @property
    def native_value(self) -> float | None:
        """Return the daily charge in NZD."""
        value = self.engine.daily_charge
        return round(value, PRICE_PRECISION) if value is not None else None


class WriteOnChange(BillingEntity):
    """
    Write state only when it changes.

    The engine updates on every source and meter reading; diagnostics change
    at band boundaries, new versions or new cycles.
    """

    _written: tuple[Any, ...] | None = None

    @callback
    def _handle_engine_update(self) -> None:
        written = (self.available, self.state, self.extra_state_attributes)
        if written != self._written:
            self._written = written
            self.async_write_ha_state()


class RateSensor(WriteOnChange, TariffEntity, SensorEntity):
    """A configured rate in force now, ex GST: shows what prices are built from."""

    entity_description: RateDescription

    def __init__(
        self, entry: ElectricityBillingNzConfigEntry, description: RateDescription
    ) -> None:
        """Initialise for one rate."""
        super().__init__(entry, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        """Return the rate in force now."""
        rates = self.engine.rates
        if rates is None or (value := self.entity_description.value(rates)) is None:
            return None
        return round(value, PRICE_PRECISION)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the version the rate comes from, and any per-band rates."""
        rates = self.engine.rates
        if rates is None:
            return None
        description = self.entity_description
        version = description.version(rates)
        attributes: dict[str, Any] = {
            "effective_from": version.effective_from.isoformat()
        }
        if source := getattr(version, "source", None):
            attributes["source"] = source
        if description.per_band is not None:
            attributes["band"] = rates.band
            attributes["rates"] = dict(description.per_band(rates))
        return attributes


class TouBandSensor(TariffEntity, SensorEntity):
    """Current time-of-use band."""

    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, entry: ElectricityBillingNzConfigEntry, key: str) -> None:
        """Initialise with every band the tariff can resolve to."""
        super().__init__(entry, key)
        self._attr_options = all_bands(self.engine.tariff.network)

    @property
    def native_value(self) -> str | None:
        """Return the band name."""
        return self.engine.tou_band


class NextBandChangeSensor(TariffEntity, SensorEntity):
    """When the time-of-use band next changes."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        """Return the next band change."""
        return self.engine.next_band_change


class CycleSensor(BillingEntity, SensorEntity):
    """A sensor for the current billing cycle: available once it has started."""

    @property
    def available(self) -> bool:
        """Return False until the meter has its first reading."""
        return self.engine.bill is not None

    @property
    def last_reset(self) -> datetime | None:
        """Return when the current cycle started."""
        bill = self.engine.bill
        return nz_midnight(bill.start) if bill is not None else None


class CycleEnergySensor(CycleSensor):
    """Energy imported or exported this billing cycle."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2

    def __init__(
        self, entry: ElectricityBillingNzConfigEntry, key: str, side: Side
    ) -> None:
        """Initialise for import or export."""
        super().__init__(entry, key)
        self._index = 0 if side == "import" else 1

    @property
    def native_value(self) -> float | None:
        """Return the kWh metered this cycle."""
        if (bill := self.engine.bill) is None:
            return None
        return round(sum(v[self._index] for v in bill.kwh.values()), 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the kWh per time-of-use band."""
        if (bill := self.engine.bill) is None:
            return None
        return {band: round(v[self._index], 3) for band, v in bill.kwh.items()}


class CycleRateSensor(CycleSensor):
    """
    Average energy rate this cycle, ex GST, to check against the bill.

    Import is per kWh with losses, as billed; export leaves out the network
    credit, which the bill shows as its own line.
    """

    _attr_native_unit_of_measurement = PRICE_UNIT
    _attr_suggested_display_precision = 4

    def __init__(
        self, entry: ElectricityBillingNzConfigEntry, key: str, side: Side
    ) -> None:
        """Initialise for import or export."""
        super().__init__(entry, key)
        self._side = side

    def _rate(self) -> float | None:
        if (bill := self.engine.bill) is None:
            return None
        if self._side == "import":
            return bill.import_energy_rate
        return bill.export_energy_rate

    @property
    def available(self) -> bool:
        """Return False until energy has been priced this cycle."""
        return self._rate() is not None

    @property
    def last_reset(self) -> datetime | None:
        """Return None: an average has no reset."""
        return None

    @property
    def native_value(self) -> float | None:
        """Return the average rate in NZD/kWh."""
        rate = self._rate()
        return round(rate, PRICE_PRECISION) if rate is not None else None


def _bill_summary(bill: Bill) -> dict[str, Any]:
    return {
        "cycle_start": bill.start.isoformat(),
        "cycle_end": bill.end.isoformat(),
        "complete": bill.complete,
        "provisional_periods": bill.provisional_periods,
        "unsettled_periods": bill.unsettled_periods,
        "unpriced_periods": bill.unpriced_periods,
        **bill.lines(),
    }


class CycleTotalSensor(CycleSensor):
    """The bill so far this cycle, with the invoice lines as attributes."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = CURRENCY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2
    # The lines change with every reading and would bloat the recorder.
    _unrecorded_attributes = frozenset(
        {"daily_charges", "network", "energy", "export", "previous_cycle"}
    )

    @property
    def native_value(self) -> float | None:
        """Return the estimated total so far."""
        bill = self.engine.bill
        return round(bill.total, 2) if bill is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the invoice lines, and the previous cycle's."""
        if (bill := self.engine.bill) is None:
            return None
        previous = self.engine.previous_bill
        return {
            **_bill_summary(bill),
            "previous_cycle": _bill_summary(previous) if previous else None,
        }


class MeteringSinceSensor(WriteOnChange, CycleSensor):
    """When metering of the current cycle began: before its start, it's complete."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def last_reset(self) -> datetime | None:
        """Return None: a timestamp has no reset."""
        return None

    @property
    def native_value(self) -> datetime | None:
        """Return when metering of the current cycle began."""
        bill, meter = self.engine.bill, self.engine.meter
        if (
            bill is None
            or meter is None
            or (cycle := meter.cycles.get(bill.start)) is None
        ):
            return None
        return cycle.since

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return whether the cycle is complete, and the meters read."""
        bill = self.engine.bill
        if bill is None:
            return None
        return {
            "complete": bill.complete,
            "meters": sorted(self.engine.meter_entities),
        }


class LifetimeTotalSensor(BillingEntity, SensorEntity):
    """
    Import cost or export earnings since billing was set up.

    Grows as periods settle. It can fall when spot prices are negative,
    hence state class total rather than total_increasing.
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = CURRENCY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2

    def __init__(
        self, entry: ElectricityBillingNzConfigEntry, key: str, side: Side
    ) -> None:
        """Initialise for import cost or export earnings."""
        super().__init__(entry, key)
        self._side = side

    @property
    def native_value(self) -> float | None:
        """Return the total in NZD."""
        meter = self.engine.meter
        if meter is None:
            return None
        value = meter.import_cost if self._side == "import" else meter.export_earnings
        return round(value, 4)
