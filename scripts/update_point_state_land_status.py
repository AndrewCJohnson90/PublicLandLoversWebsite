#!/usr/bin/env python3
"""
update_point_state_land_status.py

Automatically populates State_1 and Land_Ownership for points in an
existing ArcGIS Online Feature Service.

NO NEW OUTPUT FEATURE SERVICE IS CREATED.

Sources:
    State:
        U.S. Census Bureau TIGERweb
        TIGERweb State_County / States layer

    Surface management:
        Bureau of Land Management
        BLM National Surface Management Agency (SMA)

Workflow:

    Existing point Feature Service
            |
            +--> Census State polygon lookup
            |       |
            |       +--> State_1
            |
            +--> BLM SMA Identify
                    |
                    +--> Land_Ownership

The BLM result represents Surface Management Agency, NOT a definitive
land-ownership boundary.

Processing behavior:
    - Only processes points that need enrichment by default.
    - Existing populated values are preserved unless FORCE_REPROCESS=True.
    - Updates the existing feature service in batches.
    - Does not create a new layer.
    - Produces a CSV-style summary in the console.
    - Includes retry handling for external services.
    - Handles failures on individual points without stopping the whole job.
    - Can be run manually or from GitHub Actions.

Requirements:
    pip install arcgis requests

Authentication:
    Preferred for ArcGIS Online:
        arcpy / ArcGIS API profile named "van_life_profile"

    The script uses:
        GIS(profile=AGOL_PROFILE)

    If your profile has a different name, change AGOL_PROFILE below.

"""

import csv
import os
import sys
import time
import logging
from datetime import datetime

import requests
from arcgis.gis import GIS


# ============================================================================
# CONFIGURATION
# ============================================================================

# ---------------------------------------------------------------------------
# ArcGIS Online
# ---------------------------------------------------------------------------

AGOL_PROFILE = "van_life_profile"

# Existing production point layer.
POINTS_LAYER_URL = (
    "https://services8.arcgis.com/"
    "KzyxLudI6Hn5u85O/arcgis/rest/services/"
    "Janyne_and_Andrew_VanLife/FeatureServer/0"
)


# ---------------------------------------------------------------------------
# Fields in your existing point layer
# ---------------------------------------------------------------------------

OBJECTID_FIELD = "OBJECTID"

STATE_FIELD = "State_1"

LAND_FIELD = "Land_Ownership"

# Optional processing-status field.
#
# If this field exists, the script will use it to track processing.
#
# Recommended values:
#     OK
#     REVIEW
#     NO_STATE
#     NO_LAND_MATCH
#     ERROR
#
# If you do NOT have this field, set it to None.
PROCESS_FIELD = "Spatial_Check"


# ---------------------------------------------------------------------------
# Source services
# ---------------------------------------------------------------------------

CENSUS_STATE_LAYER_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/"
    "TIGERweb/State_County/MapServer/0"
)

BLM_MAPSERVER_URL = (
    "https://gis.blm.gov/arcgis/rest/services/"
    "lands/BLM_Natl_SMA_Cached_with_PriUnk/MapServer"
)

BLM_IDENTIFY_URL = f"{BLM_MAPSERVER_URL}/identify"


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

# False = only process records that need enrichment.
# True  = recheck every point.
FORCE_REPROCESS = False

# Number of ArcGIS Online updates submitted per batch.
UPDATE_BATCH_SIZE = 100

# Number of points retrieved from the production layer at once.
QUERY_BATCH_SIZE = 500

# Maximum number of HTTP retries for external services.
MAX_RETRIES = 3

# Seconds between retries.
RETRY_DELAY = 2

# BLM Identify pixel tolerance.
#
# This is NOT a geographic tolerance in feet/meters.
# It is the Identify tool's screen-pixel tolerance.
#
# A value of 1-3 is generally appropriate for a point lookup.
BLM_IDENTIFY_TOLERANCE = 2

# Map display size used by BLM Identify.
BLM_IMAGE_WIDTH = 512
BLM_IMAGE_HEIGHT = 512

# Size of the map extent around each point in Web Mercator.
#
# 10,000 meters gives the Identify service enough context while remaining
# relatively local.
BLM_IDENTIFY_HALF_SIZE_METERS = 10000

# Timeout for external web requests.
REQUEST_TIMEOUT = 60

