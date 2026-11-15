"""Bundled network (lines company) rate data."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .const import CONF_DISTRIBUTOR, CONF_LOSS_CATEGORY, CONF_PRICE_CATEGORY
from .tariff import (
    DEFAULT_PEAK_BAND,
    BandRule,
    BandSchedule,
    LossVersion,
    NetworkVersion,
    version_on,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

NETWORKS_DIR = Path(__file__).parent / "networks"
DATA_NETWORKS = "electricity_billing_nz_networks"

# Override keys: "daily", "loss_factor", "import_<band>", "export_credit_<band>".
OVERRIDE_DAILY = "daily"
OVERRIDE_LOSS_FACTOR = "loss_factor"
OVERRIDE_IMPORT_PREFIX = "import_"
OVERRIDE_EXPORT_CREDIT_PREFIX = "export_credit_"


class NetworkDataError(ValueError):
    """A bundled network file is malformed."""


@dataclass(frozen=True)
class Network:
    """One lines company's bundled price categories and loss categories."""

    key: str
    name: str
    categories: Mapping[str, tuple[NetworkVersion, ...]]
    loss_categories: Mapping[str, tuple[LossVersion, ...]]


def _minutes(text: str) -> int:
    hours, minutes = text.strip().split(":")
    value = int(hours) * 60 + int(minutes)
    if not 0 <= value <= 24 * 60 or int(minutes) not in (0, 30):
        msg = f"Window edge must be on a half hour: {text!r}"
        raise NetworkDataError(msg)
    return value


def _parse_window(text: str) -> tuple[int, int]:
    parts = text.split("-")
    if len(parts) != 2:  # noqa: PLR2004
        msg = f"Window must be 'HH:MM-HH:MM': {text!r}"
        raise NetworkDataError(msg)
    start, end = (_minutes(part) for part in parts)
    if start >= end:
        msg = f"Window must end after it starts: {text!r}"
        raise NetworkDataError(msg)
    return start, end


def _parse_bands(name: str, raw: Mapping[str, Any]) -> BandSchedule:
    rules = []
    for rule in raw.get("rules", []):
        weekdays = rule.get("weekdays")
        rules.append(
            BandRule(
                band=str(rule["band"]),
                months=frozenset(int(m) for m in rule["months"]),
                windows=tuple(_parse_window(w) for w in rule["windows"]),
                weekdays=frozenset(int(d) for d in weekdays)
                if weekdays is not None
                else None,
                holidays=rule.get("holidays"),
            )
        )
    schedule = BandSchedule(
        name=name,
        rules=tuple(rules),
        default=str(raw["default"]),
        peak=frozenset(str(b) for b in raw.get("peak", [DEFAULT_PEAK_BAND])),
    )
    if unknown := schedule.peak - set(schedule.bands) - {DEFAULT_PEAK_BAND}:
        msg = f"{name}: peak lists unknown bands {sorted(unknown)}"
        raise NetworkDataError(msg)
    return schedule


def _as_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _sorted_versions[V: (NetworkVersion, LossVersion)](
    label: str, versions: list[V]
) -> tuple[V, ...]:
    versions.sort(key=lambda v: v.effective_from)
    dates = [v.effective_from for v in versions]
    if len(set(dates)) != len(dates):
        msg = f"{label}: two versions share an effective date"
        raise NetworkDataError(msg)
    return tuple(versions)


def parse_network(raw: Mapping[str, Any]) -> Network:
    """Validate one network file's contents and convert it to dataclasses."""
    try:
        key = str(raw["distributor"])
        sources: Mapping[str, str] = raw.get("sources", {})
        schedules = {
            name: _parse_bands(name, body) for name, body in raw["bands"].items()
        }
        categories: dict[str, tuple[NetworkVersion, ...]] = {}
        for code, versions in raw["categories"].items():
            parsed = []
            for v in versions:
                bands = schedules[v["bands"]]
                import_rates = {b: float(r) for b, r in v["import"].items()}
                missing = set(bands.bands) - set(import_rates)
                if missing:
                    msg = f"{key} {code}: no import rate for {sorted(missing)}"
                    raise NetworkDataError(msg)
                parsed.append(
                    NetworkVersion(
                        effective_from=_as_date(v["effective_from"]),
                        daily=float(v["daily"]),
                        import_rates=import_rates,
                        export_credit={
                            b: float(r) for b, r in v.get("export_credit", {}).items()
                        },
                        bands=bands,
                        source=sources.get(v.get("schedule_version", "")),
                    )
                )
            categories[code] = _sorted_versions(f"{key} {code}", parsed)
        loss_categories = {
            code: _sorted_versions(
                f"{key} {code}",
                [
                    LossVersion(
                        effective_from=_as_date(v["effective_from"]),
                        factor=float(v["factor"]),
                        source=sources.get(v.get("schedule_version", "")),
                    )
                    for v in versions
                ],
            )
            for code, versions in raw["loss_categories"].items()
        }
    except (KeyError, TypeError, AttributeError, ValueError) as err:
        if isinstance(err, NetworkDataError):
            raise
        msg = f"Malformed network data: {err!r}"
        raise NetworkDataError(msg) from err
    return Network(
        key=key,
        name=str(raw.get("name", key)),
        categories=categories,
        loss_categories=loss_categories,
    )


