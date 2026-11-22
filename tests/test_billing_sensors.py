"""Test the billing-cycle sensors, options and storage."""

from __future__ import annotations

from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.electricity_billing_nz.billing import BillingMeter
from custom_components.electricity_billing_nz.const import DOMAIN

from .conftest import ECOTRICITY, entry_data
from .helpers import nz, set_live, set_settled, template_import
from .test_billing import TARIFF

IMPORT_METER = "sensor.grid_import_energy"
EXPORT_METER = "sensor.grid_export_energy"
ENERGY = "sensor.home_billing_cycle_import_energy"
EXPORT_ENERGY = "sensor.home_billing_cycle_export_energy"
RATE = "sensor.home_billing_cycle_import_energy_rate"
EXPORT_RATE = "sensor.home_billing_cycle_export_energy_rate"
TOTAL = "sensor.home_billing_cycle_estimated_total"
COST = "sensor.home_import_cost_total"
EARNINGS = "sensor.home_export_earnings_total"
BILLING_SENSORS = (ENERGY, EXPORT_ENERGY, RATE, EXPORT_RATE, TOTAL, COST, EARNINGS)
DAILY = 0.90 + 0.35 + 0.15


def _options(**billing: Any) -> dict[str, Any]:
    return {
        "retailer_versions": [ECOTRICITY],
        "network_overrides": [],
        "import_energy_entity": IMPORT_METER,
        "export_energy_entity": EXPORT_METER,
        **billing,
    }


@pytest.fixture
def billing_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return an entry with billing set up on the grid energy meters."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="Home", data=entry_data(), options=_options()
    )
    entry.add_to_hass(hass)
    return entry


