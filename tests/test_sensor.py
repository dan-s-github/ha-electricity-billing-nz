"""Test the Electricity Billing NZ sensors."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_UNAVAILABLE
from homeassistant.core import CoreState, HomeAssistant, State
from pytest_homeassistant.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from custom_components.electricity_billing_nz.const import DOMAIN

from .conftest import ECOTRICITY, FORECAST, SETTLED, entry_data
from .helpers import (
    nz,
    set_forecast,
    set_live,
    set_settled,
    template_export,
    template_import,
)

IMPORT = "sensor.home_import_price"
EXPORT = "sensor.home_export_price"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _price(hass: HomeAssistant, entity_id: str) -> float:
    return float(hass.states.get(entity_id).state)


async def test_prices_at_winter_peak(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """Settled prices the last period; live and forecast price the current one."""
    freezer.move_to(nz("2026-07-15 17:10"))
    # Settled holds 16:30-17:00 (off-peak) during the 17:00 period (peak).
    set_settled(hass, 16.0, nz("2026-07-15 16:30"))
    set_live(hass, 12.0, nz("2026-07-15 17:00"))
    set_forecast(hass, nz("2026-07-15 17:00"), [11.0, 10.0, 9.0])
    await _setup(hass, config_entry)

    assert _price(hass, IMPORT) == pytest.approx(
        template_import(0.16, peak=False), abs=1e-6
    )
    assert _price(hass, EXPORT) == pytest.approx(
        template_export(0.16, peak=False), abs=1e-6
    )
    attrs = hass.states.get(IMPORT).attributes
    assert attrs["tou_band"] == "offpeak"
    assert attrs["period_start"] == "2026-07-15T16:30:00+12:00"
    assert attrs["trading_period"] == 34
    assert attrs["spot"] == 0.16
    assert attrs["loss_factor"] == 1.0476
    assert attrs["unit_of_measurement"] == "NZD/kWh"
    assert attrs["state_class"] == "measurement"

    assert _price(hass, "sensor.home_import_price_live") == pytest.approx(
        template_import(0.12, peak=True), abs=1e-6
    )
    assert _price(hass, "sensor.home_export_price_live") == pytest.approx(
        template_export(0.12, peak=True), abs=1e-6
    )

    forecast = hass.states.get("sensor.home_import_price_forecast")
    assert float(forecast.state) == pytest.approx(
        template_import(0.11, peak=True), abs=1e-6
    )
    rows = forecast.attributes["forecast"]
    assert [r["period_start"] for r in rows] == [
        "2026-07-15T17:00:00+12:00",
        "2026-07-15T17:30:00+12:00",
        "2026-07-15T18:00:00+12:00",
    ]
    assert rows[1]["price"] == pytest.approx(template_import(0.10, peak=True), abs=1e-6)
    assert rows[1]["tou_band"] == "peak"

    assert hass.states.get("binary_sensor.home_peak").state == "on"
    assert hass.states.get("sensor.home_tou_band").state == "peak"
    assert (
        hass.states.get("sensor.home_next_band_change").state
        == "2026-07-15T10:00:00+00:00"
    )
    assert _price(hass, "sensor.home_daily_charge") == pytest.approx(1.61)


async def test_band_boundary_without_source_update(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """
    At 22:00 the band changes even if no source updates.

    Prices keep the period their spot is for: the live value is still the
    21:30 (peak) price until the source reports the 22:00 one.
    """
    live = "sensor.home_import_price_live"
    freezer.move_to(nz("2026-07-15 21:50"))
    set_settled(hass, 16.0, nz("2026-07-15 21:00"))
    set_live(hass, 12.0, nz("2026-07-15 21:30"))
    await _setup(hass, config_entry)
    assert hass.states.get("binary_sensor.home_peak").state == "on"

    freezer.move_to(nz("2026-07-15 22:00"))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get("binary_sensor.home_peak").state == "off"
    assert hass.states.get("sensor.home_tou_band").state == "offpeak"
    assert _price(hass, live) == pytest.approx(
        template_import(0.12, peak=True), abs=1e-6
    )
    assert hass.states.get(live).attributes["period_start"] == (
        "2026-07-15T21:30:00+12:00"
    )
    # The settled price is still for 21:00-21:30, which was peak.
    assert hass.states.get(IMPORT).attributes["tou_band"] == "peak"

    set_live(hass, 12.0, nz("2026-07-15 22:00"))
    await hass.async_block_till_done()
    assert _price(hass, live) == pytest.approx(
        template_import(0.12, peak=False), abs=1e-6
    )


async def test_source_updates_recompute(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A new settled price is picked up at once."""
    freezer.move_to(nz("2026-11-16 12:10"))
    set_settled(hass, 10.0, nz("2026-11-16 11:30"))
    await _setup(hass, config_entry)

    set_settled(hass, 20.0, nz("2026-11-16 12:00"))
    await hass.async_block_till_done()

    assert _price(hass, IMPORT) == pytest.approx(
        template_import(0.20, peak=False), abs=1e-6
    )


