
#!/usr/bin/env python3
"""
update_point_state_land_status.py

Updates State_1 and Land_Ownership on the existing Public Land Lovers
van-life production Feature Service.

Workflow:
    1. Query existing production points.
    2. Determine each point's state using Census TIGERweb.
    3. Determine the land-management category using the BLM National
       Surface Management Agency (SMA) service.
    4. Update the existing production Feature Service in place.
    5. Write a CSV summary of the processing results.

Authentication:
    - GitHub Actions:
        Uses AGOL_USERNAME and AGOL_PASSWORD environment variables.
    - Local computer:
        Falls back to the ArcGIS Python API profile
        "van_life_profile".

No new Feature Service is created.
"""

import csv
import logging
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import requests
from arcgis.gis import GIS
from arcgis.features import Feature


# ============================================================
# CONFIGURATION
# ============================================================

AGOL_PROFILE = "van_life_profile"

# Existing production points layer
POINT_LAYER_URL = (
    "https://services8.arcgis.com/KzyxLudI6Hn5u85O/"
    "arcgis/rest/services/Janyne_and_Andrew_VanLife/FeatureServer/0"
)

# Fields in the existing production layer
OBJECTID_FIELD = "OBJECTID"
STATE_FIELD = "State_1"
LAND_FIELD = "Land_Ownership"

# Optional processing-status field.
#
# If your production layer does not have this field, set:
#
#     PROCESS_FIELD = None
#
PROCESS_FIELD = "Spatial_Check"


# ============================================================
# EXTERNAL DATA SOURCES
# ============================================================

CENSUS_STATE_LAYER_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/"
    "TIGERweb/State_County/MapServer/0"
)

BLM_MAPSERVER_URL = (
    "https://gis.blm.gov/arcgis/rest/services/lands/"
    "BLM_Natl_SMA_Cached_with_PriUnk/MapServer"
)

BLM_IDENTIFY_URL = f"{BLM_MAPSERVER_URL}/identify"


# ============================================================
# PROCESSING SETTINGS
# ============================================================

# False = process only records that have not successfully completed.
# True  = process every production point.
FORCE_REPROCESS = False

# Number of records sent to ArcGIS Online per edit operation.
UPDATE_BATCH_SIZE = 50

# Number of records retrieved from the production layer at a time.
QUERY_BATCH_SIZE = 500

# HTTP/API retry settings
MAX_RETRIES = 3
RETRY_DELAY = 2

# BLM Identify settings
BLM_IDENTIFY_TOLERANCE = 2
BLM_IMAGE_WIDTH = 512
BLM_IMAGE_HEIGHT = 512

# The Identify operation requires a map extent.
# 10,000 meters = approximately a 20 km x 20 km extent.
BLM_IDENTIFY_HALF_SIZE_METERS = 10000

REQUEST_TIMEOUT = 60

SUMMARY_CSV = "state_land_status_update_summary.csv"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("state_land_status")


# ============================================================
# ARC GIS ONLINE CONNECTION
# ============================================================

def connect_to_arcgis() -> GIS:
    """
    Connect to ArcGIS Online.

    GitHub Actions:
        Uses AGOL_USERNAME and AGOL_PASSWORD environment variables.

    Local execution:
        Falls back to the configured ArcGIS Python API profile.
    """

    username = os.getenv("AGOL_USERNAME")
    password = os.getenv("AGOL_PASSWORD")

    # --------------------------------------------------------
    # GitHub Actions / environment credentials
    # --------------------------------------------------------

    if username and password:
        log.info(
            "Connecting to ArcGIS Online using environment credentials..."
        )

        try:
            gis = GIS(
                "https://www.arcgis.com",
                username,
                password,
            )

            log.info(
                "Connected to ArcGIS Online as '%s'.",
                gis.users.me.username,
            )

            return gis

        except Exception as exc:
            log.error(
                "Could not connect to ArcGIS Online using "
                "AGOL_USERNAME / AGOL_PASSWORD."
            )
            log.error("%s", exc)
            raise

    # --------------------------------------------------------
    # Local ArcGIS profile fallback
    # --------------------------------------------------------

    log.info(
        "AGOL_USERNAME / AGOL_PASSWORD not found."
    )

    log.info(
        "Connecting using local ArcGIS profile '%s'...",
        AGOL_PROFILE,
    )

    try:
        gis = GIS(profile=AGOL_PROFILE)

        log.info(
            "Connected to ArcGIS Online as '%s'.",
            gis.users.me.username,
        )

        return gis

    except Exception as exc:
        log.error(
            "Could not connect to ArcGIS Online profile '%s'.",
            AGOL_PROFILE,
        )
        log.error("%s", exc)

        log.error(
            "For GitHub Actions, make sure AGOL_USERNAME and "
            "AGOL_PASSWORD are configured as repository secrets."
        )

        raise