def _set_meter(hass: HomeAssistant, entity_id: str, kwh: float | str) -> None:
    hass.states.async_set(
        entity_id,
        str(kwh),
        {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
        },
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _value(hass: HomeAssistant, entity_id: str) -> float:
    return float(hass.states.get(entity_id).state)


async def test_cycle_is_metered_and_priced(
    hass: HomeAssistant,
    billing_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Energy counts at once, at the live spot until each period settles."""
    freezer.move_to(nz("2026-11-17 10:05"))
    set_settled(hass, 5.0, nz("2026-11-17 09:30"))
    set_live(hass, 8.0, nz("2026-11-17 10:00"))
    _set_meter(hass, IMPORT_METER, 100.0)
    _set_meter(hass, EXPORT_METER, 50.0)
    await _setup(hass, billing_entry)

    assert _value(hass, ENERGY) == 0
    # Only today's daily charges: metering began today.
    assert _value(hass, TOTAL) == pytest.approx(round(DAILY * 1.15, 2))
    assert hass.states.get(RATE).state == "unavailable"
    attrs = hass.states.get(TOTAL).attributes
    assert attrs["cycle_start"] == "2026-11-01"
    assert attrs["cycle_end"] == "2026-12-01"
    assert attrs["complete"] is False
    assert attrs["previous_cycle"] is None
    assert hass.states.get(ENERGY).attributes["last_reset"] == (
        "2026-11-01T00:00:00+13:00"
    )

    # 1 kWh from 10:05 to 10:35: 25 minutes in the 10:00 period, 5 in 10:30.
    freezer.move_to(nz("2026-11-17 10:35"))
    _set_meter(hass, IMPORT_METER, 101.0)
    await hass.async_block_till_done()
    assert _value(hass, ENERGY) == pytest.approx(1.0)
    assert hass.states.get(ENERGY).attributes["offpeak"] == pytest.approx(1.0)
    assert _value(hass, RATE) == pytest.approx(0.08 + 0.028)
    assert hass.states.get(TOTAL).attributes["provisional_periods"] == 2
    assert _value(hass, COST) == 0

    # The 10:00 period settles at 4c; 10:30 is still at the live 8c.
    set_settled(hass, 4.0, nz("2026-11-17 10:00"))
    await hass.async_block_till_done()
    settled_kwh, live_kwh = 25 / 30, 5 / 30
    assert _value(hass, RATE) == pytest.approx(
        settled_kwh * (0.04 + 0.028) + live_kwh * (0.08 + 0.028), abs=1e-6
    )
    assert hass.states.get(TOTAL).attributes["provisional_periods"] == 1
    assert _value(hass, COST) == pytest.approx(
        round(settled_kwh * template_import(0.04, peak=False), 4)
    )


async def test_export_is_metered(
    hass: HomeAssistant,
    billing_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Export energy and its rate come from the export meter."""
    freezer.move_to(nz("2026-11-17 12:00"))
    set_settled(hass, 3.0, nz("2026-11-17 11:30"))
    _set_meter(hass, IMPORT_METER, 100.0)
    _set_meter(hass, EXPORT_METER, 50.0)
    await _setup(hass, billing_entry)

    freezer.move_to(nz("2026-11-17 12:30"))
    _set_meter(hass, EXPORT_METER, 52.0)
    set_settled(hass, 3.0, nz("2026-11-17 12:00"))
    await hass.async_block_till_done()

    assert _value(hass, EXPORT_ENERGY) == pytest.approx(2.0)
    assert _value(hass, EXPORT_RATE) == pytest.approx(0.03 - 0.019)
    assert _value(hass, EARNINGS) == pytest.approx(round(2 * 0.011 * 1.15, 4))


async def test_unchanged_reading_moves_the_baseline(
    hass: HomeAssistant,
    billing_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """A meter re-reporting its value marks time when nothing was used."""
    freezer.move_to(nz("2026-11-17 10:05"))
    set_live(hass, 8.0, nz("2026-11-17 10:00"))
    _set_meter(hass, IMPORT_METER, 100.0)
    await _setup(hass, billing_entry)

    freezer.move_to(nz("2026-11-17 12:00"))
    _set_meter(hass, IMPORT_METER, 100.0)  # same value: a state report
    await hass.async_block_till_done()
    # The moved baseline is saved, so it holds over a restart.
    freezer.tick(60)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    stored = hass_storage[f"{DOMAIN}.{billing_entry.entry_id}"]["data"]
    assert stored["readings"]["import"]["at"].startswith("2026-11-16T23:00")
    freezer.move_to(nz("2026-11-17 12:20"))
    _set_meter(hass, IMPORT_METER, 101.0)
    await hass.async_block_till_done()

    # All of it in the 12:00 period, none spread back to 10:05.
    assert hass.states.get(TOTAL).attributes["provisional_periods"] == 1


def _set_power(hass: HomeAssistant, entity_id: str, watts: float | str) -> None:
    hass.states.async_set(
        entity_id,
        str(watts),
        {
            "unit_of_measurement": "W",
            "device_class": "power",
            "state_class": "measurement",
        },
    )


async def test_steady_power_settles_in_its_period(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A power sensor that doesn't change still fills each period in time."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data=entry_data(),
        options=_options(import_energy_entity="sensor.grid_import_power"),
    )
    entry.add_to_hass(hass)
    freezer.move_to(nz("2026-11-17 10:00"))
    set_live(hass, 8.0, nz("2026-11-17 10:00"))
    _set_power(hass, "sensor.grid_import_power", 2000)
    await _setup(hass, entry)

    # No new reading: the 10:30 boundary counts the held 2 kW into 10:00.
    freezer.move_to(nz("2026-11-17 10:30"))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _value(hass, ENERGY) == pytest.approx(1.0)

    freezer.move_to(nz("2026-11-17 10:35"))
    set_settled(hass, 4.0, nz("2026-11-17 10:00"))
    await hass.async_block_till_done()
    assert _value(hass, COST) == pytest.approx(
        round(template_import(0.04, peak=False), 4)
    )


async def test_held_power_is_saved_up_to_now(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """Saving counts the power held since the last reading, so it isn't lost."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data=entry_data(),
        options=_options(import_energy_entity="sensor.grid_import_power"),
    )
    entry.add_to_hass(hass)
    freezer.move_to(nz("2026-11-17 10:00"))
    _set_power(hass, "sensor.grid_import_power", 3000)
    await _setup(hass, entry)

    freezer.move_to(nz("2026-11-17 10:20"))
    assert await hass.config_entries.async_unload(entry.entry_id)
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]
    assert sum(p["import_kwh"] for p in stored["pending"]) == pytest.approx(1.0)


async def test_signed_grid_power_meters_both_sides(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """One signed sensor: positive is import, negative export."""
    options = _options(grid_power_entity="sensor.grid_power")
    del options["import_energy_entity"], options["export_energy_entity"]
    entry = MockConfigEntry(
        domain=DOMAIN, title="Home", data=entry_data(), options=options
    )
    entry.add_to_hass(hass)
    freezer.move_to(nz("2026-11-17 10:00"))
    set_live(hass, 8.0, nz("2026-11-17 10:00"))
    _set_power(hass, "sensor.grid_power", 1000)
    await _setup(hass, entry)
    assert hass.states.get(EXPORT_ENERGY) is not None

    freezer.move_to(nz("2026-11-17 10:30"))
    _set_power(hass, "sensor.grid_power", -3000)
    await hass.async_block_till_done()
    freezer.move_to(nz("2026-11-17 10:50"))
    _set_power(hass, "sensor.grid_power", 0)
    await hass.async_block_till_done()
    # Power readings refresh the bill at most every 30 seconds.
    freezer.tick(31)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert _value(hass, ENERGY) == pytest.approx(0.5)
    assert _value(hass, EXPORT_ENERGY) == pytest.approx(1.0)


async def test_options_accept_a_signed_grid_power_sensor(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """The signed grid sensor turns billing on in place of the two meters."""
    _set_power(hass, "sensor.grid_power", 500)
    result = await _billing_options(hass, config_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"grid_power_entity": "sensor.grid_power", "billing_cycle_day": 1},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert config_entry.options["grid_power_entity"] == "sensor.grid_power"
    assert hass.states.get(ENERGY) is not None
    assert hass.states.get(EXPORT_ENERGY) is not None


async def test_meter_reporting_too_rarely_raises_a_repair(
    hass: HomeAssistant,
    billing_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A meter with long gaps between readings is flagged once a run."""
    issue_id = f"slow_meter_{billing_entry.entry_id}_import"
    registry = ir.async_get(hass)
    freezer.move_to(nz("2026-11-17 10:05"))
    _set_meter(hass, IMPORT_METER, 100.0)
    await _setup(hass, billing_entry)

    freezer.move_to(nz("2026-11-17 11:05"))
    _set_meter(hass, IMPORT_METER, 101.2)  # an hour: fine
    await hass.async_block_till_done()
    assert registry.async_get_issue(DOMAIN, issue_id) is None

    freezer.move_to(nz("2026-11-17 13:05"))
    _set_meter(hass, IMPORT_METER, 102.4)
    await hass.async_block_till_done()
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_placeholders["entity_id"] == IMPORT_METER
    assert issue.translation_placeholders["minutes"] == "120"
    assert caplog.text.count("without a reading") == 1

    # Logged and raised once a run.
    freezer.move_to(nz("2026-11-17 15:05"))
    _set_meter(hass, IMPORT_METER, 103.6)
    await hass.async_block_till_done()
    assert caplog.text.count("without a reading") == 1

    # Reloading (as changing the meter does) clears it.
    assert await hass.config_entries.async_reload(billing_entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_unavailable_meter_is_not_flagged(
    hass: HomeAssistant,
    billing_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A gap while the meter was unavailable isn't the meter reporting rarely."""
    freezer.move_to(nz("2026-11-17 10:05"))
    _set_meter(hass, IMPORT_METER, 100.0)
    await _setup(hass, billing_entry)
    freezer.move_to(nz("2026-11-17 10:10"))
    _set_meter(hass, IMPORT_METER, "unavailable")
    await hass.async_block_till_done()
    freezer.move_to(nz("2026-11-17 13:05"))
    _set_meter(hass, IMPORT_METER, 102.4)
    await hass.async_block_till_done()
    issue_id = f"slow_meter_{billing_entry.entry_id}_import"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_removing_the_entry_clears_the_repair(
    hass: HomeAssistant,
    billing_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A removed entry leaves no Repairs issue behind."""
    freezer.move_to(nz("2026-11-17 10:05"))
    _set_meter(hass, IMPORT_METER, 100.0)
    await _setup(hass, billing_entry)
    freezer.move_to(nz("2026-11-17 13:05"))
    _set_meter(hass, IMPORT_METER, 102.4)
    await hass.async_block_till_done()
    issue_id = f"slow_meter_{billing_entry.entry_id}_import"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    assert await hass.config_entries.async_remove(billing_entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_metering_since_shows_when_the_cycle_was_metered_from(
    hass: HomeAssistant,
    billing_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A diagnostic shows when metering began and whether the cycle is complete."""
    freezer.move_to(nz("2026-11-17 10:05"))
    _set_meter(hass, IMPORT_METER, 100.0)
    _set_meter(hass, EXPORT_METER, 5.0)
    await _setup(hass, billing_entry)

    state = hass.states.get("sensor.home_metering_since")
    assert state.state == "2026-11-16T21:05:00+00:00"
    assert state.attributes["complete"] is False
    assert state.attributes["meters"] == [EXPORT_METER, IMPORT_METER]
    entry = er.async_get(hass).async_get("sensor.home_metering_since")
    assert entry is not None
    assert entry.entity_category is EntityCategory.DIAGNOSTIC


async def test_first_reading_after_setup_starts_the_bill(
    hass: HomeAssistant,
    billing_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A meter unavailable at setup starts the cycle on its first reading."""
    freezer.move_to(nz("2026-11-17 10:05"))
    _set_meter(hass, IMPORT_METER, "unavailable")
    await _setup(hass, billing_entry)
    assert hass.states.get(TOTAL).state == "unavailable"

    _set_meter(hass, IMPORT_METER, 100.0)
    await hass.async_block_till_done()
    assert _value(hass, TOTAL) == pytest.approx(round(DAILY * 1.15, 2))


async def test_meter_survives_a_reload(
    hass: HomeAssistant,
    billing_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """Unloading saves the meter; setting up again picks it up."""
    freezer.move_to(nz("2026-11-17 10:05"))
    set_live(hass, 8.0, nz("2026-11-17 10:00"))
    _set_meter(hass, IMPORT_METER, 100.0)
    await _setup(hass, billing_entry)
    freezer.move_to(nz("2026-11-17 10:20"))
    _set_meter(hass, IMPORT_METER, 102.0)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_reload(billing_entry.entry_id)
    await hass.async_block_till_done()

    key = f"{DOMAIN}.{billing_entry.entry_id}"
    assert hass_storage[key]["data"]["readings"]["import"]["value"] == 102.0
    assert _value(hass, ENERGY) == pytest.approx(2.0)


async def test_meter_is_loaded_from_storage(
    hass: HomeAssistant,
    billing_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """At startup the cycle continues from the stored meter."""
    meter = BillingMeter(
        TARIFF, 1, sources={"import": IMPORT_METER, "export": EXPORT_METER}
    )
    meter.add_reading("import", 10.0, nz("2026-11-01 00:00"))
    meter.add_reading("import", 13.0, nz("2026-11-02 10:00"))
    hass_storage[f"{DOMAIN}.{billing_entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.{billing_entry.entry_id}",
        "data": meter.as_dict(),
    }
    freezer.move_to(nz("2026-11-17 10:05"))
    await _setup(hass, billing_entry)

    # Version 1 storage is read and rewritten as version 2.
    assert hass_storage[f"{DOMAIN}.{billing_entry.entry_id}"]["version"] == 2
    assert _value(hass, ENERGY) == pytest.approx(3.0)
    assert hass.states.get(TOTAL).attributes["complete"] is True

    # The meter's next reading covers the time HA was down.
    _set_meter(hass, IMPORT_METER, 14.0)
    await hass.async_block_till_done()
    assert _value(hass, ENERGY) == pytest.approx(4.0)


async def test_new_cycle_starts_on_the_cycle_day(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """At midnight on the cycle day the totals move to the previous cycle."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data=entry_data(),
        options=_options(billing_cycle_day=15),
    )
    entry.add_to_hass(hass)
    freezer.move_to(nz("2026-11-14 23:20"))
    _set_meter(hass, IMPORT_METER, 100.0)
    await _setup(hass, entry)
    freezer.move_to(nz("2026-11-14 23:50"))
    _set_meter(hass, IMPORT_METER, 101.0)
    await hass.async_block_till_done()
    assert hass.states.get(TOTAL).attributes["cycle_start"] == "2026-10-15"

    freezer.move_to(nz("2026-11-15 00:00"))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    attrs = hass.states.get(TOTAL).attributes
    assert attrs["cycle_start"] == "2026-11-15"
    assert attrs["complete"] is True
    assert _value(hass, ENERGY) == 0
    previous = attrs["previous_cycle"]
    assert previous["cycle_start"] == "2026-10-15"
    # Metered from 23:20 on its last day only.
    assert previous["complete"] is False
    assert previous["days"] == 1


async def test_no_billing_sensors_without_a_meter(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Billing stays off until an import energy meter is chosen."""
    await _setup(hass, config_entry)
    for entity_id in BILLING_SENSORS:
        assert hass.states.get(entity_id) is None


async def _billing_options(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    await _setup(hass, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "billing"}
    )


async def test_options_turn_billing_on(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Choosing the meters and cycle day sets billing up."""
    _set_meter(hass, IMPORT_METER, 100.0)
    hass.states.async_set(
        EXPORT_METER,
        "5000",
        {"unit_of_measurement": "Wh", "state_class": "total_increasing"},
    )
    result = await _billing_options(hass, config_entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "billing"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "import_energy_entity": IMPORT_METER,
            "export_energy_entity": EXPORT_METER,
            "billing_cycle_day": 15,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert config_entry.options["import_energy_entity"] == IMPORT_METER
    assert config_entry.options["billing_cycle_day"] == 15
    assert config_entry.options["retailer_versions"] == [ECOTRICITY]
    assert hass.states.get(ENERGY) is not None


@pytest.mark.parametrize(
    ("values", "errors"),
    [
        (
            {"export_energy_entity": EXPORT_METER},
            {"import_energy_entity": "import_energy_required"},
        ),
        (
            {
                "import_energy_entity": IMPORT_METER,
                "export_energy_entity": IMPORT_METER,
            },
            {"export_energy_entity": "duplicate_source"},
        ),
        (
            {"import_energy_entity": "sensor.temperature"},
            {"import_energy_entity": "invalid_meter_unit"},
        ),
        (
            {"grid_power_entity": IMPORT_METER},
            {"grid_power_entity": "invalid_power_unit"},
        ),
        (
            {"import_energy_entity": IMPORT_METER, "grid_power_entity": "sensor.power"},
            {"grid_power_entity": "grid_power_exclusive"},
        ),
        (
            {"import_energy_entity": "sensor.missing"},
            {"import_energy_entity": "entity_not_found"},
        ),
        (
            {"import_energy_entity": "sensor.energy_reading"},
            {"import_energy_entity": "not_cumulative"},
        ),
    ],
)
async def test_options_billing_errors(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    values: dict[str, str],
    errors: dict[str, str],
) -> None:
    """The meters must exist, read energy totals or power, differ, include import."""
    _set_meter(hass, IMPORT_METER, 100.0)
    _set_meter(hass, EXPORT_METER, 5.0)
    hass.states.async_set("sensor.power", "300", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.temperature", "21", {"unit_of_measurement": "°C"})
    hass.states.async_set(
        "sensor.energy_reading",
        "3",
        {"unit_of_measurement": "kWh", "state_class": "measurement"},
    )
    result = await _billing_options(hass, config_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**values, "billing_cycle_day": 1}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors


async def test_options_keep_a_meter_without_a_state_class(
    hass: HomeAssistant, billing_entry: MockConfigEntry
) -> None:
    """A meter already in use isn't re-checked, so the cycle day can change."""
    hass.states.async_set(IMPORT_METER, "100", {"unit_of_measurement": "kWh"})
    _set_meter(hass, EXPORT_METER, 5.0)
    result = await _billing_options(hass, billing_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "import_energy_entity": IMPORT_METER,
            "export_energy_entity": EXPORT_METER,
            "billing_cycle_day": 15,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert billing_entry.options["billing_cycle_day"] == 15


async def test_options_turn_billing_off(
    hass: HomeAssistant, billing_entry: MockConfigEntry
) -> None:
    """Clearing the import meter removes the billing sensors."""
    _set_meter(hass, IMPORT_METER, 100.0)
    result = await _billing_options(hass, billing_entry)
    assert hass.states.get(ENERGY) is not None
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"billing_cycle_day": 1}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert "import_energy_entity" not in billing_entry.options
    registry = er.async_get(hass)
    for entity_id in BILLING_SENSORS:
        assert registry.async_get(entity_id) is None


async def test_removing_the_entry_deletes_the_meter(
    hass: HomeAssistant,
    billing_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """The stored meter goes with its entry."""
    _set_meter(hass, IMPORT_METER, 100.0)
    await _setup(hass, billing_entry)
    assert await hass.config_entries.async_unload(billing_entry.entry_id)
    key = f"{DOMAIN}.{billing_entry.entry_id}"
    assert key in hass_storage

    await hass.config_entries.async_remove(billing_entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


async def test_import_only_has_no_export_sensors(hass: HomeAssistant) -> None:
    """Without an export meter there are no export billing sensors."""
    options = _options()
    del options["export_energy_entity"]
    entry = MockConfigEntry(
        domain=DOMAIN, title="Home", data=entry_data(), options=options
    )
    entry.add_to_hass(hass)
    _set_meter(hass, IMPORT_METER, 100.0)
    await _setup(hass, entry)
    assert hass.states.get(ENERGY) is not None
    for entity_id in (EXPORT_ENERGY, EXPORT_RATE, EARNINGS):
        assert hass.states.get(entity_id) is None
