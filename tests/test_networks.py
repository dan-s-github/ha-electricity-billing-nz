"""Test the bundled network data and user overrides."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.electricity_billing_nz.networks import (
    NetworkDataError,
    apply_loss_overrides,
    apply_network_overrides,
    async_get_networks,
    load_networks,
    parse_network,
)
from custom_components.electricity_billing_nz.tariff import version_on

NETWORKS = load_networks()


def test_every_bundled_network_is_valid() -> None:
    """Every category prices every band its schedule can return."""
    assert NETWORKS
    for network in NETWORKS.values():
        for versions in network.categories.values():
            for version in versions:
                assert set(version.bands.bands) <= set(version.import_rates)
                assert version.source
        for versions in network.loss_categories.values():
            assert all(v.factor >= 1 for v in versions)


def test_vector_2026_values() -> None:
    """Vector v2026.1 as transcribed from the schedule."""
    vector = NETWORKS["vector"]
    arhlc = version_on(vector.categories["ARHLC"], date(2026, 4, 1))
    assert arhlc.effective_from == date(2026, 4, 1)
    assert arhlc.daily == 0.90
    assert arhlc.import_rates == {"peak": 0.1504, "offpeak": 0.0457}
    assert arhlc.export_credit == {"peak": 0.0524, "offpeak": 0.0}
    assert version_on(vector.categories["ARHLU"], date(2026, 4, 1)).import_rates == {
        "peak": 0.1513,
        "offpeak": 0.0466,
    }
    assert (
        version_on(vector.loss_categories["VECA1"], date(2026, 4, 1)).factor == 1.0476
    )


def test_vector_2025_values() -> None:
    """Vector v2025.1 as transcribed from the schedule."""
    vector = NETWORKS["vector"]
    arhlc = version_on(vector.categories["ARHLC"], date(2025, 4, 1))
    assert arhlc.effective_from == date(2025, 4, 1)
    assert arhlc.daily == 0.75
    assert arhlc.import_rates == {"peak": 0.1575, "offpeak": 0.0444}
    assert arhlc.export_credit == {}  # no injection credit in 2025
    assert arhlc.bands.name == "vector_tou_2025"
    on_2025 = date(2025, 4, 1)
    assert (
        version_on(vector.categories["ARHLU"], on_2025).import_rates["peak"] == 0.1585
    )
    assert version_on(vector.loss_categories["VECA1"], on_2025).factor == 1.0533
    assert arhlc.source.endswith("effectivefrom1april2025.pdf")
    assert vector.participants == {"VECT"}


def _raw(**category: object) -> dict[str, object]:
    return {
        "distributor": "test",
        "categories": {
            "X": [
                {
                    "effective_from": "2026-04-01",
                    "daily": 1,
                    "import": {"peak": 0.2, "offpeak": 0.1},
                    "bands": "tou",
                    **category,
                }
            ]
        },
        "loss_categories": {"L": [{"effective_from": "2026-04-01", "factor": 1.05}]},
        "bands": {
            "tou": {
                "default": "offpeak",
                "rules": [{"band": "peak", "months": [6], "windows": ["17:00-21:00"]}],
            }
        },
    }


def test_missing_band_rate_is_rejected() -> None:
    """A category must price every band."""
    with pytest.raises(NetworkDataError, match="no import rate"):
        parse_network(_raw(**{"import": {"offpeak": 0.1}}))


@pytest.mark.parametrize(
    "window", ["17:15-21:00", "21:00-17:00", "17:00-25:00", "17:00-21:00-22:00", "5pm"]
)
def test_bad_window_is_rejected(window: str) -> None:
    """Windows sit on half hours and run forwards within the day."""
    raw = _raw()
    raw["bands"]["tou"]["rules"][0]["windows"] = [window]  # type: ignore[index]
    with pytest.raises(NetworkDataError):
        parse_network(raw)


def test_override_lasts_until_next_bundled_version() -> None:
    """An override replaces fields from its date until the next bundled version."""
    network = parse_network(_raw())
    later = parse_network(
        _raw(
            effective_from="2027-04-01",
            daily=2,
            **{"import": {"peak": 0.3, "offpeak": 0.15}},
        )
    )
    bundled = network.categories["X"] + later.categories["X"]
    merged = apply_network_overrides(
        bundled,
        [{"effective_from": "2026-06-01", "import_peak": 0.25, "daily": None}],
    )
    assert version_on(merged, date(2026, 5, 31)).import_rates["peak"] == 0.2
    assert version_on(merged, date(2026, 6, 1)).import_rates["peak"] == 0.25
    assert version_on(merged, date(2026, 6, 1)).daily == 1
    assert version_on(merged, date(2027, 4, 1)).import_rates["peak"] == 0.3


def test_override_before_bundled_data_is_ignored() -> None:
    """There are no bands before the first bundled version."""
    bundled = parse_network(_raw()).categories["X"]
    merged = apply_network_overrides(
        bundled, [{"effective_from": "2025-01-01", "import_peak": 0.25}]
    )
    assert version_on(merged, date(2025, 6, 1)) is None
    assert version_on(merged, date(2026, 6, 1)).import_rates["peak"] == 0.2


def test_loss_override() -> None:
    """Loss factor overrides follow the same rules."""
    bundled = parse_network(_raw()).loss_categories["L"]
    merged = apply_loss_overrides(
        bundled, [{"effective_from": "2026-07-01", "loss_factor": 1.06}]
    )
    assert version_on(merged, date(2026, 6, 30)).factor == 1.05
    assert version_on(merged, date(2026, 7, 1)).factor == 1.06


def test_bad_value_is_wrapped() -> None:
    """A value that won't convert names the problem as network data."""
    with pytest.raises(NetworkDataError, match="Malformed"):
        parse_network(_raw(daily="lots"))


