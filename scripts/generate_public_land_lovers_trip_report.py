#!/usr/bin/env python3
"""
Public Land Lovers - Trip Report Generator

Generates a static HTML trip report from the Public Land Lovers
ArcGIS Feature Service.

The report contains:
    - Trip title and date range
    - Trip statistics
    - Interactive embedded ArcGIS Web Map
    - Chronological stop-by-stop itinerary
    - Mileage and drive-time information
    - Notes and descriptions
    - Land ownership / camping information
    - Photo attachments

The ArcGIS map is embedded using the ArcGIS Embeddable Map component.

Embedded Web Map:
    Item ID:
        5c7de7cbc28f4f1eaa2219732790c93a

    Portal:
        https://pll.maps.arcgis.com

IMPORTANT:
    The embedded map is intentionally NOT filtered by the report's
    --segment, --state, --start-date, or --end-date arguments.

    Those filters control the written report only.

Examples:

    python generate_public_land_lovers_trip_report.py

    python generate_public_land_lovers_trip_report.py --pdf

    python generate_public_land_lovers_trip_report.py --inspect

    python generate_public_land_lovers_trip_report.py --segment "Big Bend"

    python generate_public_land_lovers_trip_report.py --state Texas

    python generate_public_land_lovers_trip_report.py \
        --start-date 2026-01-01 \
        --end-date 2026-03-01

Requirements:

    python -m pip install requests

Optional PDF support:

    python -m pip install playwright
    python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import time

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


# ============================================================================
# CONFIGURATION
# ============================================================================

LAYER_URL = (
    "https://services8.arcgis.com/"
    "KzyxLudI6Hn5u85O/arcgis/rest/services/"
    "Janyne_and_Andrew_VanLife/FeatureServer/0"
)

WEBSITE_URL = "https://publiclandlovers.com"

BRAND = "Public Land Lovers"

AUTHORS = "Andrew & Janyne"

OUTPUT_DIR = Path("public_land_lovers_report")

REQUEST_TIMEOUT = 60

RETRIES = 3

PAGE_SIZE = 1000


# --------------------------------------------------------------------------
# EMBEDDED ARCGIS WEB MAP
# --------------------------------------------------------------------------

WEB_MAP_ITEM_ID = "5c7de7cbc28f4f1eaa2219732790c93a"

ARCGIS_PORTAL_URL = "https://pll.maps.arcgis.com"

ARCGIS_COMPONENT_SCRIPT = (
    "https://js.arcgis.com/5.1/embeddable-components/"
)

# These are the settings supplied by the ArcGIS Online embed generator.
ARCGIS_MAP_THEME = "light"

ARCGIS_MAP_CENTER = "-100.96733044185954,32.356554882199454"

ARCGIS_MAP_SCALE = "36978595.47447219"

ARCGIS_MAP_HEIGHT = "600px"


# None = all image attachments.
MAX_PHOTOS_PER_STOP = None


# Report sections are grouped by this field.
SECTION_FIELD = "Segment"


# --------------------------------------------------------------------------
# FIELDS INTENTIONALLY EXCLUDED
# --------------------------------------------------------------------------
#
# DateLeft
# Location__slept_
# seq
# wpt_id
# category
# type
# nameOverride
# vagueAddress
# desc_
# MediaLink
# created_user
# created_date
# last_edited_user
# last_edited_date
# GlobalID
#
# Internal camping/rating fields are also excluded unless explicitly added
# later.
# --------------------------------------------------------------------------


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class Attachment:
    attachment_id: int
    name: str
    content_type: str
    size: int
    url: str


@dataclass
class Stop:
    object_id: int
    attributes: dict[str, Any]
    geometry: dict[str, Any] | None
    attachments: list[Attachment]


class ReportError(RuntimeError):
    pass


# ============================================================================
# HTTP / ARCGIS
# ============================================================================

def request_json(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
) -> dict[str, Any]:

    last_error = None

    for attempt in range(1, RETRIES + 1):

        try:

            response = session.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

            response.raise_for_status()

            data = response.json()

            if data.get("error"):

                raise ReportError(
                    json.dumps(
                        data["error"],
                        indent=2,
                    )
                )

            return data

        except Exception as exc:

            last_error = exc

            if attempt < RETRIES:
                time.sleep(attempt * 1.5)

    raise ReportError(
        f"ArcGIS request failed:\n"
        f"{url}\n"
        f"{last_error}"
    )


def get_layer_info(
    session: requests.Session,
) -> dict[str, Any]:

    return request_json(
        session,
        LAYER_URL,
        {
            "f": "json",
        },
    )


# ============================================================================
# QUERY STOPS
# ============================================================================

def query_stops(
    session: requests.Session,
    where: str,
) -> list[dict[str, Any]]:

    query_url = f"{LAYER_URL}/query"

    ids = request_json(
        session,
        query_url,
        {
            "where": where,
            "returnIdsOnly": "true",
            "f": "json",
        },
    ).get("objectIds") or []

    ids = [int(x) for x in ids]

    if not ids:
        return []

    features: list[dict[str, Any]] = []

    for start in range(
        0,
        len(ids),
        PAGE_SIZE,
    ):

        batch = ids[start:start + PAGE_SIZE]

        result = request_json(
            session,
            query_url,
            {
                "objectIds": ",".join(
                    map(str, batch)
                ),
                "outFields": "*",
                "returnGeometry": "true",
                "f": "json",
            },
        )

        features.extend(
            result.get("features", [])
        )

    return features


# ============================================================================
# ATTACHMENTS
# ============================================================================

def is_image_attachment(
    name: str,
    content_type: str,
) -> bool:

    extension = Path(name).suffix.lower()

    content_type = (
        content_type or ""
    ).lower()

    return (
        content_type.startswith("image/")
        or extension in {
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".webp",
            ".heic",
            ".heif",
        }
    )


def query_attachments(
    session: requests.Session,
    object_ids: list[int],
) -> dict[int, list[Attachment]]:

    result: dict[
        int,
        list[Attachment]
    ] = defaultdict(list)

    if not object_ids:
        return result

    for start in range(
        0,
        len(object_ids),
        PAGE_SIZE,
    ):

        batch = object_ids[start:start + PAGE_SIZE]

        data = request_json(
            session,
            f"{LAYER_URL}/queryAttachments",
            {
                "objectIds": ",".join(
                    map(str, batch)
                ),
                "returnUrl": "false",
                "f": "json",
            },
        )

        for group in data.get(
            "attachmentGroups",
            [],
        ):

            parent_id = group.get(
                "parentObjectId"
            )

            if parent_id is None:
                continue

            for info in group.get(
                "attachmentInfos",
                [],
            ):

                attachment_id = info.get("id")

                name = (
                    info.get("name")
                    or "photo"
                )

                content_type = (
                    info.get("contentType")
                    or ""
                )

                if attachment_id is None:
                    continue

                if not is_image_attachment(
                    name,
                    content_type,
                ):
                    continue

                url = (
                    f"{LAYER_URL}/"
                    f"{int(parent_id)}/attachments/"
                    f"{int(attachment_id)}"
                    f"?w=1800"
                )

                result[
                    int(parent_id)
                ].append(
                    Attachment(
                        attachment_id=int(
                            attachment_id
                        ),
                        name=name,
                        content_type=content_type,
                        size=int(
                            info.get("size")
                            or 0
                        ),
                        url=url,
                    )
                )

    for attachments in result.values():

        attachments.sort(
            key=lambda x: x.attachment_id
        )

    return result


# ============================================================================
# DATE / FIELD HELPERS
# ============================================================================

def parse_date(
    value: Any,
) -> datetime | None:

    if value in (
        None,
        "",
    ):
        return None

    if isinstance(
        value,
        (int, float),
    ):

        try:

            return datetime.fromtimestamp(
                float(value) / 1000,
                tz=timezone.utc,
            )

        except Exception:
            return None

    text = str(value).strip()

    formats = (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y",
    )

    for fmt in formats:

        try:
            return datetime.strptime(
                text,
                fmt,
            )

        except ValueError:
            pass

    return None


def day_without_zero(
    dt: datetime,
) -> str:

    return str(dt.day)


def format_date(
    value: Any,
) -> str:

    dt = parse_date(value)

    if dt:

        return (
            f"{dt.strftime('%A')}, "
            f"{dt.strftime('%B')} "
            f"{day_without_zero(dt)}, "
            f"{dt.year}"
        )

    if value not in (
        None,
        "",
    ):

        return str(value)

    return ""


def format_short_date(
    value: Any,
) -> str:

    dt = parse_date(value)

    if dt:

        return dt.strftime(
            "%m/%d/%Y"
        )

    if value not in (
        None,
        "",
    ):

        return str(value)

    return ""


def clean_text(
    value: Any,
) -> str:

    if value is None:
        return ""

    text = str(value).strip()

    if not text:
        return ""

    if text.lower() in {
        "null",
        "none",
        "nan",
        "n/a",
        "na",
    }:

        return ""

    return text


def esc(
    value: Any,
) -> str:

    return html.escape(
        clean_text(value)
    )


def format_number(
    value: Any,
    decimals: int = 0,
) -> str:

    if value in (
        None,
        "",
    ):
        return ""

    try:

        number = float(value)

        if not math.isfinite(number):
            return ""

        if decimals == 0:

            return f"{number:,.0f}"

        return (
            f"{number:,.{decimals}f}"
        )

    except (
        TypeError,
        ValueError,
    ):

        return clean_text(value)


def format_miles(
    value: Any,
) -> str:

    if value in (
        None,
        "",
    ):
        return ""

    try:

        number = float(value)

        if not math.isfinite(number):
            return ""

        if abs(
            number - round(number)
        ) < 0.05:

            return (
                f"{round(number):,} mi"
            )

        return (
            f"{number:,.1f} mi"
        )

    except (
        TypeError,
        ValueError,
    ):

        text = clean_text(value)

        if not text:
            return ""

        return f"{text} mi"


def format_drive_time(
    drive_time: Any,
    drive_minutes: Any,
) -> str:

    text = clean_text(
        drive_time
    )

    if text:
        return text

    if drive_minutes not in (
        None,
        "",
    ):

        try:

            total = round(
                float(drive_minutes)
            )

            if total < 0:
                return ""

            hours, minutes = divmod(
                total,
                60,
            )

            if hours and minutes:

                return (
                    f"{hours} hr "
                    f"{minutes} min"
                )

            if hours:

                return (
                    f"{hours} hr"
                )

            return (
                f"{minutes} min"
            )

        except (
            TypeError,
            ValueError,
        ):

            pass

    return ""


# ============================================================================
# SORTING
# ============================================================================

def stop_sort_key(
    stop: Stop,
):

    date_value = stop.attributes.get(
        "DateArrived"
    )

    dt = parse_date(
        date_value
    )

    if dt:

        return (
            0,
            dt.timestamp(),
            stop.object_id,
        )

    # Undated stops go at the end.
    return (
        1,
        float("inf"),
        stop.object_id,
    )


# ============================================================================
# FILTERS
# ============================================================================

def sql_quote(
    value: str,
) -> str:

    return (
        "'"
        + value.replace(
            "'",
            "''",
        )
        + "'"
    )


def build_where(
    args: argparse.Namespace,
) -> str:

    clauses = [
        "1=1"
    ]

    if args.segment:

        clauses.append(
            "Segment = "
            + sql_quote(
                args.segment
            )
        )

    if args.state:

        clauses.append(
            "State_1 = "
            + sql_quote(
                args.state
            )
        )

    if args.start_date:

        clauses.append(
            "DateArrived >= DATE "
            + sql_quote(
                args.start_date
            )
        )

    if args.end_date:

        clauses.append(
            "DateArrived <= DATE "
            + sql_quote(
                args.end_date
            )
        )

    return " AND ".join(
        clauses
    )


# ============================================================================
# REPORT STATISTICS
# ============================================================================

def numeric_values(
    stops: list[Stop],
    field: str,
) -> list[float]:

    values: list[float] = []

    for stop in stops:

        value = stop.attributes.get(
            field
        )

        if value in (
            None,
            "",
        ):
            continue

        try:

            number = float(value)

            if math.isfinite(number):
                values.append(number)

        except (
            TypeError,
            ValueError,
        ):

            continue

    return values


def total_numeric(
    stops: list[Stop],
    field: str,
) -> float:

    return sum(
        numeric_values(
            stops,
            field,
        )
    )


def get_trip_title(
    stops: list[Stop],
    args: argparse.Namespace,
) -> str:

    if args.title:
        return args.title

    if args.segment:
        return args.segment

    segments: list[str] = []

    for stop in stops:

        value = clean_text(
            stop.attributes.get(
                "Segment"
            )
        )

        if (
            value
            and value not in segments
        ):

            segments.append(
                value
            )

    if len(segments) == 1:
        return segments[0]

    return "Trip Report"


def date_range_text(
    stops: list[Stop],
) -> str:

    dates: list[datetime] = []

    for stop in stops:

        dt = parse_date(
            stop.attributes.get(
                "DateArrived"
            )
        )

        if dt:
            dates.append(dt)

    if not dates:
        return ""

    first = min(dates)
    last = max(dates)

    if first.date() == last.date():

        return (
            f"{first.strftime('%B')} "
            f"{day_without_zero(first)}, "
            f"{first.year}"
        )

    return (
        f"{first.strftime('%B')} "
        f"{day_without_zero(first)}, "
        f"{first.year}"
        f" – "
        f"{last.strftime('%B')} "
        f"{day_without_zero(last)}, "
        f"{last.year}"
    )


# ============================================================================
# GROUPING
# ============================================================================

def group_by_segment(
    stops: list[Stop],
) -> list[
    tuple[str, list[Stop]]
]:

    groups: dict[
        str,
        list[Stop]
    ] = defaultdict(list)

    for stop in stops:

        segment = clean_text(
            stop.attributes.get(
                SECTION_FIELD
            )
        ) or "Other"

        groups[
            segment
        ].append(stop)

    result = list(
        groups.items()
    )

    for _, values in result:

        values.sort(
            key=stop_sort_key
        )

    result.sort(
        key=lambda item: (
            stop_sort_key(
                item[1][0]
            )
            if item[1]
            else (
                99,
                float("inf"),
                0,
            )
        )
    )

    return result


# ============================================================================
# ARCGIS EMBED
# ============================================================================

def build_arcgis_map_embed() -> str:

    return f"""