@pytest.mark.parametrize("state", [STATE_UNAVAILABLE, "unknown"])
async def test_unavailable_spot(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    state: str,
) -> None:
    """No stale price is carried forward when the spot price goes away."""
    freezer.move_to(nz("2026-11-16 12:10"))
    set_settled(hass, 10.0, nz("2026-11-16 11:30"))
    await _setup(hass, config_entry)

    set_settled(hass, state, nz("2026-11-16 11:30"))
    await hass.async_block_till_done()

    assert hass.states.get(IMPORT).state == STATE_UNAVAILABLE
    assert hass.states.get(EXPORT).state == STATE_UNAVAILABLE


async def test_restore_until_source_reports(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """After a restart the last price is kept until the source reports."""
    freezer.move_to(nz("2026-11-16 12:10"))
    hass.set_state(CoreState.starting)
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(
                    IMPORT,
                    "0.3",
                    {"tou_band": "offpeak", "device_class": "stale"},
                ),
                {"native_value": 0.3, "native_unit_of_measurement": "NZD/kWh"},
            )
        ],
    )
    await _setup(hass, config_entry)
    assert hass.states.get(IMPORT).state == "0.3"
    restored = hass.states.get(IMPORT).attributes
    assert restored["tou_band"] == "offpeak"
    assert "device_class" not in restored  # only the price breakdown is restored
    assert hass.states.get(EXPORT).state == STATE_UNAVAILABLE

    set_settled(hass, 10.0, nz("2026-11-16 11:30"))
    await hass.async_block_till_done()
    assert _price(hass, IMPORT) == pytest.approx(
        template_import(0.10, peak=False), abs=1e-6
    )


async def test_no_restore_after_gst_change(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A price saved with the other GST setting isn't restored."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data=entry_data(gst_inclusive=False),
        options={"retailer_versions": [ECOTRICITY], "network_overrides": []},
    )
    entry.add_to_hass(hass)
    freezer.move_to(nz("2026-11-16 12:10"))
    hass.set_state(CoreState.starting)
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(IMPORT, "0.3", {"gst": 1.15}),
                {"native_value": 0.3, "native_unit_of_measurement": "NZD/kWh"},
            )
        ],
    )
    await _setup(hass, entry)
    assert hass.states.get(IMPORT).state == STATE_UNAVAILABLE


async def test_restore_survives_startup_placeholder(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """HA's unavailable placeholder for an unloaded source keeps the restored price."""
    freezer.move_to(nz("2026-11-16 12:10"))
    hass.set_state(CoreState.starting)
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(IMPORT, "0.3", {"tou_band": "offpeak"}),
                {"native_value": 0.3, "native_unit_of_measurement": "NZD/kWh"},
            )
        ],
    )
    await _setup(hass, config_entry)
    hass.states.async_set(SETTLED, STATE_UNAVAILABLE, {"restored": True})
    await hass.async_block_till_done()
    assert hass.states.get(IMPORT).state == "0.3"

    # Once HA has started, a source that still has no value isn't waited for.
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert hass.states.get(IMPORT).state == STATE_UNAVAILABLE


