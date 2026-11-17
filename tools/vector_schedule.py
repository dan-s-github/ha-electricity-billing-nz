"""
Find a new Vector pricing schedule and add it to the bundled network data.

Run monthly by .github/workflows/vector-schedule.yml. It reads the schedule
PDF's text (pdftotext -layout) and only writes vector.yaml when every value
it needs is found in the layout it knows; otherwise it raises LayoutError,
so a person can transcribe the schedule instead.
"""

from __future__ import annotations

import argparse
import calendar
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

PRICING_PAGE = "https://vector.co.nz/our-services/electricity/electricity-pricing"
VECTOR_YAML = (
    Path(__file__).parents[1]
    / "custom_components/electricity_billing_nz/networks/vector.yaml"
)
USER_AGENT = "Mozilla/5.0 (compatible; ha-electricity-billing-nz schedule check)"

# The bundled categories and loss category.
CATEGORIES = ("ARHLC", "ARHLU")
LOSS_CATEGORIES = ("VECA1",)

# href="…/electricitypricescheduleseffectivefrom1april2026.pdf", which may
# be relative to the page.
SCHEDULE_LINK = re.compile(
    r"href=[\"']([^\"']*electricitypriceschedules?effectivefrom"
    r"(\d{1,2})([a-z]+)(\d{4})\.pdf)[\"']",
    re.IGNORECASE,
)
MONTHS = {
    name.lower(): number for number, name in enumerate(calendar.month_name) if name
}

# Price table columns: the first FIXD is the daily charge; injection
# prices are charges, so a credit is shown in brackets.
KNOWN_COLUMNS = {"FIXD", "24UC", "AICO", "OFPK", "PEAK", "IJOP", "IJPK", "INJT"}
HEADER_CODE = re.compile(r"(?<!\S)-?([A-Z0-9]{4})(?!\S)")
NUMBER = re.compile(r"\(?-?\d+\.\d+\)?")
PEAK_DEFINITION = re.compile(
    r"-PEAK\s+(\d{2}:\d{2}) to (\d{2}:\d{2}) for months (\w+) to (\w+)"
)
# Any peak definition with a time, whether or not it's in the known wording.
PEAK_WITH_TIME = re.compile(r"-PEAK\s+\d")
LOSS_ROW = re.compile(r"^\s*(\w+)\s+\d+(?:\.\d+)?%\s+(\d\.\d+)\s", re.MULTILINE)
VERSION = re.compile(r"^Version:\s+(v\d{4}\.\d+)\s*$", re.MULTILINE)


class Rate(float):
    """A rate or factor written with four decimals, as the schedules print them."""


def _represent_rate(representer: Any, value: Rate) -> Any:
    # More decimals than four are kept, not rounded away.
    text = f"{value:.4f}" if float(f"{value:.4f}") == value else repr(float(value))
    return representer.represent_scalar("tag:yaml.org,2002:float", text)


def _yaml() -> YAML:
    """Return a round-trip YAML that keeps vector.yaml's layout."""
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.representer.add_representer(Rate, _represent_rate)
    # Entries share one date object; write it out each time, not as an alias.
    yaml.representer.ignore_aliases = lambda *_: True
    return yaml


class LayoutError(ValueError):
    """The schedule isn't laid out as the parser expects."""


@dataclass
class Schedule:
    """The values of one schedule this integration bundles (ex GST, NZD)."""

    effective_from: date
    version: str
    url: str
    categories: dict[str, dict[str, Any]] = field(default_factory=dict)
    losses: dict[str, float] = field(default_factory=dict)
    # Peak rules, every day including public holidays.
    rules: list[dict[str, Any]] = field(default_factory=list)


def find_schedules(page: str, page_url: str = PRICING_PAGE) -> dict[date, str]:
    """Return the schedule PDFs linked from the pricing page, by start date."""
    found = {}
    for match in SCHEDULE_LINK.finditer(page):
        href, day, month, year = match.groups()
        if (number := MONTHS.get(month.lower())) is None:
            continue
        url = urllib.parse.urljoin(page_url, href)
        found[date(int(year), number, int(day))] = url
    return found


