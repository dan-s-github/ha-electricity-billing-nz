"""ICP identifiers: an entry's optional unique id, and its registry record."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.config_entries import SOURCE_IGNORE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# The Electricity Authority's public "Your meter" page. There's no keyless
# API, so this reads the page; any failure just leaves the form unfilled.
# load_more=True adds the network pricing table.
REGISTRY_URL = "https://www.ea.govt.nz/your-power/your-meter/icp/"
# Short: the setup form waits on it.
LOOKUP_TIMEOUT = 10
# A grid point of connection, e.g. ROS0221.
POC_PATTERN = re.compile(r"[A-Z]{3}[0-9]{4}", re.ASCII)

# 10 digits, the 2-letter distributor code, then 3 letters or digits. Only
# the shape is checked, not the checksum. re.ASCII, and matched before
# upper-casing: otherwise non-ASCII digits, or letters that upper-case to
# ASCII (the "ﬀ" ligature), would store a second spelling of the same ICP.
ICP_PATTERN = re.compile(r"[0-9]{10}[A-Z]{2}[0-9A-Z]{3}", re.ASCII | re.IGNORECASE)


def normalise_icp(text: str | None) -> str | None:
    """
    Return the ICP in canonical form, or None if none was entered.

    Raises ValueError if it isn't an ICP identifier.
    """
    icp = re.sub(r"[\s-]", "", text or "")
    if not icp:
        return None
    if not ICP_PATTERN.fullmatch(icp):
        msg = f"Not an ICP identifier: {text!r}"
        raise ValueError(msg)
    return icp.upper()


@callback
def async_validate_icp(
    hass: HomeAssistant, text: str | None, entry: ConfigEntry | None = None
) -> tuple[str | None, str | None]:
    """Return (ICP or None, error key or None) for a form's ICP field."""
    try:
        icp = normalise_icp(text)
    except ValueError:
        return None, "invalid_icp"
    if icp and (error := async_icp_holder(hass, icp, entry)):
        return None, error
    return icp, None


@callback
def async_icp_holder(
    hass: HomeAssistant, icp: str, entry: ConfigEntry | None = None
) -> str | None:
    """
    Return an error key if another entry or a setup in progress has this ICP.

    Ignored entries don't count: like a finished config flow, setting the
    ICP in the options removes them (async_remove_ignored).
    """
    for other in hass.config_entries.async_entries(DOMAIN, include_ignore=False):
        if other.unique_id == icp and (
            entry is None or other.entry_id != entry.entry_id
        ):
            return "icp_in_use"
    if any(
        flow["context"].get("unique_id") == icp
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    ):
        return "icp_in_setup"
    return None


async def async_remove_ignored(hass: HomeAssistant, icp: str) -> None:
    """Remove ignored entries for this ICP, as a finished config flow would."""
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.unique_id == icp and other.source == SOURCE_IGNORE:
            await hass.config_entries.async_remove(other.entry_id)


@dataclass(frozen=True)
class RegistryRecord:
    """The fields of an ICP's registry record the setup uses."""

    network: str | None
    poc: str | None
    price_category: str | None
    loss_category: str | None


# Row label on the page -> RegistryRecord field.
_REGISTRY_LABELS = {
    "network participant": "network",
    "poc": "poc",
    "distributor price category code": "price_category",
    "distributor loss category code": "loss_category",
}


class _RowParser(HTMLParser):
    """Collect each table row's cell texts."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: object) -> None:  # noqa: ARG002
        if tag == "tr":
            self.rows.append([])
        elif tag in ("th", "td") and self.rows and self._cell is None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("th", "td") and self._cell is not None:
            self.rows[-1].append(" ".join("".join(self._cell).split()))
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def parse_registry_page(text: str) -> RegistryRecord | None:
    """Return the record on a "Your meter" page, or None if it has none."""
    parser = _RowParser()
    parser.feed(text)
    fields: dict[str, str | None] = dict.fromkeys(_REGISTRY_LABELS.values())
    for row in parser.rows:
        # Some rows end in an empty cell: the value is always the second.
        if len(row) < 2:  # noqa: PLR2004
            continue
        name = _REGISTRY_LABELS.get(row[0].casefold())
        if name and fields[name] is None and row[1]:
            fields[name] = row[1]
    # "VECT - Vector Limited": the participant code is what's matched.
    if fields["network"]:
        fields["network"] = fields["network"].split(" - ")[0].strip().upper()
    # It's shown in a warning, so only a well-formed node code is kept.
    if fields["poc"] and not POC_PATTERN.fullmatch(fields["poc"].upper()):
        fields["poc"] = None
    if not any(fields.values()):
        return None
    return RegistryRecord(**fields)


async def async_lookup_icp(hass: HomeAssistant, icp: str) -> RegistryRecord | None:
    """Return the ICP's registry record, or None if it can't be read."""
    session = async_get_clientsession(hass)
    try:
        async with asyncio.timeout(LOOKUP_TIMEOUT):
            response = await session.get(
                REGISTRY_URL,
                params={"icp": icp, "load_more": "True"},
                raise_for_status=True,
            )
            text = await response.text(errors="replace")
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.debug("Registry lookup for %s failed: %s", icp, err)
        return None
    if (record := parse_registry_page(text)) is None:
        _LOGGER.debug("Registry page for %s has no record", icp)
    return record