<div class="map-wrapper">
    <div class="map-header">
        <div>
            <div class="map-title">
                Trip Map
            </div>
            <div class="map-subtitle">
                Explore the trip interactively
            </div>
        </div>

        <div class="map-powered">
            ArcGIS
        </div>
    </div>

    <div class="map-container">
        <arcgis-embedded-map
            style="height:{html.escape(ARCGIS_MAP_HEIGHT)};width:100%;"
            item-id="{html.escape(WEB_MAP_ITEM_ID)}"
            theme="{html.escape(ARCGIS_MAP_THEME)}"
            bookmarks-enabled
            heading-enabled
            legend-enabled
            information-enabled
            scroll-enabled
            basemap-gallery-enabled
            time-zone-label-enabled
            center="{html.escape(ARCGIS_MAP_CENTER)}"
            scale="{html.escape(ARCGIS_MAP_SCALE)}"
            portal-url="{html.escape(ARCGIS_PORTAL_URL)}">
        </arcgis-embedded-map>
    </div>

    <div class="map-note">
        Interactive map provided by ArcGIS Online.
        Use the map controls to pan, zoom, inspect locations,
        and explore available map information.
    </div>
</div>
""".strip()


# ============================================================================
# OPTIONAL FIELDS
# ============================================================================

def optional_field(
    label: str,
    value: Any,
) -> str:

    text = clean_text(value)

    if not text:
        return ""

    return (
        '<div class="optional-field">'
        f'<span class="field-label">'
        f'{esc(label)}'
        f'</span>'
        f'<span>{esc(text)}</span>'
        '</div>'
    )


# ============================================================================
# STOP HTML
# ============================================================================

def build_stop_html(
    stop: Stop,
    index: int,
) -> str:

    attributes = stop.attributes

    name = (
        clean_text(
            attributes.get("name")
        )
        or "Unnamed stop"
    )

    description = clean_text(
        attributes.get(
            "description"
        )
    )

    notes = clean_text(
        attributes.get(
            "Notes"
        )
    )

    arrival = format_short_date(
        attributes.get(
            "DateArrived"
        )
    )

    miles_total = format_miles(
        attributes.get(
            "MilesTotal"
        )
    )

    miles_since = (
        format_miles(
            attributes.get(
                "MilesSince"
            )
        )
        or format_miles(
            attributes.get(
                "Miles_From_Previous"
            )
        )
    )

    drive_time = format_drive_time(
        attributes.get(
            "Drive_Time_From_Previous"
        ),
        attributes.get(
            "Drive_Minutes"
        ),
    )

    location = clean_text(
        attributes.get(
            "Location"
        )
    )

    state = clean_text(
        attributes.get(
            "State_1"
        )
    )

    photos = stop.attachments

    if MAX_PHOTOS_PER_STOP is not None:

        photos = photos[
            :MAX_PHOTOS_PER_STOP
        ]

    parts = [
        '<article class="stop">',

        '<div class="stop-heading">',

        f'<div class="stop-number">'
        f'{index}'
        f'</div>',

        '<div class="stop-heading-main">',

        f'<h3>{esc(name)}</h3>',

        '<div class="stop-location">',
    ]

    location_parts: list[str] = []

    if location:
        location_parts.append(
            location
        )

    if state:
        location_parts.append(
            state
        )

    if location_parts:

        parts.append(
            esc(
                ", ".join(
                    location_parts
                )
            )
        )

    parts.extend(
        [
            '</div>',
            '</div>',
        ]
    )

    if arrival:

        parts.append(
            f'<div class="stop-date">'
            f'{esc(arrival)}'
            f'</div>'
        )

    parts.append(
        '</div>'
    )

    # ----------------------------------------------------------------------
    # METRICS
    # ----------------------------------------------------------------------

    metrics: list[str] = []

    if miles_since:

        metrics.append(
            '<span>'
            f'<b>{esc(miles_since)}</b>'
            ' from previous'
            '</span>'
        )

    if drive_time:

        metrics.append(
            '<span>'
            f'<b>{esc(drive_time)}</b>'
            ' drive'
            '</span>'
        )

    if miles_total:

        metrics.append(
            '<span>'
            f'<b>{esc(miles_total)}</b>'
            ' total'
            '</span>'
        )

    if metrics:

        parts.append(
            '<div class="metrics">'
            + "".join(metrics)
            + '</div>'
        )

    # ----------------------------------------------------------------------
    # DESCRIPTION
    # ----------------------------------------------------------------------

    if description:

        parts.append(
            '<div class="description">'
            f'{esc(description)}'
            '</div>'
        )

    # ----------------------------------------------------------------------
    # NOTES
    # ----------------------------------------------------------------------

    if notes:

        parts.append(
            '<div class="notes">'
            '<strong>Notes:</strong> '
            f'{esc(notes)}'
            '</div>'
        )

    # ----------------------------------------------------------------------
    # SECONDARY FIELDS
    # ----------------------------------------------------------------------

    optional = "".join(
        [
            optional_field(
                "Land ownership",
                attributes.get(
                    "Land_Ownership"
                ),
            ),

            optional_field(
                "Camp spot",
                attributes.get(
                    "camp_spot"
                ),
            ),

            optional_field(
                "Camp type",
                attributes.get(
                    "Camp_spot_type"
                ),
            ),
        ]
    )

    if optional:

        parts.append(
            '<div class="optional-fields">'
            f'{optional}'
            '</div>'
        )

    # ----------------------------------------------------------------------
    # PHOTOS
    # ----------------------------------------------------------------------

    if photos:

        parts.append(
            '<div class="photos">'
        )

        for photo in photos:

            alt = (
                f"{name} — "
                f"{photo.name}"
            )

            parts.append(
                '<figure class="photo-figure">'
                f'<img '
                f'src="{html.escape(photo.url, quote=True)}" '
                f'alt="{esc(alt)}" '
                'loading="lazy">'
                '</figure>'
            )

        parts.append(
            '</div>'
        )

    parts.append(
        '</article>'
    )

    return "".join(parts)


# ============================================================================
# CSS
# ============================================================================

CSS = r"""
@page {
    size: Letter;
    margin: 0.42in;
}