def _value(token: str) -> float:
    if token.startswith("("):
        return -float(token.strip("()"))
    return float(token)


def _price_columns(lines: list[str]) -> tuple[int, list[tuple[str, int]]]:
    """Return the header line's index and its (code, centre) columns."""
    for index, line in enumerate(lines):
        codes = list(HEADER_CODE.finditer(line))
        names = [m.group(1) for m in codes]
        if not {"FIXD", "OFPK", "PEAK"} <= set(names):
            continue
        # e.g. 2025's separate summer and winter PEAK columns.
        if set(names) - KNOWN_COLUMNS or names.count("PEAK") != 1:
            msg = f"Unexpected price columns: {names}"
            raise LayoutError(msg)
        return index, [(m.group(1), (m.start(1) + m.end(1)) // 2) for m in codes]
    msg = "No price table header with FIXD, OFPK and PEAK columns"
    raise LayoutError(msg)


def _category_row(
    lines: list[str], start: int, code: str, columns: list[tuple[str, int]]
) -> dict[str, Any]:
    for line in lines[start:]:
        match = re.search(rf"\s{code}\s", line)
        if match is None:
            continue
        values: dict[str, float] = {}
        for token in NUMBER.finditer(line, match.end()):
            centre = (token.start() + token.end()) // 2
            index = min(range(len(columns)), key=lambda i: abs(columns[i][1] - centre))
            name = columns[index][0]
            # The first FIXD is $/day; a second is $/day per fitting.
            if name == "FIXD" and index != 0:
                name = "FIXD2"
            if name in values:
                msg = f"{code}: two values under {name}"
                raise LayoutError(msg)
            values[name] = _value(token.group())
        if not {"FIXD", "OFPK", "PEAK"} <= set(values) or set(values) - {
            "FIXD",
            "OFPK",
            "PEAK",
            "IJOP",
            "IJPK",
        }:
            msg = f"{code}: not a time of use row: {values}"
            raise LayoutError(msg)
        row: dict[str, Any] = {
            "daily": values["FIXD"],
            "import": {"peak": values["PEAK"], "offpeak": values["OFPK"]},
        }
        if {"IJOP", "IJPK"} <= set(values):
            # A negative injection charge is a credit paid to the customer.
            row["export_credit"] = {
                "peak": round(-values["IJPK"], 6) + 0.0,
                "offpeak": round(-values["IJOP"], 6) + 0.0,
            }
        elif "IJOP" in values or "IJPK" in values:
            msg = f"{code}: only one injection price"
            raise LayoutError(msg)
        return row
    msg = f"No row for price category {code}"
    raise LayoutError(msg)


def _section(text: str, start: str, end: str) -> str:
    try:
        after = text.split(start, 1)[1]
        return after.split(end, 1)[0] if end in after else ""
    except IndexError:
        return ""


# Words that would restrict a peak definition in a way the rules don't say.
QUALIFIER = re.compile(
    r"day|weekend|holiday|business|except|exclud|only|\bnot\b|\bno\b",
    re.IGNORECASE,
)
# The days table's wording for peak every day, holidays included.
EVERY_DAY = "Monday to Sunday (including public holidays) in months"
DAY_RANGE = re.compile(
    r"\b(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day\b[^\n]{0,60}?(?:in months|$)",
    re.MULTILINE,
)
# How far off the definition's column carried-on text may start.
COLUMN_SLACK = 3


def _check_qualifiers(line: str, match: re.Match[str], below: list[str]) -> None:
    """
    Raise LayoutError if a peak definition says more than its times and months.

    The definition's column starts at its time, and two or more spaces
    separate it from the next column. It may carry on below, e.g. with
    "inclusive"; the next definition starts with a capital letter.
    """
    chunks = [line[match.end() :].split("  ", 1)[0].strip()]
    column = match.start(1)
    for following in below:
        if PEAK_WITH_TIME.search(following):
            break  # the next peak definition
        start = len(following) - len(following[column - COLUMN_SLACK :].lstrip())
        if not following.strip() or abs(start - column) > COLUMN_SLACK:
            continue
        chunk = following[start:].split("  ", 1)[0].strip()
        if chunk[0].isupper() and not QUALIFIER.search(chunk):
            break  # the next price component's description
        chunks.append(chunk)
    for chunk in chunks:
        if chunk not in ("", "inclusive"):
            msg = f"Peak definition in an unknown wording: {line.strip()!r} ({chunk!r})"
            raise LayoutError(msg)


def _peak_definitions(text: str) -> list[tuple[str, str, str, str]]:
    """
    Return each (start, end, first month, last month) peak definition.

    Only the price component definitions are read, and every peak time
    there must be in the known wording, followed by nothing but
    "inclusive" in its column, on its line or the lines carrying on below
    it, so a qualifier such as "weekdays" isn't lost.
    """
    section = _section(text, "Price component definitions", "Peak and off-peak periods")
    if not section:
        msg = "No 'Price component definitions' before 'Peak and off-peak periods'"
        raise LayoutError(msg)
    definitions = []
    lines = section.splitlines()
    for index, line in enumerate(lines):
        for candidate in PEAK_WITH_TIME.finditer(line):
            match = PEAK_DEFINITION.match(line, candidate.start())
            if match is None:
                msg = f"Peak definition in an unknown wording: {line.strip()!r}"
                raise LayoutError(msg)
            _check_qualifiers(line, match, lines[index + 1 :])
            start, end, first, last = match.groups()
            definitions.append((start, end, first, last))
    if not definitions:
        msg = "No peak period definitions ('-PEAK hh:mm to hh:mm for months ...')"
        raise LayoutError(msg)
    return definitions


def _peak_rules(text: str) -> list[dict[str, Any]]:
    """Group the peak windows by the months they apply in."""
    windows_by_month: dict[int, list[str]] = {}
    for start, end, first, last in _peak_definitions(text):
        month, stop = MONTHS.get(first.lower()), MONTHS.get(last.lower())
        if month is None or stop is None:
            msg = f"Unknown months {first} to {last}"
            raise LayoutError(msg)
        while True:
            windows_by_month.setdefault(month, []).append(f"{start}-{end}")
            if month == stop:
                break
            month = month % 12 + 1
    section = text.split("Peak and off-peak periods", 1)
    if len(section) != 2:  # noqa: PLR2004
        msg = "No 'Peak and off-peak periods' section"
        raise LayoutError(msg)
    days = section[1][:3000]
    # The rules written apply every day, holidays included, so that's the
    # only wording accepted: anything else needs a person.
    ranges = {" ".join(m.group().split()) for m in DAY_RANGE.finditer(days)}
    if ranges != {EVERY_DAY}:
        msg = f"Peak days aren't every day: {sorted(ranges)}"
        raise LayoutError(msg)
    if days.lower().count("holiday") != days.count("(including public holidays)"):
        msg = "Public holidays are treated differently somewhere"
        raise LayoutError(msg)
    rules: list[dict[str, Any]] = []
    for month in sorted(windows_by_month):
        windows = sorted(windows_by_month[month])
        for rule in rules:
            if rule["windows"] == windows:
                rule["months"].append(month)
                break
        else:
            rules.append({"band": "peak", "months": [month], "windows": windows})
    return rules


def parse_schedule(text: str, url: str) -> Schedule:
    """Return the bundled values from a schedule's pdftotext -layout text."""
    effective = re.search(r"Effective:\s+(\d{1,2} \w+ \d{4})", text)
    if effective is None:
        msg = "No 'Effective:' date"
        raise LayoutError(msg)
    if (version := VERSION.search(text)) is None:
        msg = "No 'Version:' line"
        raise LayoutError(msg)
    try:
        effective_from = datetime.strptime(effective.group(1), "%d %B %Y").date()  # noqa: DTZ007
    except ValueError as err:
        msg = f"Unexpected 'Effective:' date {effective.group(1)!r}"
        raise LayoutError(msg) from err
    schedule = Schedule(
        effective_from=effective_from,
        version=version.group(1),
        url=url,
    )
    lines = text.splitlines()
    header, columns = _price_columns(lines)
    for code in CATEGORIES:
        schedule.categories[code] = _category_row(lines, header + 1, code, columns)
    # The first row for a code: the loss factor table comes first.
    losses: dict[str, str] = {}
    for code, factor in LOSS_ROW.findall(text):
        losses.setdefault(code, factor)
    for code in LOSS_CATEGORIES:
        if code not in losses:
            msg = f"No loss factor for {code}"
            raise LayoutError(msg)
        schedule.losses[code] = float(losses[code])
    schedule.rules = _peak_rules(text)
    return schedule


def _rates(rates: dict[str, float]) -> CommentedMap:
    flow = CommentedMap({band: Rate(rate) for band, rate in rates.items()})
    flow.fa.set_flow_style()
    return flow


def same_rules(raw: dict[str, Any], rules: list[dict[str, Any]]) -> bool:
    """Return whether a bundled band schedule has exactly these peak rules."""

    def key(rule: dict[str, Any]) -> tuple[Any, ...]:
        return (
            rule["band"],
            tuple(sorted(rule["months"])),
            tuple(sorted(rule["windows"])),
            tuple(sorted(rule["weekdays"])) if rule.get("weekdays") else None,
            rule.get("holidays"),
        )

    return (
        raw.get("default") == "offpeak"
        and list(raw.get("peak", ["peak"])) == ["peak"]
        and sorted(map(key, raw.get("rules", []))) == sorted(map(key, rules))
    )


def add_schedule(path: Path, schedule: Schedule) -> str | None:
    """Add the schedule to the network file; return its version, or None."""
    yaml = _yaml()
    data = yaml.load(path.read_text(encoding="utf-8"))
    latest = data["categories"][CATEGORIES[0]][-1]
    if latest["effective_from"] >= schedule.effective_from:
        return None
    version = schedule.version
    if version in data["sources"]:
        msg = f"{version} is already bundled, with an earlier date"
        raise LayoutError(msg)
    data["sources"][version] = schedule.url

    bands = latest["bands"]
    if not same_rules(data["bands"][bands], schedule.rules):
        # Never reuse a name: earlier versions keep their own peak rules.
        bands = f"vector_tou_{schedule.effective_from.year}"
        if bands in data["bands"]:
            bands = "vector_tou_" + version.removeprefix("v").replace(".", "_")
        rules = CommentedSeq()
        for rule in schedule.rules:
            rules.append(
                CommentedMap(
                    band=rule["band"],
                    months=_flow_seq(rule["months"]),
                    windows=_flow_seq(
                        [DoubleQuotedScalarString(w) for w in rule["windows"]]
                    ),
                )
            )
        data["bands"][bands] = CommentedMap(
            default="offpeak", peak=_flow_seq(["peak"]), rules=rules
        )
        data["bands"].yaml_set_comment_before_after_key(
            bands,
            before="Peak applies Monday to Sunday, including public holidays.",
            indent=2,
        )

    for code, values in schedule.categories.items():
        entry = CommentedMap(
            effective_from=schedule.effective_from,
            schedule_version=version,
            daily=Rate(values["daily"]),
            **{"import": _rates(values["import"])},
        )
        if "export_credit" in values:
            entry["export_credit"] = _rates(values["export_credit"])
        entry["bands"] = bands
        data["categories"][code].append(entry)
    for code, factor in schedule.losses.items():
        data["loss_categories"][code].append(
            CommentedMap(
                effective_from=schedule.effective_from,
                schedule_version=version,
                factor=Rate(factor),
            )
        )
    with path.open("w", encoding="utf-8") as handle:
        yaml.dump(data, handle)
    return version


def _flow_seq(items: list[Any]) -> CommentedSeq:
    seq = CommentedSeq(items)
    seq.fa.set_flow_style()
    return seq


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        return response.read()


def pdf_text(pdf: bytes) -> str:
    """Return a PDF's pdftotext -layout text; LayoutError if it can't be read."""
    if not pdf.startswith(b"%PDF"):
        msg = "The schedule link isn't a PDF"
        raise LayoutError(msg)
    result = subprocess.run(
        ["pdftotext", "-layout", "-", "-"],  # noqa: S607
        input=pdf,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        error = result.stderr.decode("utf-8", errors="replace").strip()
        msg = f"pdftotext couldn't read the PDF: {error or result.returncode}"
        raise LayoutError(msg)
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError as err:
        msg = f"pdftotext output isn't UTF-8: {err}"
        raise LayoutError(msg) from err


def _read_schedule(url: str, effective_from: date) -> Schedule:
    """Download and parse a schedule, checking it's the one the link names."""
    schedule = parse_schedule(pdf_text(_get(url)), url)
    return _check_date(schedule, effective_from)


def _check_date(schedule: Schedule, effective_from: date) -> Schedule:
    if schedule.effective_from != effective_from:
        msg = (
            f"The PDF says it's effective from {schedule.effective_from}, "
            f"its link {effective_from}"
        )
        raise LayoutError(msg)
    return schedule


def _output(**values: str) -> None:
    """Pass results to later workflow steps, and print them."""
    for name, value in values.items():
        print(f"{name}={value}")  # noqa: T201
    if path := os.environ.get("GITHUB_OUTPUT"):
        with Path(path).open("a", encoding="utf-8") as handle:
            for name, value in values.items():
                handle.write(f"{name}={value}\n")


def _revision(url: str, bundled_version: str) -> str | None:
    """Return why the bundled schedule's PDF needs a look, or None."""
    text = pdf_text(_get(url))
    if (version := VERSION.search(text)) is None:
        return "The bundled schedule's PDF has no 'Version:' line"
    if version.group(1) != bundled_version:
        return (
            f"The bundled schedule's PDF is now {version.group(1)}, "
            f"not {bundled_version}: check it for corrections"
        )
    return None


def main(argv: list[str] | None = None) -> int:
    """
    Add every linked schedule newer than the bundled data, oldest first.

    Outputs for the workflow: updated (true/false) with the versions added,
    and manual (true/false) with what a person must handle and why: a
    revised bundled schedule, and the first newer schedule that couldn't be
    read or downloaded. Adding stops there, so later ones aren't added out
    of order.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yaml", type=Path, default=VECTOR_YAML)
    parser.add_argument("--page", default=PRICING_PAGE)
    args = parser.parse_args(argv)

    data = _yaml().load(args.yaml.read_text(encoding="utf-8"))
    latest = data["categories"][CATEGORIES[0]][-1]
    bundled: date = latest["effective_from"]
    page = _get(args.page).decode("utf-8", errors="replace")
    linked = find_schedules(page, args.page)

    added: list[Schedule] = []
    manual: list[tuple[date, str, str]] = []
    if bundled in linked:
        try:
            reason = _revision(linked[bundled], latest["schedule_version"])
        except (LayoutError, OSError) as err:
            reason = str(err)
        if reason:
            manual.append((bundled, linked[bundled], reason))
    for effective_from in sorted(d for d in linked if d > bundled):
        url = linked[effective_from]
        try:
            schedule = _read_schedule(url, effective_from)
            if add_schedule(args.yaml, schedule):
                added.append(schedule)
        except (LayoutError, OSError) as err:
            # OSError: the download failed, or pdftotext is missing.
            later = " Later schedules wait until this one is bundled."
            manual.append((effective_from, url, f"{err}{later}"))
            break

    _output(
        updated="true" if added else "false",
        versions=" ".join(s.version for s in added),
        sources=" ".join(s.url for s in added),
        manual="true" if manual else "false",
    )
    if manual:
        # The issue is titled after the first; its text lists them all.
        _output(
            manual_effective_from=manual[0][0].isoformat(),
            manual_url=manual[0][1],
            reason=" ".join(
                f"[{d.isoformat()}]({url}): {reason}".replace("\n", " ")
                for d, url, reason in manual
            ),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