# Output CSV summary.
SUMMARY_CSV = "state_land_status_update_summary.csv"


# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("state_land_status")


# ============================================================================
# HTTP SESSION
# ============================================================================

session = requests.Session()

session.headers.update(
    {
        "User-Agent": (
            "PublicLandLovers-GIS-SpatialEnrichment/1.0 "
            "(ArcGIS Online automated workflow)"
        )
    }
)


# ============================================================================
# HELPERS
# ============================================================================

def request_json(url, params, description="request"):
    """
    Perform an HTTP GET and return JSON with retries.
    """

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):

        try:

            response = session.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

            response.raise_for_status()

            data = response.json()

            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(
                    f"{description} returned ArcGIS error: "
                    f"{data['error']}"
                )

            return data

        except Exception as exc:

            last_error = exc

            log.warning(
                "%s failed (attempt %s/%s): %s",
                description,
                attempt,
                MAX_RETRIES,
                exc,
            )

            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    raise RuntimeError(
        f"{description} failed after {MAX_RETRIES} attempts: "
        f"{last_error}"
    )


def is_blank(value):
    """
    Return True if a field is empty/null/whitespace.
    """

    if value is None:
        return True

    if isinstance(value, str):
        return value.strip() == ""

    return False


def clean_string(value):
    """
    Convert a value to a clean string.
    """

    if value is None:
        return None

    return str(value).strip()


# ============================================================================
# WEB MERCATOR
# ============================================================================

def lonlat_to_web_mercator(lon, lat):
    """
    Convert WGS84 longitude/latitude to EPSG:3857.

    This avoids requiring arcpy or pyproj just for this transformation.
    """

    import math

    x = lon * 20037508.34 / 180.0

    y = math.log(
        math.tan(
            (90.0 + lat) * math.pi / 360.0
        )
    ) / (math.pi / 180.0)

    y = y * 20037508.34 / 180.0

    return x, y


# ============================================================================
# GEOMETRY EXTRACTION
# ============================================================================

def extract_lon_lat(feature):
    """
    Extract longitude/latitude from an ArcGIS feature geometry.

    Supports:
        - x/y geometry
        - geographic coordinates
        - Web Mercator coordinates

    The production layer is expected to be point geometry.
    """

    geometry = feature.get("geometry")

    if not geometry:
        return None, None

    x = geometry.get("x")
    y = geometry.get("y")

    if x is None or y is None:
        return None, None

    spatial_reference = geometry.get("spatialReference", {})

    wkid = spatial_reference.get("latestWkid")

    if wkid is None:
        wkid = spatial_reference.get("wkid")

    # WGS84
    if wkid in (4326, None):

        # If no SR was supplied, assume WGS84.
        return float(x), float(y)

    # Web Mercator
    if wkid in (3857, 102100):

        import math

        lon = (
            float(x)
            / 20037508.34
            * 180.0
        )

        lat = (
            float(y)
            / 20037508.34
            * 180.0
        )

        lat = (
            180.0
            / math.pi
            * (
                2.0 * math.atan(
                    math.exp(
                        lat * math.pi / 180.0
                    )
                )
                - math.pi / 2.0
            )
        )

        return lon, lat

    raise ValueError(
        f"Unsupported point spatial reference WKID: {wkid}"
    )


# ============================================================================
# CENSUS STATE LOOKUP
# ============================================================================

def lookup_state(lon, lat):
    """
    Find the Census state polygon containing the point.

    Returns:
        {
            "state": "Utah",
            "state_abbr": "UT",
            "geoid": "49"
        }

    or None if no state is found.
    """

    params = {
        "f": "json",
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "NAME,STATE,GEOID,STUSAB",
        "returnGeometry": "false",
        "resultRecordCount": 5,
    }

    data = request_json(
        f"{CENSUS_STATE_LAYER_URL}/query",
        params,
        description="Census state lookup",
    )

    features = data.get("features", [])

    if not features:
        return None

    attributes = features[0].get("attributes", {})

    state_name = (
        attributes.get("NAME")
        or attributes.get("BASENAME")
    )

    state_abbr = (
        attributes.get("STUSAB")
        or attributes.get("STATE")
    )

    geoid = attributes.get("GEOID")

    if is_blank(state_name):
        return None

    return {
        "state": clean_string(state_name),
        "state_abbr": clean_string(state_abbr),
        "geoid": clean_string(geoid),
    }