def load_networks(directory: Path = NETWORKS_DIR) -> dict[str, Network]:
    """Load every bundled network file (blocking I/O); bad files are skipped."""
    networks = {}
    for path in sorted(directory.glob("*.yaml")):
        try:
            with path.open(encoding="utf-8") as handle:
                network = parse_network(yaml.safe_load(handle))
        except (OSError, yaml.YAMLError, NetworkDataError) as err:
            _LOGGER.error("Skipping network file %s: %s", path.name, err)  # noqa: TRY400
            continue
        networks[network.key] = network
    return networks


async def async_get_networks(hass: HomeAssistant) -> dict[str, Network]:
    """Return the bundled networks, loading them once per HA run."""
    # Keep the future, so entries set up together share one load.
    if (loading := hass.data.get(DATA_NETWORKS)) is None:
        loading = hass.data[DATA_NETWORKS] = hass.async_add_executor_job(load_networks)
    return await loading


@dataclass(frozen=True)
class EntryNetwork:
    """The bundled data one config entry prices with."""

    network: Network
    versions: tuple[NetworkVersion, ...]
    losses: tuple[LossVersion, ...]


def network_for_entry(
    networks: Mapping[str, Network], data: Mapping[str, Any]
) -> EntryNetwork | None:
    """
    Return the entry's network, price category and loss category.

    None if any is missing from the bundled data, including for entries made
    before these fields existed.
    """
    network = networks.get(data.get(CONF_DISTRIBUTOR, ""))
    if network is None:
        return None
    versions = network.categories.get(data.get(CONF_PRICE_CATEGORY, ""))
    losses = network.loss_categories.get(data.get(CONF_LOSS_CATEGORY, ""))
    if not versions or not losses:
        return None
    return EntryNetwork(network=network, versions=versions, losses=losses)


def _merge_overrides[V: (NetworkVersion, LossVersion)](
    versions: tuple[V, ...],
    overrides: list[Mapping[str, Any]],
    apply: Callable[[V, Mapping[str, Any]], V],
) -> tuple[V, ...]:
    """
    Merge overrides into bundled versions.

    An override replaces fields from its date until the next bundled version
    takes effect, so a new bundled schedule supersedes it. Overrides dated
    before the first bundled version are ignored (there is nothing to change).
    """
    if not overrides:
        return versions
    ordered = sorted(overrides, key=lambda o: _as_date(o["effective_from"]))
    starts = [_as_date(o["effective_from"]) for o in ordered]
    merged = []
    for day in sorted({v.effective_from for v in versions} | set(starts)):
        base = version_on(versions, day)
        if base is None:
            continue
        version = base
        for override, start in zip(ordered, starts, strict=True):
            if base.effective_from <= start <= day:
                version = apply(version, override)
        merged.append(replace(version, effective_from=day))
    return tuple(merged)


def _is_network_field(name: str) -> bool:
    return name == OVERRIDE_DAILY or name.startswith(
        (OVERRIDE_IMPORT_PREFIX, OVERRIDE_EXPORT_CREDIT_PREFIX)
    )


def _apply_network(
    version: NetworkVersion, override: Mapping[str, Any]
) -> NetworkVersion:
    daily = version.daily
    import_rates = dict(version.import_rates)
    export_credit = dict(version.export_credit)
    for name, value in override.items():
        if value is None:
            continue
        if name == OVERRIDE_DAILY:
            daily = float(value)
        elif name.startswith(OVERRIDE_EXPORT_CREDIT_PREFIX):
            export_credit[name.removeprefix(OVERRIDE_EXPORT_CREDIT_PREFIX)] = float(
                value
            )
        elif name.startswith(OVERRIDE_IMPORT_PREFIX):
            import_rates[name.removeprefix(OVERRIDE_IMPORT_PREFIX)] = float(value)
    return replace(
        version, daily=daily, import_rates=import_rates, export_credit=export_credit
    )


def apply_network_overrides(
    versions: tuple[NetworkVersion, ...], overrides: list[Mapping[str, Any]]
) -> tuple[NetworkVersion, ...]:
    """Merge the overrides that change network values into bundled versions."""
    relevant = [
        o
        for o in overrides
        if any(_is_network_field(k) and v is not None for k, v in o.items())
    ]
    return _merge_overrides(versions, relevant, _apply_network)


def apply_loss_overrides(
    versions: tuple[LossVersion, ...], overrides: list[Mapping[str, Any]]
) -> tuple[LossVersion, ...]:
    """Merge loss-factor overrides, with the same rules as the network."""
    relevant = [o for o in overrides if o.get(OVERRIDE_LOSS_FACTOR) is not None]
    return _merge_overrides(
        versions,
        relevant,
        lambda version, o: replace(version, factor=float(o[OVERRIDE_LOSS_FACTOR])),
    )