@pytest.mark.parametrize(
    ("participants", "expected"),
    [(["vect", "UNET"], {"VECT", "UNET"}), (None, set())],
)
def test_participants(participants: list[str] | None, expected: set[str]) -> None:
    """Registry participant codes are upper-cased; none is allowed."""
    raw = {**_raw(), "participants": participants}
    assert parse_network(raw).participants == expected


def test_participants_must_be_a_list() -> None:
    """A bare code would otherwise become a set of letters."""
    with pytest.raises(NetworkDataError, match="participants"):
        parse_network({**_raw(), "participants": "VECT"})


def test_bad_file_is_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """One malformed file doesn't stop the other networks loading."""
    (tmp_path / "bad.yaml").write_text("distributor: bad\ncategories: {}\n")
    (tmp_path / "broken.yaml").write_text("distributor: [unclosed\n")
    good = Path(__file__).parents[1] / (
        "custom_components/electricity_billing_nz/networks/vector.yaml"
    )
    (tmp_path / "vector.yaml").write_text(good.read_text())

    assert list(load_networks(tmp_path)) == ["vector"]
    assert "bad.yaml" in caplog.text
    assert "broken.yaml" in caplog.text


def test_peak_bands() -> None:
    """Schedules name their peak bands; the default is the band 'peak'."""
    assert parse_network(_raw()).categories["X"][0].bands.peak == {"peak"}
    raw = _raw()
    raw["bands"]["tou"]["peak"] = ["offpeak"]  # type: ignore[index]
    assert parse_network(raw).categories["X"][0].bands.peak == {"offpeak"}
    raw["bands"]["tou"]["peak"] = ["nope"]  # type: ignore[index]
    with pytest.raises(NetworkDataError, match="unknown bands"):
        parse_network(raw)


def test_loss_only_override_adds_no_network_version() -> None:
    """A loss factor override leaves the network versions alone."""
    bundled = parse_network(_raw()).categories["X"]
    override = [{"effective_from": "2026-07-01", "loss_factor": 1.06}]
    assert apply_network_overrides(bundled, override) == bundled


async def test_concurrent_loads_share_one_read(hass: HomeAssistant) -> None:
    """Entries set up together share one load of the network files."""
    with patch(
        "custom_components.electricity_billing_nz.networks.load_networks",
        return_value=NETWORKS,
    ) as load:
        first, second = await asyncio.gather(
            async_get_networks(hass), async_get_networks(hass)
        )
    assert first is second
    assert load.call_count == 1
