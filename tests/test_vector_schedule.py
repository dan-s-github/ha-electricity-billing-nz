"""Test the Vector schedule updater in tools/vector_schedule.py."""

from __future__ import annotations

import shutil
import subprocess
import urllib.error
from datetime import date
from email.message import Message
from pathlib import Path

import pytest
import yaml

from custom_components.electricity_billing_nz.networks import parse_network
from custom_components.electricity_billing_nz.tariff import version_on
from tools import vector_schedule
from tools.vector_schedule import (
    VECTOR_YAML,
    LayoutError,
    add_schedule,
    find_schedules,
    parse_schedule,
)

from .conftest import NETWORK_SNAPSHOT

FIXTURES = Path(__file__).parent / "fixtures"
# pdftotext -layout output of the 2025 and 2026 schedules.
TEXT_2026 = (FIXTURES / "vector_2026.txt").read_text()
TEXT_2025 = (FIXTURES / "vector_2025.txt").read_text()
DOCS = "https://media.vector.co.nz/production/documents/"
URL_2026 = f"{DOCS}electricitypricescheduleseffectivefrom1april2026.pdf"
URL_2027 = f"{DOCS}electricitypricescheduleseffectivefrom1april2027.pdf"
URL_OCT_2026 = f"{DOCS}electricitypricescheduleseffectivefrom1october2026.pdf"


def _text(
    effective: str = "1 April 2027",
    version: str = "v2027.1",
    *,
    morning: str = "07:00 to 11:00",
) -> str:
    """Return the 2026 text as if it were a later schedule with a new peak rate."""
    return (
        TEXT_2026.replace("1 April 2026", effective)
        .replace("Version:          v2026.1", f"Version:          {version}")
        .replace("0.1504", "0.1600")
        .replace("07:00 to 11:00 for months", f"{morning} for months")
    )


def _text_2027(*, morning: str = "07:00 to 11:00") -> str:
    return _text(morning=morning)


@pytest.fixture
def network_file(tmp_path: Path) -> Path:
    """Return a copy of the frozen vector.yaml, which ends at v2026.1."""
    path = tmp_path / "vector.yaml"
    shutil.copy(NETWORK_SNAPSHOT / "vector.yaml", path)
    return path


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_parse_matches_bundled_2026() -> None:
    """The 2026 schedule parses to the values transcribed by hand."""
    schedule = parse_schedule(TEXT_2026, "url")
    bundled = parse_network(_load(VECTOR_YAML))
    assert schedule.effective_from == date(2026, 4, 1)
    for code, values in schedule.categories.items():
        version = version_on(bundled.categories[code], schedule.effective_from)
        assert values["daily"] == version.daily
        assert values["import"] == version.import_rates
        assert values["export_credit"] == version.export_credit
    loss = version_on(bundled.loss_categories["VECA1"], schedule.effective_from)
    assert schedule.losses == {"VECA1": loss.factor}
    raw_rules = _load(VECTOR_YAML)["bands"]["vector_tou_2026"]
    assert vector_schedule.same_rules(raw_rules, schedule.rules)


def test_parse_rejects_2025_layout() -> None:
    """2025's separate summer and winter peak columns need a person."""
    with pytest.raises(LayoutError, match="columns"):
        parse_schedule(TEXT_2025, "url")


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (("Effective:", "Updated:"), "Effective"),
        (("ARHLU ", "ARHLX "), "ARHLU"),
        (("VECA1          ", "VECA9          "), "VECA1"),
        (("for months May to September", "for May to September"), "wording"),
        (
            ("for months May to September", "for months May to September weekdays"),
            "wording",
        ),
        (("Version:  ", "Revision:"), "Version"),
        # A qualifier on the definition's next line in its column.
        (
            (
                "                                    inclusive",
                "                                    inclusive except public holidays",
            ),
            "wording",
        ),
        (
            (
                "(including public holidays) in months",
                "(excluding public holidays) in months",
            ),
            "public holidays",
        ),
        (("Effective:        1 April 2026", "Effective:        1 Apr 2026"), "date"),
        (
            (
                "(including public holidays) in months",
                "(not including public holidays) in months",
            ),
            "holidays",
        ),
        (
            (
                "Monday to Sunday (including public holidays) in months\n",
                "Monday to Saturday (including public holidays) in months\n",
            ),
            "every day",
        ),
        # A capitalised restriction carried on below the definition.
        (
            (
                "                                    inclusive",
                "                                    Weekends only",
            ),
            "wording",
        ),
        # Text one column off the definition's is still read.
        (
            (
                "                                    inclusive",
                "                                   except holidays",
            ),
            "wording",
        ),
        (
            (
                "Monday to Sunday (including public holidays) in months",
                "Monday to Friday (including public holidays) in months",
            ),
            "every day",
        ),
    ],
)
def test_parse_missing_values(change: tuple[str, str], match: str) -> None:
    """A value it can't find raises, rather than writing a guess."""
    old, new = change
    assert old in TEXT_2026
    text = TEXT_2026.replace(old, new)
    with pytest.raises(LayoutError, match=match):
        parse_schedule(text, "url")