# ============================================================
# HTTP REQUEST HELPER
# ============================================================

def request_json(
    url: str,
    params: Dict,
    description: str,
) -> Dict:
    """
    Perform a GET request with retry handling.
    """

    last_exception = None

    for attempt in range(1, MAX_RETRIES + 1):

        try:
            response = requests.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

            response.raise_for_status()

            data = response.json()

            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(
                    f"{description} returned ArcGIS error: "
                    f"{data['error']}"
                )

            return data

        except Exception as exc:

            last_exception = exc

            log.warning(
                "%s failed (attempt %d/%d): %s",
                description,
                attempt,
                MAX_RETRIES,
                exc,
            )

            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)

    raise RuntimeError(
        f"{description} failed after {MAX_RETRIES} attempts: "
        f"{last_exception}"
    )


# ============================================================
# GEOMETRY HELPERS
# ============================================================

def get_point_lon_lat(feature) -> Optional[Tuple[float, float]]:
    """
    Extract longitude/latitude from an ArcGIS Feature.

    Supports both:
        - x/y geometry
        - lon/lat geometry
    """

    geometry = feature.geometry

    if not geometry:
        return None

    try:
        x = geometry.get("x")
        y = geometry.get("y")

        if x is not None and y is not None:
            return float(x), float(y)

        lon = geometry.get("longitude")
        lat = geometry.get("latitude")

        if lon is not None and lat is not None:
            return float(lon), float(lat)

    except Exception:
        pass

    return None


def lonlat_to_web_mercator(
    lon: float,
    lat: float,
) -> Tuple[float, float]:
    """
    Convert WGS84 longitude/latitude to Web Mercator.

    EPSG:4326 -> EPSG:3857
    """

    import math

    # Prevent mathematical problems at the poles.
    lat = max(min(lat, 89.9999), -89.9999)

    x = lon * 20037508.34 / 180.0

    y = (
        math.log(
            math.tan(
                (90.0 + lat) * math.pi / 360.0
            )
        )
        / (math.pi / 180.0)
    )

    y = y * 20037508.34 / 180.0

    return x, y


# ============================================================
# CENSUS STATE LOOKUP
# ============================================================

def lookup_state(
    lon: float,
    lat: float,
) -> Optional[str]:
    """
    Find the state containing the point using Census TIGERweb.
    """

    query_url = f"{CENSUS_STATE_LAYER_URL}/query"

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
        query_url,
        params,
        "Census state lookup",
    )

    features = data.get("features", [])

    if not features:
        return None

    attributes = features[0].get("attributes", {})

    # Prefer the normal state name.
    state_name = attributes.get("NAME")

    if state_name:
        return str(state_name).strip()

    # Fall back to abbreviation.
    abbreviation = attributes.get("STUSAB")

    if abbreviation:
        return str(abbreviation).strip()

    return None


# ============================================================
# BLM LAND STATUS LOOKUP
# ============================================================