* {
    box-sizing: border-box;
}

html,
body {
    margin: 0;
    padding: 0;
}

body {
    background: #ffffff;
    color: #333333;
    font-family: Georgia, "Times New Roman", serif;
    font-size: 10.5pt;
    line-height: 1.38;
}

.report {
    max-width: 8.0in;
    margin: 0 auto;
    padding: 0.35in 0.48in;
}

.topline {
    display: flex;
    justify-content: space-between;
    color: #777777;
    font: 8pt Arial, sans-serif;
    margin-bottom: 0.20in;
}

.brand {
    color: #7b8730;
    font: 700 22pt Arial, sans-serif;
    letter-spacing: .2px;
}

.brand-subtitle {
    color: #777777;
    font: 9pt Arial, sans-serif;
    margin-top: 2px;
}

.brand-subtitle a {
    color: inherit;
    text-decoration: none;
}

.hero {
    text-align: center;
    margin: 0.24in 0 0.24in;
}

.hero h1 {
    font-size: 28pt;
    line-height: 1.05;
    font-weight: 400;
    margin: 0 0 8px;
}

.date-range {
    color: #777777;
    font-size: 10pt;
    margin-bottom: 12px;
}

.summary {
    display: flex;
    justify-content: center;
    flex-wrap: wrap;
    gap: 20px;
    font-family: Arial, sans-serif;
    color: #777777;
}