def test_find_relative_link() -> None:
    """A link relative to the page is resolved against it."""
    page = f'<a href="{URL_2027.removeprefix("https://media.vector.co.nz")}">'
    assert find_schedules(page, "https://media.vector.co.nz/pricing") == {
        date(2027, 4, 1): URL_2027
    }


def test_rate_keeps_extra_decimals(network_file: Path) -> None:
    """A rate printed with five decimals isn't rounded to four."""
    schedule = parse_schedule(_text_2027().replace("0.1600", "0.16005"), URL_2027)
    add_schedule(network_file, schedule)
    assert "peak: 0.16005," in network_file.read_text()


def test_find_schedules() -> None:
    """Schedule links on the pricing page are found by their start date."""
    page = f"""
        <a href="{URL_2026}">
        <a href='{URL_2027}'>
        <a href="{DOCS}vectorpricingroadmap2026-1.pdf">
    """
    assert find_schedules(page) == {
        date(2026, 4, 1): URL_2026,
        date(2027, 4, 1): URL_2027,
    }


def test_add_schedule_reuses_unchanged_bands(network_file: Path) -> None:
    """A schedule with the same peak times reuses the band schedule."""
    original = network_file.read_text()
    version = add_schedule(network_file, parse_schedule(_text_2027(), URL_2027))
    assert version == "v2027.1"
    raw = _load(network_file)
    assert raw["sources"]["v2027.1"] == URL_2027
    assert set(raw["bands"]) == {"vector_tou_2025", "vector_tou_2026"}
    network = parse_network(raw)
    latest = network.categories["ARHLC"][-1]
    assert latest.effective_from == date(2027, 4, 1)
    assert latest.import_rates == {"peak": 0.16, "offpeak": 0.0457}
    assert latest.bands.name == "vector_tou_2026"
    assert network.loss_categories["VECA1"][-1].factor == 1.0476
    # Only additions: every original line is kept, in order.
    lines = iter(network_file.read_text().splitlines())
    assert all(line in lines for line in original.splitlines())


def test_add_schedule_adds_changed_bands(network_file: Path) -> None:
    """Changed peak times get a band schedule of their own."""
    schedule = parse_schedule(_text_2027(morning="07:00 to 10:00"), URL_2027)
    add_schedule(network_file, schedule)
    network = parse_network(_load(network_file))
    bands = network.categories["ARHLU"][-1].bands
    assert bands.name == "vector_tou_2027"
    assert {rule.windows for rule in bands.rules} == {
        ((17 * 60, 22 * 60),),
        ((7 * 60, 10 * 60), (17 * 60, 22 * 60)),
    }


def test_add_schedule_only_once(network_file: Path) -> None:
    """A schedule that's already bundled isn't added again."""
    schedule = parse_schedule(_text_2027(), URL_2027)
    assert add_schedule(network_file, schedule) == "v2027.1"
    assert add_schedule(network_file, schedule) is None
    assert add_schedule(network_file, parse_schedule(TEXT_2026, "url")) is None


def test_second_schedule_in_a_year_keeps_earlier_bands(network_file: Path) -> None:
    """New peak times in a mid-year schedule don't change earlier versions'."""
    october = parse_schedule(
        _text("1 October 2026", "v2026.2", morning="07:00 to 10:00"), URL_OCT_2026
    )
    assert add_schedule(network_file, october) == "v2026.2"
    raw = _load(network_file)
    assert raw["categories"]["ARHLC"][1]["bands"] == "vector_tou_2026"
    assert raw["categories"]["ARHLC"][2]["bands"] == "vector_tou_2026_2"
    network = parse_network(raw)
    assert {r.windows for r in network.categories["ARHLC"][1].bands.rules} == {
        ((17 * 60, 22 * 60),),
        ((7 * 60, 11 * 60), (17 * 60, 22 * 60)),
    }


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    network_file: Path,
    pdfs: dict[str, str],
) -> dict[str, str]:
    """Run main() with a page linking these PDFs; return its outputs."""
    page = " ".join(f'<a href="{url}">' for url in pdfs).encode()
    monkeypatch.setattr(
        vector_schedule,
        "_get",
        lambda url: page if url == "page" else url.encode(),
    )
    monkeypatch.setattr(vector_schedule, "pdf_text", lambda pdf: pdfs[pdf.decode()])
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert vector_schedule.main(["--yaml", str(network_file), "--page", "page"]) == 0
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