# ============================================================================
# BLM SMA LOOKUP
# ============================================================================

def build_blm_extent(x_mercator, y_mercator):
    """
    Build a small Web Mercator map extent around the point.
    """

    half = BLM_IDENTIFY_HALF_SIZE_METERS

    return (
        f"{x_mercator - half},"
        f"{y_mercator - half},"
        f"{x_mercator + half},"
        f"{y_mercator + half}"
    )


def normalize_blm_category(layer_name):
    """
    Convert BLM layer names to concise values suitable for the
    Land_Ownership field.

    The source service's categories are documented by BLM.
    """

    if not layer_name:
        return None

    name = layer_name.strip()

    normalized = name.lower()

    mappings = [
        (
            "bureau of land management",
            "BLM",
        ),
        (
            "national park service",
            "NPS",
        ),
        (
            "us forest service",
            "USFS",
        ),
        (
            "us fish and wildlife",
            "USFWS",
        ),
        (
            "bureau of reclamation",
            "BOR",
        ),
        (
            "bureau of indian affairs",
            "BIA",
        ),
        (
            "department of defense",
            "DOD",
        ),
        (
            "other federal",
            "Other Federal",
        ),
        (
            "state",
            "State",
        ),
        (
            "local",
            "Local",
        ),
        (
            "private or unknown",
            "Private or Unknown",
        ),
        (
            "alaska native allotment",
            "Alaska Native Allotment",
        ),
        (
            "alaska native lands",
            "Alaska Native Lands",
        ),
    ]

    for source_text, output_value in mappings:

        if source_text in normalized:
            return output_value

    # Strip common parenthetical notation if it is an otherwise
    # recognizable category.
    return name.replace(" (", " - ").replace(")", "")


def lookup_blm_sma(lon, lat):
    """
    Use the BLM cached MapServer Identify operation to determine the
    Surface Management Agency at a point.

    IMPORTANT:
        This is SMA information, not definitive ownership.

    Returns:
        {
            "value": "BLM",
            "layer_name": "...",
            "layer_id": 22,
            "details": {...}
        }

    or None.
    """

    x_mercator, y_mercator = lonlat_to_web_mercator(
        lon,
        lat,
    )

    map_extent = build_blm_extent(
        x_mercator,
        y_mercator,
    )

    geometry = (
        f'{{"x":{x_mercator},'
        f'"y":{y_mercator},'
        f'"spatialReference":{{"wkid":3857}}}}'
    )

    params = {
        "f": "json",
        "geometry": geometry,
        "geometryType": "esriGeometryPoint",
        "sr": 3857,

        # FEATURES group layer.
        #
        # This allows Identify to inspect the underlying SMA feature
        # layers rather than just the cached tile image.
        "layers": "all:17",

        "tolerance": BLM_IDENTIFY_TOLERANCE,
        "mapExtent": map_extent,
        "imageDisplay": (
            f"{BLM_IMAGE_WIDTH},"
            f"{BLM_IMAGE_HEIGHT},96"
        ),
        "returnGeometry": "false",
        "returnFieldName": "true",
    }

    data = request_json(
        BLM_IDENTIFY_URL,
        params,
        description="BLM SMA Identify",
    )

    results = data.get("results", [])

    if not results:
        return None

    # We want the actual feature layer result, not a group layer.
    for result in results:

        layer_name = result.get("layerName")

        if not layer_name:
            continue

        value = normalize_blm_category(
            layer_name
        )

        if value:
            attributes = result.get(
                "attributes",
                {},
            )

            return {
                "value": value,
                "layer_name": layer_name,
                "layer_id": result.get("layerId"),
                "details": attributes,
            }

    return None


# ============================================================================
# FEATURE QUERY
# ============================================================================

def get_point_layer(gis):
    """
    Get the existing point Feature Layer.
    """

    from arcgis.features import FeatureLayer

    return FeatureLayer(
        POINTS_LAYER_URL,
        gis=gis,
    )


def get_layer_fields(layer):
    """
    Return a dictionary of field name -> field definition.
    """

    properties = layer.properties

    return {
        field["name"]: field
        for field in properties.fields
    }