.summary-item strong {
    color: #7b8730;
    font-size: 14pt;
    margin-right: 4px;
}


/* ========================================================================
   INTERACTIVE MAP
   ======================================================================== */

.map-wrapper {
    width: 100%;
    margin: 0 0 0.35in;
    border: 1px solid #e1e3dd;
    border-radius: 7px;
    overflow: hidden;
    background: #ffffff;
    break-inside: avoid;
}

.map-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 15px;
    padding: 10px 14px;
    background: #f4f5f1;
    border-bottom: 1px solid #e1e3dd;
}

.map-title {
    font: 700 11pt Arial, sans-serif;
    color: #333333;
}

.map-subtitle {
    font: 8.5pt Arial, sans-serif;
    color: #777777;
    margin-top: 2px;
}

.map-powered {
    font: 700 8pt Arial, sans-serif;
    color: #777777;
}

.map-container {
    width: 100%;
    height: 600px;
    background: #f4f5f1;
}

.map-container arcgis-embedded-map {
    display: block;
    width: 100%;
    height: 600px;
}

.map-note {
    padding: 7px 12px;
    color: #888888;
    background: #fafaf8;
    border-top: 1px solid #e7e8e3;
    font: 7.5pt Arial, sans-serif;
}


/* ========================================================================
   SECTIONS
   ======================================================================== */