@pytest.mark.parametrize(
    ("pdfs", "updated", "manual"),
    [
        ({}, "", None),
        ({URL_2026: TEXT_2026}, "", None),
        ({URL_2026: TEXT_2026, URL_2027: _text_2027()}, "v2027.1", None),
        (
            {URL_2027: TEXT_2025.replace("1 April 2025", "1 April 2027")},
            "",
            "columns",
        ),
        # The PDF's own date must match its link's.
        ({URL_2027: _text("1 April 2028", "v2028.1")}, "", "effective from"),
        # A corrected PDF for the bundled schedule.
        ({URL_2026: _text("1 April 2026", "v2026.2")}, "", "v2026.2"),
    ],
)
def test_main(
    network_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pdfs: dict[str, str],
    updated: str,
    manual: str | None,
) -> None:
    """The workflow learns what was added, and what needs a person."""
    original = network_file.read_text()
    outputs = _run_main(monkeypatch, tmp_path, network_file, pdfs)
    assert outputs["versions"] == updated
    assert outputs["updated"] == ("true" if updated else "false")
    assert (network_file.read_text() != original) == bool(updated)
    assert outputs["manual"] == ("true" if manual else "false")
    if manual:
        assert manual in outputs["reason"]


def test_main_adds_every_newer_schedule_in_order(
    network_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two new schedules are both added, oldest first."""
    pdfs = {
        URL_2027: _text_2027(),
        URL_OCT_2026: _text("1 October 2026", "v2026.2"),
    }
    outputs = _run_main(monkeypatch, tmp_path, network_file, pdfs)
    assert outputs["versions"] == "v2026.2 v2027.1"
    dates = [v["effective_from"] for v in _load(network_file)["categories"]["ARHLU"]]
    assert dates == [
        date(2025, 4, 1),
        date(2026, 4, 1),
        date(2026, 10, 1),
        date(2027, 4, 1),
    ]


def test_main_stops_at_unreadable_schedule(
    network_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later schedule isn't added past one that needs a person."""
    pdfs = {
        URL_OCT_2026: _text("1 October 2026", "v2026.2").replace("ARHLU ", "ARHLX "),
        URL_2027: _text_2027(),
    }
    outputs = _run_main(monkeypatch, tmp_path, network_file, pdfs)
    assert outputs["updated"] == "false"
    assert outputs["manual_url"] == URL_OCT_2026


def test_main_reports_bundled_revision_first(
    network_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrected bundled schedule isn't hidden by a later unreadable one."""
    pdfs = {
        URL_2026: _text("1 April 2026", "v2026.2"),
        URL_2027: TEXT_2025.replace("1 April 2025", "1 April 2027"),
    }
    outputs = _run_main(monkeypatch, tmp_path, network_file, pdfs)
    assert outputs["manual_url"] == URL_2026
    # Both problems are reported, the bundled correction first.
    assert outputs["reason"].index("v2026.2") < outputs["reason"].index("columns")
    assert URL_2027 in outputs["reason"]


def test_main_failed_download_needs_a_person(
    network_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schedule that can't be downloaded opens an issue, not a crash."""
    page = f'<a href="{URL_2027}">'.encode()

    def get(url: str) -> bytes:
        if url == "page":
            return page
        raise urllib.error.HTTPError(url, 500, "Server Error", Message(), None)

    monkeypatch.setattr(vector_schedule, "_get", get)
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert vector_schedule.main(["--yaml", str(network_file), "--page", "page"]) == 0
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert outputs["manual"] == "true"
    assert "500" in outputs["reason"]


def test_pdftotext_failure_needs_a_person(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PDF pdftotext can't read raises LayoutError, with its message."""
    monkeypatch.setattr(
        vector_schedule.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 1, b"", b"Syntax Error: broken"
        ),
    )
    with pytest.raises(LayoutError, match="Syntax Error: broken"):
        vector_schedule.pdf_text(b"%PDF-1.6 broken")