def normalize_land_category(
    layer_name: str,
) -> str:
    """
    Convert BLM SMA layer names into a simpler category.
    """

    name = (layer_name or "").strip().upper()

    if "BUREAU OF LAND MANAGEMENT" in name:
        return "BLM"

    if "BLM" in name:
        return "BLM"

    if "NATIONAL PARK SERVICE" in name:
        return "NPS"

    if "NPS" in name:
        return "NPS"

    if "FOREST SERVICE" in name:
        return "USFS"

    if "USFS" in name:
        return "USFS"

    if "FISH AND WILDLIFE" in name:
        return "USFWS"

    if "USFWS" in name:
        return "USFWS"

    if "BUREAU OF RECLAMATION" in name:
        return "BOR"

    if "RECLAMATION" in name:
        return "BOR"

    if "BUREAU OF INDIAN AFFAIRS" in name:
        return "BIA"

    if "INDIAN AFFAIRS" in name:
        return "BIA"

    if "DEPARTMENT OF DEFENSE" in name:
        return "DOD"

    if "DEFENSE" in name:
        return "DOD"

    if "STATE" in name:
        return "State"

    if "LOCAL" in name:
        return "Local"

    if "PRIVATE" in name:
        return "Private"

    if "UNKNOWN" in name:
        return "Other Federal"

    if "FEDERAL" in name:
        return "Other Federal"

    return "Other"


def lookup_blm_land_status(
    lon: float,
    lat: float,
) -> Optional[str]:
    """
    Use the BLM National SMA MapServer Identify operation.

    Important:
        This dataset represents Surface Management Agency (SMA),
        not definitive legal land ownership.
    """

    x, y = lonlat_to_web_mercator(lon, lat)

    half_size = BLM_IDENTIFY_HALF_SIZE_METERS

    map_extent = (
        f"{x - half_size},"
        f"{y - half_size},"
        f"{x + half_size},"
        f"{y + half_size}"
    )

    geometry = (
        f'{{"x":{x},'
        f'"y":{y},'
        f'"spatialReference":{{"wkid":3857}}}}'
    )

    params = {
        "f": "json",
        "geometry": geometry,
        "geometryType": "esriGeometryPoint",
        "sr": 3857,
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
        "BLM SMA Identify",
    )

    results = data.get("results", [])

    if not results:
        return None

    categories = []

    for result in results:

        layer_name = result.get("layerName", "")

        category = normalize_land_category(layer_name)

        if category not in categories:
            categories.append(category)

    if not categories:
        return None

    # Prefer a specific agency if multiple results were returned.
    preferred_order = [
        "BLM",
        "NPS",
        "USFS",
        "USFWS",
        "BOR",
        "BIA",
        "DOD",
        "State",
        "Local",
        "Private",
        "Other Federal",
        "Other",
    ]

    for preferred in preferred_order:
        if preferred in categories:
            return preferred

    return categories[0]


# ============================================================
# QUERY PRODUCTION POINTS
# ============================================================

def query_production_features(layer):
    """
    Retrieve production point features in batches.
    """

    log.info("Querying existing production points...")

    object_id_field = layer.properties.objectIdField

    log.info(
        "ArcGIS object ID field: %s",
        object_id_field,
    )

    all_features = []

    offset = 0

    while True:

        log.info(
            "Querying production points: offset %d",
            offset,
        )

        feature_set = layer.query(
            where="1=1",
            out_fields="*",
            return_geometry=True,
            result_offset=offset,
            result_record_count=QUERY_BATCH_SIZE,
        )

        features = feature_set.features

        if not features:
            break

        all_features.extend(features)

        log.info(
            "Retrieved %d features (total %d)",
            len(features),
            len(all_features),
        )

        if len(features) < QUERY_BATCH_SIZE:
            break

        offset += len(features)

    log.info(
        "Total production points retrieved: %d",
        len(all_features),
    )

    return all_features


# ============================================================
# DETERMINE WHETHER A RECORD NEEDS PROCESSING
# ============================================================

