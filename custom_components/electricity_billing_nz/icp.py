"""ICP identifiers: the optional unique id of a config entry."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from homeassistant.config_entries import SOURCE_IGNORE
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

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