async def test_settled_timestamp_without_offset(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A timestamp with no offset is read in HA's time zone (NZ)."""
    freezer.move_to(nz("2026-07-15 17:40"))
    hass.states.async_set(
        SETTLED,
        "16.0",
        {"unit_of_measurement": "c/kWh", "timestamp": "2026-07-15T17:00:00"},
    )
    await _setup(hass, config_entry)

    attrs = hass.states.get(IMPORT).attributes
    assert attrs["period_start"] == "2026-07-15T17:00:00+12:00"
    assert attrs["tou_band"] == "peak"


@pytest.mark.parametrize(
    "attributes",
    [
        {"history": None, "forecast": None},
        {"history": ["junk"], "forecast": [None, 3]},
        {
            "forecast": [
                {"period_start": "2026-11-16T12:00:00+13:00", "price": [4.0]},
                {"period_start": "2026-11-16T12:30:00+13:00", "price": "nan"},
            ]
        },
        {"forecast": "not a list"},
    ],
)
async def test_malformed_forecast(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    attributes: dict,
) -> None:
    """Bad forecast rows are skipped and the other sensors keep updating."""
    freezer.move_to(nz("2026-11-16 12:10"))
    set_settled(hass, 10.0, nz("2026-11-16 11:30"))
    await _setup(hass, config_entry)

    hass.states.async_set(
        FORECAST, "1.0", {"unit_of_measurement": "c/kWh", **attributes}
    )
    await hass.async_block_till_done()
    set_settled(hass, 20.0, nz("2026-11-16 12:00"))
    await hass.async_block_till_done()

    assert hass.states.get("sensor.home_import_price_forecast").state == (
        STATE_UNAVAILABLE
    )
    assert _price(hass, IMPORT) == pytest.approx(
        template_import(0.20, peak=False), abs=1e-6
    )


async def test_future_rate_version_without_restart(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A retailer version dated ahead takes effect at its date."""
    hass.config_entries.async_update_entry(
        config_entry,
        options={
            **config_entry.options,
            "retailer_versions": [
                *config_entry.options["retailer_versions"],
                {
                    **config_entry.options["retailer_versions"][0],
                    "effective_from": "2026-11-17",
                    "import_margin": 0.04,
                },
            ],
        },
    )
    freezer.move_to(nz("2026-11-16 23:50"))
    set_live(hass, 10.0, nz("2026-11-16 23:30"))
    set_settled(hass, 10.0, nz("2026-11-16 23:00"))
    await _setup(hass, config_entry)
    live = "sensor.home_import_price_live"
    assert hass.states.get(live).attributes["margin"] == 0.028

    freezer.move_to(nz("2026-11-17 00:00"))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    # Still the 23:30 spot, so still the old version.
    assert hass.states.get(live).attributes["margin"] == 0.028

    set_live(hass, 10.0, nz("2026-11-17 00:00"))
    await hass.async_block_till_done()
    assert hass.states.get(live).attributes["margin"] == 0.04
    assert hass.states.get(live).attributes["retailer_version"] == "2026-11-17"


async def test_optional_sources_not_configured(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Live and forecast sensors exist only when their sources are set."""
    data = entry_data()
    del data["live_entity"], data["forecast_entity"]
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data=data,
        options={"retailer_versions": [ECOTRICITY], "network_overrides": []},
    )
    entry.add_to_hass(hass)
    freezer.move_to(nz("2026-11-16 12:10"))
    await _setup(hass, entry)

    assert hass.states.get("sensor.home_import_price_live") is None
    assert hass.states.get("sensor.home_import_price_forecast") is None
    assert hass.states.get(IMPORT) is not None


async def test_settled_without_timestamp_keeps_its_period(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A boundary doesn't re-date an unchanged settled price into the new period."""
    # During the 17:00 period the settled value is for 16:30 (off-peak).
    freezer.move_to(nz("2026-07-15 17:20"))
    hass.states.async_set(SETTLED, "16.0", {"unit_of_measurement": "c/kWh"})
    await _setup(hass, config_entry)
    assert hass.states.get(IMPORT).attributes["period_start"] == (
        "2026-07-15T16:30:00+12:00"
    )

    # Re-dating it at 17:30 would price it as 17:00, which is peak.
    freezer.move_to(nz("2026-07-15 17:30"))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    attrs = hass.states.get(IMPORT).attributes
    assert attrs["period_start"] == "2026-07-15T16:30:00+12:00"
    assert attrs["tou_band"] == "offpeak"


async def test_settled_trading_period_wins_over_timestamp(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A timestamp at the period end still prices the period it's for."""
    freezer.move_to(nz("2026-07-15 17:10"))
    hass.states.async_set(
        SETTLED,
        "16.0",
        {
            "unit_of_measurement": "c/kWh",
            "timestamp": "2026-07-15T17:00:05+12:00",
            "trading_period": 34,
        },
    )
    await _setup(hass, config_entry)

    attrs = hass.states.get(IMPORT).attributes
    assert attrs["period_start"] == "2026-07-15T16:30:00+12:00"
    assert attrs["tou_band"] == "offpeak"


async def test_peak_follows_schedule_peak_bands(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """The peak sensor uses the bands the schedule marks as peak."""
    freezer.move_to(nz("2026-07-15 17:10"))
    await _setup(hass, config_entry)
    assert hass.states.get("binary_sensor.home_peak").state == "on"

    engine = config_entry.runtime_data.engine
    network = engine.tariff.network[-1]
    bands = replace(network.bands, peak=frozenset({"offpeak"}))
    engine.tariff = replace(engine.tariff, network=(replace(network, bands=bands),))
    freezer.move_to(nz("2026-07-15 17:30"))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.home_tou_band").state == "peak"
    assert hass.states.get("binary_sensor.home_peak").state == "off"


async def test_forecast_trading_period_is_validated(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """Forecast rows carry an int trading period, or None if it's not valid."""
    freezer.move_to(nz("2026-11-16 12:10"))
    rows = [
        {
            "period_start": "2026-11-16T12:00:00+13:00",
            "trading_period": "25",
            "price": 1,
        },
        {"period_start": "2026-11-16T12:30:00+13:00", "trading_period": 99, "price": 1},
    ]
    hass.states.async_set(
        FORECAST,
        "1",
        {"unit_of_measurement": "c/kWh", "history": rows[:1], "forecast": rows[1:]},
    )
    await _setup(hass, config_entry)

    forecast = hass.states.get("sensor.home_import_price_forecast").attributes
    assert [r["trading_period"] for r in forecast["forecast"]] == [25, None]


async def test_no_band_change_is_rechecked_daily(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """'No change within the horizon' is searched again the next day."""
    freezer.move_to(nz("2026-07-15 23:10"))
    await _setup(hass, config_entry)
    engine = config_entry.runtime_data.engine
    original = engine.tariff
    network = original.network[-1]
    flat = replace(network, bands=replace(network.bands, rules=()))
    engine.tariff = replace(original, network=(flat,))
    engine._band_change_checked = None  # noqa: SLF001
    freezer.move_to(nz("2026-07-15 23:30"))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.home_next_band_change").state == "unknown"

    engine.tariff = original
    freezer.move_to(nz("2026-07-16 00:00"))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert (
        hass.states.get("sensor.home_next_band_change").state
        == "2026-07-15T19:00:00+00:00"
    )


async def test_rereported_value_moves_to_new_period(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """The same value reported again in a new period is that period's value."""
    attributes = {"unit_of_measurement": "c/kWh"}
    freezer.move_to(nz("2026-07-15 17:20"))
    hass.states.async_set(SETTLED, "16.0", attributes)
    await _setup(hass, config_entry)

    freezer.move_to(nz("2026-07-15 17:30"))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(IMPORT).attributes["period_start"] == (
        "2026-07-15T16:30:00+12:00"
    )

    # Unchanged state and attributes: HA fires state_reported, not changed.
    freezer.move_to(nz("2026-07-15 17:35"))
    hass.states.async_set(SETTLED, "16.0", attributes)
    await hass.async_block_till_done()

    attrs = hass.states.get(IMPORT).attributes
    assert attrs["period_start"] == "2026-07-15T17:00:00+12:00"
    assert attrs["tou_band"] == "peak"


async def test_before_rate_data(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """Before the bundled data starts there's no price, not a too-low one."""
    # Vector's bundled data starts on 2025-04-01.
    freezer.move_to(nz("2025-03-31 22:10"))
    set_settled(hass, 16.0, nz("2025-03-31 21:30"))
    set_forecast(hass, nz("2025-03-31 22:00"), [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    await _setup(hass, config_entry)

    assert hass.states.get(IMPORT).state == STATE_UNAVAILABLE
    assert hass.states.get(EXPORT).state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.home_daily_charge").state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.home_tou_band").state == STATE_UNAVAILABLE
    assert hass.states.get("binary_sensor.home_peak").state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.home_next_band_change").state == STATE_UNAVAILABLE
    # Only periods from 2025-04-01 are priced; none of them is "now", but
    # the list is still readable.
    forecast = "sensor.home_import_price_forecast"
    assert hass.states.get(forecast).state == "unknown"
    assert [
        r["period_start"] for r in hass.states.get(forecast).attributes["forecast"]
    ] == ["2025-04-01T00:00:00+13:00", "2025-04-01T00:30:00+13:00"]

    # 23:30 is listed but can't be priced: 00:00 mustn't stand in for it.
    freezer.move_to(nz("2025-03-31 23:40"))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(forecast).state == "unknown"

    freezer.move_to(nz("2025-04-01 00:00"))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    # Vector 2025: $0.75/day, off-peak 0.0444, VECA1 1.0533.
    assert _price(hass, "sensor.home_daily_charge") == pytest.approx(
        (0.75 + 0.35 + 0.15) * 1.15
    )
    assert float(
        hass.states.get("sensor.home_import_price_forecast").state
    ) == pytest.approx(template_import(0.05, network=0.0444, loss=1.0533), abs=1e-6)


async def test_forecast_current_row(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """Off-the-half-hour stamps find their period; a null price isn't skipped over."""
    forecast = "sensor.home_import_price_forecast"
    freezer.move_to(nz("2026-11-16 12:10"))
    rows = [
        {"period_start": "2026-11-16T12:00:00.500+13:00", "price": 4.0},
        {"period_start": "2026-11-16T12:30:00+13:00", "price": 5.0},
    ]
    hass.states.async_set(
        FORECAST,
        "4",
        {"unit_of_measurement": "c/kWh", "history": rows[:1], "forecast": rows[1:]},
    )
    await _setup(hass, config_entry)
    assert float(hass.states.get(forecast).state) == pytest.approx(
        template_import(0.04, peak=False), abs=1e-6
    )
    assert hass.states.get(forecast).attributes["forecast"][0]["period_start"] == (
        "2026-11-16T12:00:00+13:00"
    )

    # The current period is listed without a price yet: no stand-in.
    rows[0]["price"] = None
    hass.states.async_set(
        FORECAST,
        "5",
        {"unit_of_measurement": "c/kWh", "history": rows[:1], "forecast": rows[1:]},
    )
    await hass.async_block_till_done()
    assert hass.states.get(forecast).state == "unknown"


async def test_forecast_rows_stamped_with_period_end(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """With a trading period, a row stamped with its period's end is still its own."""
    forecast = "sensor.home_import_price_forecast"
    freezer.move_to(nz("2026-11-16 12:10"))
    rows = [
        # Periods 25 (12:00) and 26 (12:30), stamped 12:30 and 13:00.
        {"period_start": "2026-11-16T12:30:00+13:00", "trading_period": 25, "price": 4},
        {"period_start": "2026-11-16T13:00:00+13:00", "trading_period": 26, "price": 5},
    ]
    hass.states.async_set(
        FORECAST,
        "4",
        {"unit_of_measurement": "c/kWh", "history": rows[:1], "forecast": rows[1:]},
    )
    await _setup(hass, config_entry)

    assert float(hass.states.get(forecast).state) == pytest.approx(
        template_import(0.04, peak=False), abs=1e-6
    )
    assert [
        (r["period_start"], r["trading_period"])
        for r in hass.states.get(forecast).attributes["forecast"]
    ] == [("2026-11-16T12:00:00+13:00", 25), ("2026-11-16T12:30:00+13:00", 26)]


async def test_forecast_trading_period_only_corrects_period_end(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A trading period that disagrees with the stamp otherwise is dropped."""
    freezer.move_to(nz("2026-11-16 12:10"))
    rows = [
        {"period_start": "2026-11-16T12:00:00+13:00", "trading_period": 25, "price": 4},
        # Period 30 is 14:30: not this stamp's period or the one before it.
        {"period_start": "2026-11-16T12:30:00+13:00", "trading_period": 30, "price": 5},
    ]
    hass.states.async_set(
        FORECAST,
        "4",
        {"unit_of_measurement": "c/kWh", "history": rows[:1], "forecast": rows[1:]},
    )
    await _setup(hass, config_entry)

    assert [
        (r["period_start"], r["trading_period"])
        for r in hass.states.get("sensor.home_import_price_forecast").attributes[
            "forecast"
        ]
    ] == [("2026-11-16T12:00:00+13:00", 25), ("2026-11-16T12:30:00+13:00", None)]


async def test_forecast_period_end_stamps_as_dst_ends(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """Period-end stamps with trading periods land right in the repeated hour."""
    # 2027-04-04: 02:00-03:00 repeats; periods 5-6 are NZDT, 7-8 NZST.
    freezer.move_to(datetime(2027, 4, 3, 12, 40, tzinfo=UTC))  # 01:40 NZDT
    ends = [
        "2027-04-04T02:00:00+13:00",
        "2027-04-04T02:30:00+13:00",
        "2027-04-04T02:00:00+12:00",
        "2027-04-04T02:30:00+12:00",
        "2027-04-04T03:00:00+12:00",
    ]
    rows = [
        {"period_start": end, "trading_period": 4 + i, "price": float(i)}
        for i, end in enumerate(ends)
    ]
    hass.states.async_set(
        FORECAST, "0", {"unit_of_measurement": "c/kWh", "forecast": rows}
    )
    await _setup(hass, config_entry)

    forecast = hass.states.get("sensor.home_import_price_forecast")
    assert [
        (r["period_start"], r["trading_period"])
        for r in forecast.attributes["forecast"]
    ] == [
        ("2027-04-04T01:30:00+13:00", 4),
        ("2027-04-04T02:00:00+13:00", 5),
        ("2027-04-04T02:30:00+13:00", 6),
        ("2027-04-04T02:00:00+12:00", 7),
        ("2027-04-04T02:30:00+12:00", 8),
    ]


async def test_forecast_drops_first_pass_of_repeated_hour(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """On the second 02:00 as DST ends, the first-pass periods are past."""
    # 2027-04-04 02:10 NZST (second pass) is 14:10 UTC.
    freezer.move_to(datetime(2027, 4, 3, 14, 10, tzinfo=UTC))
    rows = [
        {"period_start": "2027-04-04T02:00:00+13:00", "price": 1.0},
        {"period_start": "2027-04-04T02:30:00+13:00", "price": 2.0},
        {"period_start": "2027-04-04T02:00:00+12:00", "price": 3.0},
        {"period_start": "2027-04-04T02:30:00+12:00", "price": 4.0},
    ]
    hass.states.async_set(
        FORECAST, "3", {"unit_of_measurement": "c/kWh", "forecast": rows}
    )
    await _setup(hass, config_entry)

    forecast = hass.states.get("sensor.home_import_price_forecast")
    assert [r["period_start"] for r in forecast.attributes["forecast"]] == [
        "2027-04-04T02:00:00+12:00",
        "2027-04-04T02:30:00+12:00",
    ]