def needs_processing(attributes: Dict) -> bool:
    """
    Determine whether a point needs spatial processing.
    """

    if FORCE_REPROCESS:
        return True

    # --------------------------------------------------------
    # If a processing-status field exists, use it.
    # --------------------------------------------------------

    if PROCESS_FIELD:
        status = attributes.get(PROCESS_FIELD)

        if status is None:
            return True

        status = str(status).strip().upper()

        successful_statuses = {
            "OK",
        }

        if status in successful_statuses:
            return False

        return True

    # --------------------------------------------------------
    # Otherwise use blank State/Land fields.
    # --------------------------------------------------------

    state = attributes.get(STATE_FIELD)
    land = attributes.get(LAND_FIELD)

    state_missing = (
        state is None
        or str(state).strip() == ""
    )

    land_missing = (
        land is None
        or str(land).strip() == ""
    )

    return state_missing or land_missing


# ============================================================
# PROCESS ONE FEATURE
# ============================================================

def process_feature(feature) -> Dict:
    """
    Process one production point and return the attributes
    that should be updated.
    """

    attributes = feature.attributes

    object_id = attributes.get(OBJECTID_FIELD)

    coordinates = get_point_lon_lat(feature)

    if coordinates is None:

        log.warning(
            "OBJECTID %s has no usable geometry.",
            object_id,
        )

        result = {
            OBJECTID_FIELD: object_id,
        }

        if PROCESS_FIELD:
            result[PROCESS_FIELD] = "ERROR"

        return result

    lon, lat = coordinates

    log.info(
        "OBJECTID %s | %.6f, %.6f",
        object_id,
        lon,
        lat,
    )

    result = {
        OBJECTID_FIELD: object_id,
    }

    state = None
    land = None

    # --------------------------------------------------------
    # State
    # --------------------------------------------------------

    try:

        state = lookup_state(
            lon,
            lat,
        )

        if state:
            result[STATE_FIELD] = state

    except Exception as exc:

        log.error(
            "OBJECTID %s state lookup failed: %s",
            object_id,
            exc,
        )

    # --------------------------------------------------------
    # Land status
    # --------------------------------------------------------

    try:

        land = lookup_blm_land_status(
            lon,
            lat,
        )

        if land:
            result[LAND_FIELD] = land

    except Exception as exc:

        log.error(
            "OBJECTID %s land lookup failed: %s",
            object_id,
            exc,
        )

    # --------------------------------------------------------
    # Determine processing status
    # --------------------------------------------------------

    if state and land:
        status = "OK"

    elif state and not land:
        status = "NO_LAND_MATCH"

    elif not state and land:
        status = "NO_STATE"

    else:
        status = "REVIEW"

    if PROCESS_FIELD:
        result[PROCESS_FIELD] = status

    log.info(
        "OBJECTID %s | State=%s | Land=%s | Status=%s",
        object_id,
        state,
        land,
        status,
    )

    return result


# ============================================================
# APPLY UPDATES
# ============================================================