def validate_fields(layer):
    """
    Make sure required fields exist before making any updates.
    """

    fields = get_layer_fields(layer)

    required = [
        OBJECTID_FIELD,
        STATE_FIELD,
        LAND_FIELD,
    ]

    if PROCESS_FIELD:
        required.append(PROCESS_FIELD)

    missing = [
        field
        for field in required
        if field not in fields
    ]

    if missing:

        raise RuntimeError(
            "The following fields are missing from the "
            f"production layer: {', '.join(missing)}\n\n"
            "Either create these fields or change the "
            "configuration at the top of this script."
        )

    log.info(
        "Required fields verified: %s",
        ", ".join(required),
    )


# ============================================================================
# DETERMINE WHICH POINTS NEED PROCESSING
# ============================================================================

def build_where_clause():
    """
    Build the WHERE clause used for incremental processing.

    If PROCESS_FIELD exists:
        Process NULL / blank / error / review records.

    State and land values are also checked later, so the script remains
    useful even if the processing status field is not present.
    """

    if FORCE_REPROCESS:
        return "1=1"

    if PROCESS_FIELD:
        return (
            f"({PROCESS_FIELD} IS NULL "
            f"OR {PROCESS_FIELD} = '' "
            f"OR {PROCESS_FIELD} = 'ERROR' "
            f"OR {PROCESS_FIELD} = 'REVIEW' "
            f"OR {PROCESS_FIELD} = 'NO_STATE' "
            f"OR {PROCESS_FIELD} = 'NO_LAND_MATCH')"
        )

    return (
        f"({STATE_FIELD} IS NULL "
        f"OR {STATE_FIELD} = '' "
        f"OR {LAND_FIELD} IS NULL "
        f"OR {LAND_FIELD} = '')"
    )


def get_features_to_process(layer):
    """
    Retrieve all point features needing processing.
    """

    where = build_where_clause()

    log.info(
        "Processing WHERE clause: %s",
        where,
    )

    all_features = []

    result_offset = 0

    while True:

        log.info(
            "Retrieving features %s-%s...",
            result_offset + 1,
            result_offset + QUERY_BATCH_SIZE,
        )

        result = layer.query(
            where=where,
            out_fields="*",
            return_geometry=True,
            result_offset=result_offset,
            result_record_count=QUERY_BATCH_SIZE,
            return_exceeded_limit_features=True,
        )

        features = result.features

        if not features:
            break

        all_features.extend(features)

        if len(features) < QUERY_BATCH_SIZE:
            break

        result_offset += QUERY_BATCH_SIZE

    return all_features


# ============================================================================
# PROCESS A SINGLE POINT
# ============================================================================

def process_feature(feature):
    """
    Process one point.

    Returns:
        {
            "update": {...},
            "result": {...}
        }
    """

    attributes = feature.attributes

    objectid = attributes.get(
        OBJECTID_FIELD
    )

    try:

        lon, lat = extract_lon_lat(
            feature.as_dict
        )

        if lon is None or lat is None:

            return {
                "update": {
                    OBJECTID_FIELD: objectid,
                    STATE_FIELD: None,
                    LAND_FIELD: None,
                    **(
                        {PROCESS_FIELD: "ERROR"}
                        if PROCESS_FIELD
                        else {}
                    ),
                },
                "result": {
                    "objectid": objectid,
                    "status": "ERROR",
                    "reason": "Missing geometry",
                    "state": "",
                    "land": "",
                },
            }

        log.info(
            "OBJECTID %s | %.6f, %.6f",
            objectid,
            lon,
            lat,
        )

        # ---------------------------------------------------------------
        # State
        # ---------------------------------------------------------------

        state_result = lookup_state(
            lon,
            lat,
        )

        state_name = None

        if state_result:
            state_name = state_result["state"]

        # ---------------------------------------------------------------
        # BLM SMA
        # ---------------------------------------------------------------

        blm_result = lookup_blm_sma(
            lon,
            lat,
        )

        land_value = None

        if blm_result:
            land_value = blm_result["value"]

        # ---------------------------------------------------------------
        # Determine processing status
        # ---------------------------------------------------------------

        if state_name and land_value:

            status = "OK"

        elif state_name and not land_value:

            status = "NO_LAND_MATCH"

        elif not state_name:

            status = "NO_STATE"

        else:

            status = "REVIEW"

        # ---------------------------------------------------------------
        # Build update
        # ---------------------------------------------------------------

        update = {
            OBJECTID_FIELD: objectid,
        }

        # Only write a state value if one was actually found.
        if state_name:
            update[STATE_FIELD] = state_name

        # Only write a land value if one was actually found.
        if land_value:
            update[LAND_FIELD] = land_value

        if PROCESS_FIELD:
            update[PROCESS_FIELD] = status

        result = {
            "objectid": objectid,
            "status": status,
            "reason": "",
            "state": state_name or "",
            "land": land_value or "",
        }

        return {
            "update": update,
            "result": result,
        }

    except Exception as exc:

        log.exception(
            "OBJECTID %s failed",
            objectid,
        )

        update = {
            OBJECTID_FIELD: objectid,
        }

        if PROCESS_FIELD:
            update[PROCESS_FIELD] = "ERROR"

        return {
            "update": update,
            "result": {
                "objectid": objectid,
                "status": "ERROR",
                "reason": str(exc),
                "state": "",
                "land": "",
            },
        }