.section {
    margin-top: 0.22in;
}

.section-header {
    border-bottom: 1.5px solid #7b8730;
    margin-bottom: 0.14in;
    padding-bottom: 5px;
}

.section-title {
    margin: 0;
    color: #333333;
    font-size: 17pt;
    font-weight: 500;
}

.section-summary {
    color: #888888;
    font-size: 9pt;
    margin-top: 2px;
}


/* ========================================================================
   STOPS
   ======================================================================== */

.stop {
    break-inside: avoid;
    margin: 0 0 0.22in;
}

.stop-heading {
    display: flex;
    align-items: flex-start;
    gap: 9px;
}

.stop-number {
    flex: 0 0 auto;
    width: 22px;
    height: 22px;
    line-height: 22px;
    text-align: center;
    border-radius: 50%;
    background: #7b8730;
    color: #ffffff;
    font: 700 9pt Arial, sans-serif;
    margin-top: 1px;
}

.stop-heading-main {
    flex: 1;
    min-width: 0;
}

.stop h3 {
    font-size: 12pt;
    margin: 0;
    font-weight: 700;
}

.stop-location {
    color: #777777;
    font-size: 9pt;
    margin-top: 1px;
}

.stop-date {
    white-space: nowrap;
    color: #555555;
    font: 9pt Arial, sans-serif;
    padding-top: 2px;
}

.metrics {
    display: flex;
    flex-wrap: wrap;
    gap: 5px 16px;
    margin: 6px 0 5px 31px;
    color: #777777;
    font: 8.8pt Arial, sans-serif;
}

.metrics b {
    color: #7b8730;
}

.description {
    margin-left: 31px;
    color: #444444;
}

.notes {
    margin: 5px 0 0 31px;
    color: #666666;
    font-size: 9pt;
}

.optional-fields {
    display: flex;
    flex-wrap: wrap;
    gap: 5px 16px;
    margin: 7px 0 0 31px;
    color: #666666;
    font: 8.5pt Arial, sans-serif;
}

.optional-field {
    display: inline-flex;
    gap: 4px;
}

.field-label {
    font-weight: 700;
    color: #7b8730;
}


/* ========================================================================
   PHOTOS
   ======================================================================== */

.photos {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
    margin: 10px 0 0 31px;
}

.photo-figure {
    margin: 0;
    break-inside: avoid;
}

.photo-figure img {
    width: 100%;
    max-height: 3.1in;
    object-fit: cover;
    border-radius: 4px;
    display: block;
}


/* ========================================================================
   FOOTER
   ======================================================================== */

.footer {
    border-top: 1px solid #e5e5e5;
    margin-top: .25in;
    padding-top: 6px;
    display: flex;
    justify-content: space-between;
    gap: 15px;
    color: #888888;
    font: 7.5pt Arial, sans-serif;
}


/* ========================================================================
   SCREEN
   ======================================================================== */

@media screen {

    body {
        background: #eeeeec;
    }

    .report {
        min-height: 100vh;
        background: white;
        box-shadow: 0 2px 20px rgba(0,0,0,.08);
    }

}


/* ========================================================================
   RESPONSIVE
   ======================================================================== */