def apply_updates(
    layer,
    updates: List[Dict],
) -> List[Dict]:
    """
    Apply updates to the existing Feature Service.

    Uses explicit arcgis.features.Feature objects because some
    ArcGIS Python API versions reject plain dictionaries passed
    directly to edit_features().
    """

    if not updates:
        log.info("No updates to apply.")

        return []

    results = []

    total = len(updates)

    for start in range(
        0,
        total,
        UPDATE_BATCH_SIZE,
    ):

        batch = updates[
            start:start + UPDATE_BATCH_SIZE
        ]

        batch_number = (
            start // UPDATE_BATCH_SIZE
        ) + 1

        log.info(
            "Updating production layer: %d-%d of %d",
            start + 1,
            min(
                start + len(batch),
                total,
            ),
            total,
        )

        feature_updates = []

        for update in batch:

            attributes = dict(update)

            feature_updates.append(
                Feature(
                    attributes=attributes
                )
            )

        try:

            response = layer.edit_features(
                updates=feature_updates,
                rollback_on_failure=False,
            )

        except Exception as exc:

            log.error(
                "Batch %d failed: %s",
                batch_number,
                exc,
            )

            log.warning(
                "Retrying failed batch one feature at a time..."
            )

            # ------------------------------------------------
            # Individual retry
            # ------------------------------------------------

            for update in batch:

                object_id = update.get(
                    OBJECTID_FIELD
                )

                try:

                    single_feature = Feature(
                        attributes=dict(update)
                    )

                    single_response = (
                        layer.edit_features(
                            updates=[
                                single_feature
                            ],
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
                                "OBJECTID %s update failed: %s",
                                object_id,
                                result,
                            )

                    else:

                        log.error(
                            "OBJECTID %s returned no update result.",
                            object_id,
                        )

                except Exception as single_exc:

                    log.error(
                        "OBJECTID %s individual update failed: %s",
                        object_id,
                        single_exc,
                    )

            continue

        update_results = response.get(
            "updateResults",
            [],
        )

        results.extend(update_results)

        failures = [
            item
            for item in update_results
            if not item.get("success")
        ]

        for failure in failures:

            log.error(
                "Update failed: %s",
                failure,
            )

    return results


# ============================================================
# WRITE CSV SUMMARY
# ============================================================

def write_summary(
    records: List[Dict],
    filename: str,
):
    """
    Write a simple CSV summary of processing results.
    """

    fieldnames = [
        "OBJECTID",
        "longitude",
        "latitude",
        "state",
        "land_status",
        "status",
        "error",
    ]

    with open(
        filename,
        "w",
        newline="",
        encoding="utf-8",
    ) as csv_file:

        writer = csv.DictWriter(
            csv_file,
            fieldnames=fieldnames,
        )

        writer.writeheader()

        for record in records:
            writer.writerow(record)

    log.info(
        "Summary written to %s",
        filename,
    )


# ============================================================
# MAIN
# ============================================================

def main() -> int:

    log.info("=" * 60)
    log.info(
        "Public Land Lovers Spatial Attribute Update"
    )
    log.info("=" * 60)

    log.info(
        "Existing layer: %s",
        POINT_LAYER_URL,
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

    # --------------------------------------------------------
    # Connect
    # --------------------------------------------------------

    gis = connect_to_arcgis()

    # --------------------------------------------------------
    # Access production layer
    # --------------------------------------------------------

    log.info(
        "Opening existing production layer..."
    )

    layer = gis.content.get(
        POINT_LAYER_URL
    ) if False else None

    # FeatureServer URLs can be passed directly to FeatureLayer.
    from arcgis.features import FeatureLayer

    layer = FeatureLayer(
        POINT_LAYER_URL,
        gis=gis,
    )

    log.info(
        "Connected to production Feature Layer."
    )

    log.info(
        "Object ID field: %s",
        layer.properties.objectIdField,
    )

    # --------------------------------------------------------
    # Verify configured fields
    # --------------------------------------------------------

    layer_fields = {
        field["name"]
        for field in layer.properties.fields
    }

    required_fields = {
        OBJECTID_FIELD,
        STATE_FIELD,
        LAND_FIELD,
    }

    missing_fields = (
        required_fields - layer_fields
    )

    if missing_fields:

        log.error(
            "The following required fields are missing "
            "from the production layer: %s",
            ", ".join(sorted(missing_fields)),
        )

        return 1

    if (
        PROCESS_FIELD
        and PROCESS_FIELD not in layer_fields
    ):

        log.warning(
            "Processing field '%s' does not exist.",
            PROCESS_FIELD,
        )

        log.warning(
            "Continuing without the processing-status field."
        )

        # Do not modify the global configuration; simply
        # treat this run as if the field were disabled.
        processing_field_available = False

    else:

        processing_field_available = bool(
            PROCESS_FIELD
        )

    # --------------------------------------------------------
    # Query production points
    # --------------------------------------------------------

    features = query_production_features(
        layer
    )

    if not features:

        log.info(
            "No production points found."
        )

        return 0

    # --------------------------------------------------------
    # Determine records to process
    # --------------------------------------------------------

    features_to_process = []

    for feature in features:

        attributes = feature.attributes

        if (
            PROCESS_FIELD
            and not processing_field_available
        ):

            # Temporarily use blank State/Land logic.
            state = attributes.get(
                STATE_FIELD
            )

            land = attributes.get(
                LAND_FIELD
            )

            state_missing = (
                state is None
                or str(state).strip() == ""
            )

            land_missing = (
                land is None
                or str(land).strip() == ""
            )

            should_process = (
                FORCE_REPROCESS
                or state_missing
                or land_missing
            )

        else:

            should_process = needs_processing(
                attributes
            )

        if should_process:
            features_to_process.append(
                feature
            )

    log.info(
        "Processing %d of %d",
        len(features_to_process),
        len(features),
    )

    if not features_to_process:

        log.info(
            "No points require processing."
        )

        log.info(
            "All existing records are already marked OK."
        )

        return 0

    # --------------------------------------------------------
    # Process points
    # --------------------------------------------------------

    updates = []

    summary_records = []

    for index, feature in enumerate(
        features_to_process,
        start=1,
    ):

        log.info(
            "Processing %d of %d",
            index,
            len(features_to_process),
        )

        attributes = feature.attributes

        coordinates = get_point_lon_lat(
            feature
        )

        result = process_feature(
            feature
        )

        updates.append(result)

        object_id = attributes.get(
            OBJECTID_FIELD
        )

        longitude = None
        latitude = None

        if coordinates:
            longitude, latitude = coordinates

        summary_records.append(
            {
                "OBJECTID": object_id,
                "longitude": longitude,
                "latitude": latitude,
                "state": result.get(
                    STATE_FIELD
                ),
                "land_status": result.get(
                    LAND_FIELD
                ),
                "status": result.get(
                    PROCESS_FIELD
                ) if PROCESS_FIELD
                else "",
                "error": "",
            }
        )

    # --------------------------------------------------------
    # Update production layer
    # --------------------------------------------------------

    log.info("=" * 60)
    log.info(
        "Updating existing production layer..."
    )
    log.info("=" * 60)

    update_results = apply_updates(
        layer,
        updates,
    )

    # --------------------------------------------------------
    # Match update responses back to summary
    # --------------------------------------------------------

    result_by_id = {}

    for result in update_results:

        object_id = result.get(
            "objectId"
        )

        if object_id is not None:
            result_by_id[object_id] = result

    successful = 0
    failed = 0

    for record in summary_records:

        object_id = record["OBJECTID"]

        result = result_by_id.get(
            object_id
        )

        if result:

            if result.get("success"):
                successful += 1
            else:
                failed += 1

                record["error"] = str(
                    result
                )

        else:

            # If there was no response for this
            # OBJECTID, count it as failed.
            failed += 1

            record["error"] = (
                "No update result returned"
            )

    # --------------------------------------------------------
    # Write summary
    # --------------------------------------------------------

    write_summary(
        summary_records,
        SUMMARY_CSV,
    )

    # --------------------------------------------------------
    # Final report
    # --------------------------------------------------------

    log.info("=" * 60)
    log.info(
        "Spatial attribute update complete."
    )
    log.info(
        "Records requiring processing: %d",
        len(features_to_process),
    )
    log.info(
        "Successful updates: %d",
        successful,
    )
    log.info(
        "Failed updates: %d",
        failed,
    )
    log.info(
        "Summary CSV: %s",
        SUMMARY_CSV,
    )
    log.info("=" * 60)

    # --------------------------------------------------------
    # GitHub Actions behavior
    # --------------------------------------------------------
    #
    # If every record failed, return a non-zero exit code.
    # Otherwise allow the workflow to continue.
    #
    if successful == 0 and failed > 0:
        log.error(
            "No production records were successfully updated."
        )

        return 1

    return 0


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    try:
        sys.exit(main())

    except KeyboardInterrupt:

        log.error(
            "Interrupted by user."
        )

        sys.exit(130)

    except Exception as exc:

        log.exception(
            "Fatal error: %s",
            exc,
        )

        sys.exit(1)