# ============================================================================
# APPLY UPDATES
# ============================================================================

def apply_updates(layer, updates):
    """
    Update the existing production Feature Service in batches.

    ArcGIS Python API versions can be picky about the format supplied
    to edit_features(). Convert each attribute dictionary into an
    explicit Feature object before submitting the batch.
    """

    from arcgis.features import Feature

    if not updates:
        return []

    results = []

    for start in range(
        0,
        len(updates),
        UPDATE_BATCH_SIZE,
    ):

        batch = updates[
            start:start + UPDATE_BATCH_SIZE
        ]

        log.info(
            "Updating production layer: %s-%s of %s",
            start + 1,
            min(
                start + len(batch),
                len(updates),
            ),
            len(updates),
        )

        # ---------------------------------------------------------------
        # Convert our dictionaries into ArcGIS Feature objects.
        #
        # Each update dictionary should look like:
        #
        # {
        #     "OBJECTID": 123,
        #     "State_1": "Utah",
        #     "Land_Ownership": "BLM",
        #     "Spatial_Check": "OK"
        # }
        #
        # edit_features() expects the OBJECTID inside the attributes.
        # ---------------------------------------------------------------

        feature_updates = []

        for update in batch:

            attributes = dict(update)

            feature_updates.append(
                Feature(
                    attributes=attributes
                )
            )

        # ---------------------------------------------------------------
        # Submit the batch.
        # ---------------------------------------------------------------

        try:

            response = layer.edit_features(
                updates=feature_updates,
                rollback_on_failure=False,
            )

        except Exception as exc:

            log.error(
                "Batch update failed for records %s-%s.",
                start + 1,
                min(
                    start + len(batch),
                    len(updates),
                ),
            )

            log.error(
                "ArcGIS error: %s",
                exc,
            )

            # -----------------------------------------------------------
            # If a 100-record batch fails, retry each feature individually.
            #
            # This is useful because one bad record or field value should
            # not prevent the other 99 records from being updated.
            # -----------------------------------------------------------

            log.warning(
                "Retrying failed batch one feature at a time..."
            )

            for update in batch:

                objectid = update.get(
                    OBJECTID_FIELD
                )

                try:

                    single_feature = Feature(
                        attributes=dict(update)
                    )

                    single_response = (
                        layer.edit_features(
                            updates=[single_feature],
                            rollback_on_failure=False,
                        )
                    )

                    single_results = (
                        single_response.get(
                            "updateResults",
                            [],
                        )
                    )

                    if single_results:

                        result = single_results[0]

                        results.append(result)

                        if not result.get("success"):

                            log.error(
                                "Individual update failed: "
                                "OBJECTID=%s | %s",
                                objectid,
                                result.get("error"),
                            )

                    else:

                        log.error(
                            "No update result returned for "
                            "OBJECTID=%s",
                            objectid,
                        )

                except Exception as single_exc:

                    log.error(
                        "Individual update exception: "
                        "OBJECTID=%s | %s",
                        objectid,
                        single_exc,
                    )

            continue

        # ---------------------------------------------------------------
        # Normal successful batch.
        # ---------------------------------------------------------------

        update_results = response.get(
            "updateResults",
            [],
        )

        results.extend(
            update_results
        )

        failures = [
            item
            for item in update_results
            if not item.get("success")
        ]

        if failures:

            for failure in failures:

                log.error(
                    "ArcGIS update failed: OBJECTID=%s error=%s",
                    failure.get("objectId"),
                    failure.get("error"),
                )

    return results