@media screen and (max-width: 700px) {

    .report {
        padding: 0.25in 0.20in;
    }

    .topline {
        font-size: 7pt;
    }

    .brand {
        font-size: 19pt;
    }

    .hero h1 {
        font-size: 23pt;
    }

    .map-container,
    .map-container arcgis-embedded-map {
        height: 500px;
    }

    .stop-heading {
        gap: 7px;
    }

    .stop-date {
        font-size: 8pt;
    }

    .photos {
        grid-template-columns: 1fr;
    }

}


/* ========================================================================
   PRINT
   ======================================================================== */

@media print {

    body {
        background: white;
    }

    .report {
        max-width: none;
        padding: 0;
        box-shadow: none;
    }

    /*
       The ArcGIS map is interactive only in the HTML version.
       Hide it when printing so the report doesn't attempt to print
       the live web application.
    */

    .map-wrapper {
        display: none !important;
    }

}
"""


# ============================================================================
# BUILD REPORT
# ============================================================================

def build_report(
    stops: list[Stop],
    layer_info: dict[str, Any],
    args: argparse.Namespace,
) -> str:

    title = get_trip_title(
        stops,
        args,
    )

    # ----------------------------------------------------------------------
    # TOTAL TRIP MILEAGE
    # ----------------------------------------------------------------------

    cumulative_miles = numeric_values(
        stops,
        "MilesTotal",
    )

    if cumulative_miles:

        total_trip_miles = max(
            cumulative_miles
        )

    else:

        total_trip_miles = total_numeric(
            stops,
            "MilesTotal",
        )

    # ----------------------------------------------------------------------
    # SEGMENT MILEAGE
    # ----------------------------------------------------------------------

    total_segment_miles = total_numeric(
        stops,
        "MilesSince",
    )

    if total_segment_miles == 0:

        total_segment_miles = total_numeric(
            stops,
            "Miles_From_Previous",
        )

    # ----------------------------------------------------------------------
    # GROUPS
    # ----------------------------------------------------------------------

    groups = group_by_segment(
        stops
    )

    # ----------------------------------------------------------------------
    # PHOTOS
    # ----------------------------------------------------------------------

    image_count = sum(
        len(stop.attachments)
        for stop in stops
    )

    # ----------------------------------------------------------------------
    # GENERATED DATE
    # ----------------------------------------------------------------------

    generated = datetime.now().strftime(
        "%m/%d/%Y %I:%M %p"
    ).lstrip("0")

    # ----------------------------------------------------------------------
    # START HTML
    # ----------------------------------------------------------------------

    parts = [
        "<!doctype html>",

        '<html lang="en">',

        "<head>",

        '<meta charset="utf-8">',

        (
            '<meta name="viewport" '
            'content="width=device-width,initial-scale=1">'
        ),

        (
            '<meta name="description" '
            f'content="{esc(title)} — {esc(BRAND)}">'
        ),

        # Intentionally unlisted: not linked from site nav, and excluded
        # from search indexing so it's only reachable via direct link.
        '<meta name="robots" content="noindex, nofollow">',

        (
            '<title>'
            f'{esc(title)} — {esc(BRAND)}'
            '</title>'
        ),

        f"<style>{CSS}</style>",

        # ------------------------------------------------------------------
        # ArcGIS Embeddable Map Component
        # ------------------------------------------------------------------

        (
            '<script type="module" '
            f'src="{html.escape(ARCGIS_COMPONENT_SCRIPT, quote=True)}">'
            '</script>'
        ),

        "</head>",

        "<body>",

        '<main class="report">',

        # ------------------------------------------------------------------
        # Topline
        # ------------------------------------------------------------------

        (
            '<div class="topline">'
            f'<span>{esc(generated)}</span>'
            f'<span>{esc(AUTHORS)}</span>'
            '</div>'
        ),

        # ------------------------------------------------------------------
        # Brand
        # ------------------------------------------------------------------

        f'<div class="brand">{esc(BRAND)}</div>',

        (
            '<div class="brand-subtitle">'
            f'<a href="{html.escape(WEBSITE_URL, quote=True)}">'
            f'{esc(WEBSITE_URL)}'
            '</a>'
            '</div>'
        ),

        # ------------------------------------------------------------------
        # Hero
        # ------------------------------------------------------------------

        '<section class="hero">',

        f"<h1>{esc(title)}</h1>",

        (
            '<div class="date-range">'
            f'{esc(date_range_text(stops))}'
            '</div>'
        ),

        '<div class="summary">',

        (
            '<span class="summary-item">'
            f'<strong>'
            f'{format_number(total_trip_miles)}'
            f'</strong> mi'
            '</span>'
        ),

        (
            '<span class="summary-item">'
            f'<strong>{len(stops):,}</strong> stops'
            '</span>'
        ),

        (
            '<span class="summary-item">'
            f'<strong>{len(groups):,}</strong> sections'
            '</span>'
        ),

        (
            '<span class="summary-item">'
            f'<strong>{image_count:,}</strong> photos'
            '</span>'
        ),

        '</div>',

        '</section>',

        # ------------------------------------------------------------------
        # INTERACTIVE ARCGIS MAP
        # ------------------------------------------------------------------

        build_arcgis_map_embed(),
    ]

    # =========================================================================
    # SECTIONS
    # =========================================================================

    running_index = 0

    for (
        section_name,
        section_stops,
    ) in groups:

        # ---------------------------------------------------------------
        # Segment mileage
        # ---------------------------------------------------------------

        section_miles_values = numeric_values(
            section_stops,
            "MilesSince",
        )

        if not section_miles_values:

            section_miles_values = numeric_values(
                section_stops,
                "Miles_From_Previous",
            )

        section_miles = sum(
            section_miles_values
        )

        # ---------------------------------------------------------------
        # Section header
        # ---------------------------------------------------------------

        parts.extend(
            [
                '<section class="section">',

                '<div class="section-header">',

                (
                    '<h2 class="section-title">'
                    f'{esc(section_name)}'
                    '</h2>'
                ),

                (
                    '<div class="section-summary">'
                    f'{len(section_stops):,} stops'
                    + (
                        f' · '
                        f'{format_number(section_miles, 1)} mi'
                        if section_miles
                        else ""
                    )
                    + '</div>'
                ),

                '</div>',
            ]
        )

        # ---------------------------------------------------------------
        # Stops
        # ---------------------------------------------------------------

        for stop in section_stops:

            running_index += 1

            parts.append(
                build_stop_html(
                    stop,
                    running_index,
                )
            )

        parts.append(
            "</section>"
        )

    # =========================================================================
    # FOOTER
    # =========================================================================

    parts.extend(
        [
            '<footer class="footer">',

            (
                '<span>'
                f'{esc(BRAND)} · '
                f'{esc(WEBSITE_URL)}'
                '</span>'
            ),

            (
                '<span>'
                f'{len(stops):,} stops · '
                f'{image_count:,} photos'
                '</span>'
            ),

            '</footer>',

            '</main>',

            '</body>',

            '</html>',
        ]
    )

    return "".join(parts)


# ============================================================================
# OPTIONAL PDF
# ============================================================================

def generate_pdf(
    html_path: Path,
    pdf_path: Path,
) -> None:

    try:

        from playwright.sync_api import (
            sync_playwright
        )

    except ImportError as exc:

        raise ReportError(
            "Playwright is not installed.\n\n"
            "Run:\n"
            "  python -m pip install playwright\n"
            "  python -m playwright install chromium"
        ) from exc

    with sync_playwright() as playwright:

        browser = playwright.chromium.launch()

        page = browser.new_page()

        page.goto(
            html_path.resolve().as_uri(),
            wait_until="networkidle",
            timeout=120000,
        )

        page.wait_for_timeout(
            1500
        )

        # Wait for normal images.
        page.evaluate(
            """
            () => Promise.all(
                Array.from(document.images).map(img => {
                    if (img.complete) {
                        return Promise.resolve();
                    }

                    return new Promise(resolve => {
                        img.onload = resolve;
                        img.onerror = resolve;
                    });
                })
            )
            """
        )

        page.pdf(
            path=str(
                pdf_path.resolve()
            ),
            format="Letter",
            print_background=True,
            prefer_css_page_size=True,
            margin={
                "top": "0.42in",
                "right": "0.42in",
                "bottom": "0.42in",
                "left": "0.42in",
            },
        )

        browser.close()


# ============================================================================
# INSPECTION
# ============================================================================

def inspect_layer(
    layer_info: dict[str, Any],
) -> None:

    print()
    print("=" * 88)
    print(
        "PUBLIC LAND LOVERS — "
        "PublicLandLovers_Stops"
    )
    print("=" * 88)

    print(
        "Geometry:       ",
        layer_info.get(
            "geometryType"
        ),
    )

    spatial_reference = (
        layer_info.get(
            "extent",
            {}
        ).get(
            "spatialReference",
            {}
        ).get(
            "wkid"
        )
    )

    if spatial_reference is None:

        spatial_reference = (
            layer_info.get(
                "spatialReference",
                {}
            ).get(
                "wkid"
            )
        )

    print(
        "Spatial ref:    ",
        spatial_reference,
    )

    print(
        "Attachments:    ",
        layer_info.get(
            "hasAttachments"
        ),
    )

    print(
        "Record limit:   ",
        layer_info.get(
            "maxRecordCount"
        ),
    )

    print()
    print("Fields")
    print("-" * 88)

    for field in layer_info.get(
        "fields",
        [],
    ):

        print(
            f"{field.get('name'):30} | "
            f"{field.get('alias')} | "
            f"{field.get('type')}"
        )

    print()
    print(
        "Report fields intentionally used"
    )
    print("-" * 88)

    for field in [
        "name",
        "description",
        "DateArrived",
        "MilesTotal",
        "MilesSince",
        "Miles_From_Previous",
        "Drive_Time_From_Previous",
        "Drive_Minutes",
        "Segment",
        "Location",
        "State_1",
        "Notes",
        "camp_spot",
        "Land_Ownership",
        "Camp_spot_type",
    ]:

        print(
            f"  {field}"
        )

    print()
    print(
        "Embedded ArcGIS Web Map"
    )
    print("-" * 88)

    print(
        f"  Item ID: {WEB_MAP_ITEM_ID}"
    )

    print(
        f"  Portal:  {ARCGIS_PORTAL_URL}"
    )

    print(
        f"  Center:  {ARCGIS_MAP_CENTER}"
    )

    print(
        f"  Scale:   {ARCGIS_MAP_SCALE}"
    )

    print()
    print(
        "Excluded by design:"
    )

    print(
        "  DateLeft"
    )

    print(
        "  Location__slept_"
    )

    print(
        "  internal/admin fields"
    )

    print()


# ============================================================================
# MAIN
# ============================================================================

def main() -> int:

    parser = argparse.ArgumentParser(
        description=(
            "Generate a Public Land Lovers "
            "trip report from the "
            "PublicLandLovers_Stops "
            "Feature Service."
        )
    )

    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Inspect the layer and exit.",
    )

    parser.add_argument(
        "--pdf",
        action="store_true",
        help=(
            "Generate a PDF in addition "
            "to HTML."
        ),
    )

    parser.add_argument(
        "--segment",
        help=(
            "Only include one Segment "
            "value in the report."
        ),
    )

    parser.add_argument(
        "--state",
        help=(
            "Only include one State_1 "
            "value in the report."
        ),
    )

    parser.add_argument(
        "--start-date",
        help=(
            "Only include DateArrived "
            "on/after YYYY-MM-DD."
        ),
    )

    parser.add_argument(
        "--end-date",
        help=(
            "Only include DateArrived "
            "on/before YYYY-MM-DD."
        ),
    )

    parser.add_argument(
        "--title",
        help=(
            "Override the report title."
        ),
    )

    parser.add_argument(
        "--output",
        default=str(
            OUTPUT_DIR
        ),
        help=(
            "Output directory."
        ),
    )

    args = parser.parse_args()

    # ----------------------------------------------------------------------
    # OUTPUT
    # ----------------------------------------------------------------------

    output = Path(
        args.output
    )

    output.mkdir(
        parents=True,
        exist_ok=True,
    )

    html_path = (
        output / "index.html"
    )

    pdf_path = (
        output
        / "public_land_lovers_report.pdf"
    )

    # ----------------------------------------------------------------------
    # HTTP SESSION
    # ----------------------------------------------------------------------

    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": (
                "PublicLandLoversTripReport/1.1 "
                "(https://publiclandlovers.com)"
            )
        }
    )

    # ----------------------------------------------------------------------
    # LAYER
    # ----------------------------------------------------------------------

    print(
        "Reading ArcGIS layer..."
    )

    layer_info = get_layer_info(
        session
    )

    if args.inspect:

        inspect_layer(
            layer_info
        )

        return 0

    # ----------------------------------------------------------------------
    # WHERE
    # ----------------------------------------------------------------------

    where = build_where(
        args
    )

    print(
        "Query:"
    )

    print(
        f"  {where}"
    )

    # ----------------------------------------------------------------------
    # STOPS
    # ----------------------------------------------------------------------

    print(
        "Downloading stops..."
    )

    features = query_stops(
        session,
        where,
    )

    print(
        f"Found {len(features):,} "
        f"stop records."
    )

    if not features:

        raise ReportError(
            "No stops matched the "
            "selected filters."
        )

    # ----------------------------------------------------------------------
    # BUILD STOP OBJECTS
    # ----------------------------------------------------------------------

    stops: list[Stop] = []

    for feature in features:

        attributes = (
            feature.get(
                "attributes"
            )
            or {}
        )

        object_id = attributes.get(
            "OBJECTID"
        )

        if object_id is None:
            continue

        try:

            object_id = int(
                object_id
            )

        except (
            TypeError,
            ValueError,
        ):

            continue

        stops.append(
            Stop(
                object_id=object_id,
                attributes=attributes,
                geometry=feature.get(
                    "geometry"
                ),
                attachments=[],
            )
        )

    # ----------------------------------------------------------------------
    # CHRONOLOGICAL ORDER
    # ----------------------------------------------------------------------

    stops.sort(
        key=stop_sort_key
    )

    # ----------------------------------------------------------------------
    # ATTACHMENTS
    # ----------------------------------------------------------------------

    print(
        "Downloading photo "
        "attachment information..."
    )

    attachment_map = query_attachments(
        session,
        [
            stop.object_id
            for stop in stops
        ],
    )

    for stop in stops:

        stop.attachments = (
            attachment_map.get(
                stop.object_id,
                [],
            )
        )

    photo_count = sum(
        len(stop.attachments)
        for stop in stops
    )

    print(
        f"Found {photo_count:,} "
        f"image attachments."
    )

    # ----------------------------------------------------------------------
    # BUILD REPORT
    # ----------------------------------------------------------------------

    print(
        "Generating HTML..."
    )

    report = build_report(
        stops,
        layer_info,
        args,
    )

    html_path.write_text(
        report,
        encoding="utf-8",
    )

    print()
    print(
        "HTML created:"
    )

    print(
        f"  {html_path.resolve()}"
    )

    # ----------------------------------------------------------------------
    # OPTIONAL PDF
    # ----------------------------------------------------------------------

    if args.pdf:

        print()
        print(
            "Generating PDF with "
            "Chromium..."
        )

        generate_pdf(
            html_path,
            pdf_path,
        )

        print(
            "PDF created:"
        )

        print(
            f"  {pdf_path.resolve()}"
        )

    print()
    print(
        "Done."
    )

    return 0


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":

    try:

        raise SystemExit(
            main()
        )

    except KeyboardInterrupt:

        print(
            "\nCancelled."
        )

        raise SystemExit(
            130
        )

    except Exception as exc:

        print()
        print(
            "ERROR"
        )
        print(
            "=" * 80
        )
        print(
            exc
        )

        raise SystemExit(
            1
        )