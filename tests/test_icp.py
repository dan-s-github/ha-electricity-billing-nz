"""Test the ICP registry lookup."""

from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.electricity_billing_nz.icp import (
    REGISTRY_URL,
    RegistryRecord,
    async_lookup_icp,
    parse_registry_page,
)

from .conftest import ICP

PAGE = (Path(__file__).parent / "fixtures" / "registry_icp.html").read_text()
PARAMS = {"icp": ICP, "load_more": "True"}


def test_parse_registry_page() -> None:
    """The network and category rows are read; the network keeps its code."""
    assert parse_registry_page(PAGE) == RegistryRecord(
        network="VECT", poc="ROS0221", price_category="ARHLC", loss_category="VECA1"
    )


def test_parse_tolerates_layout_changes() -> None:
    """Extra cells, lower case and nested markup still parse."""
    page = """
        <table><tr><th>Network participant</th><th>vect - Vector</th><th></th></tr>
        <tr><th>POC</th><th><span>ros0221</span></th></tr>
        <tr><th>Distributor price category code</th><td> arhlc </td></tr></table>
    """
    assert parse_registry_page(page) == RegistryRecord(
        network="VECT", poc="ROS0221", price_category="ARHLC", loss_category=None
    )


def test_parse_drops_malformed_poc() -> None:
    """The POC is shown in a warning, so only a node code is kept."""
    page = "<table><tr><th>POC</th><th>[click](http://x)</th></tr></table>"
    assert parse_registry_page(page) is None


@pytest.mark.parametrize(
    "page",
    [
        # What the page shows for an unknown ICP.
        "<main><h2>ICP number not found</h2><p>0000000000XX000</p></main>",
        "",
    ],
)
def test_parse_page_without_record(page: str) -> None:
    """A page without the registry rows has no record."""
    assert parse_registry_page(page) is None


async def test_lookup(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The lookup asks for the full record and parses the page."""
    aioclient_mock.get(REGISTRY_URL, params=PARAMS, text=PAGE)
    record = await async_lookup_icp(hass, ICP)
    assert record is not None
    assert record.price_category == "ARHLC"


@pytest.mark.parametrize(
    "response", [{"status": 502}, {"exc": TimeoutError()}, {"text": "<p>Down</p>"}]
)
async def test_lookup_failure(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, response: dict
) -> None:
    """Any failure, including a page without a record, finds nothing."""
    aioclient_mock.get(REGISTRY_URL, params=PARAMS, **response)
    assert await async_lookup_icp(hass, ICP) is None