# ============================================================================
# CSV SUMMARY
# ============================================================================

def write_summary(results):
    """
    Write processing results to CSV.
    """

    if not results:
        return

    fieldnames = [
        "objectid",
        "status",
        "state",
        "land",
        "reason",
    ]

    with open(
        SUMMARY_CSV,
        "w",
        newline="",
        encoding="utf-8",
    ) as csvfile:

        writer = csv.DictWriter(
            csvfile,
            fieldnames=fieldnames,
        )

        writer.writeheader()

        writer.writerows(results)

    log.info(
        "Summary written to %s",
        SUMMARY_CSV,
    )


# ============================================================================
# MAIN
# ============================================================================

def main():

    start_time = datetime.now()

    log.info(
        "============================================================"
    )
    log.info(
        "Public Land Lovers Spatial Attribute Update"
    )
    log.info(
        "============================================================"
    )

    log.info(
        "Existing layer: %s",
        POINTS_LAYER_URL,
    )

    log.info(
        "State source: Census TIGERweb"
    )

    log.info(
        "Land source: BLM National SMA"
    )

    log.info(
        "Force reprocess: %s",
        FORCE_REPROCESS,
    )

    # ---------------------------------------------------------------------
    # Connect to ArcGIS Online
    # ---------------------------------------------------------------------

    log.info(
        "Connecting to ArcGIS Online profile '%s'...",
        AGOL_PROFILE,
    )

    try:

        gis = GIS(
            profile=AGOL_PROFILE
        )

    except Exception as exc:

        log.error(
            "Could not connect to ArcGIS Online profile '%s'.",
            AGOL_PROFILE,
        )

        log.error(
            "%s",
            exc,
        )

        log.error(
            "Make sure the ArcGIS Python API is installed and "
            "the profile exists."
        )

        sys.exit(1)

    log.info(
        "Connected as: %s",
        gis.users.me.username,
    )

    # ---------------------------------------------------------------------
    # Open production layer
    # ---------------------------------------------------------------------

    layer = get_point_layer(gis)

    validate_fields(layer)

    # ---------------------------------------------------------------------
    # Find records needing processing
    # ---------------------------------------------------------------------

    features = get_features_to_process(
        layer
    )

    total = len(features)

    if total == 0:

        log.info(
            "No points require spatial enrichment."
        )

        log.info(
            "Nothing was changed."
        )

        return

    log.info(
        "Found %s point(s) requiring processing.",
        total,
    )

    # ---------------------------------------------------------------------
    # Process
    # ---------------------------------------------------------------------

    updates = []
    results = []

    for index, feature in enumerate(
        features,
        start=1,
    ):

        log.info(
            "------------------------------------------------------------"
        )

        log.info(
            "Processing %s of %s",
            index,
            total,
        )

        processed = process_feature(
            feature
        )

        updates.append(
            processed["update"]
        )

        results.append(
            processed["result"]
        )

    # ---------------------------------------------------------------------
    # Update production layer
    # ---------------------------------------------------------------------

    log.info(
        "============================================================"
    )

    log.info(
        "Updating existing production layer..."
    )

    update_results = apply_updates(
        layer,
        updates,
    )

    # ---------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------

    write_summary(results)

    counts = {}

    for result in results:

        status = result["status"]

        counts[status] = (
            counts.get(status, 0)
            + 1
        )

    elapsed = (
        datetime.now()
        - start_time
    )

    log.info(
        "============================================================"
    )

    log.info(
        "PROCESSING COMPLETE"
    )

    log.info(
        "============================================================"
    )

    log.info(
        "Points examined: %s",
        total,
    )

    log.info(
        "ArcGIS update responses: %s",
        len(update_results),
    )

    for status, count in sorted(
        counts.items()
    ):

        log.info(
            "%-20s %s",
            status,
            count,
        )

    log.info(
        "Elapsed time: %s",
        elapsed,
    )

    log.info(
        "CSV summary: %s",
        SUMMARY_CSV,
    )

    log.info(
        "No output Feature Service was created."
    )


if __name__ == "__main__":
    main()