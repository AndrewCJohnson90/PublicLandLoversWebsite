"""
process_new_stops.py

Process new Field Maps staging points into:
    1. Destination points in the real waypoints layer
    2. Routed leg lines in the working/calculation legs layer
    3. Final routed leg lines pushed to the production legs layer

WORKFLOW
--------
Field Maps staging points are sorted by the user-defined
STAGING_ORDER_FIELD (process_seq), lowest first.

process_seq is the authoritative processing order.

    - visit_date does NOT determine processing order.
    - OBJECTID does NOT determine processing order.
    - process_seq must be populated for every unprocessed record.
    - Duplicate process_seq values are rejected to prevent ambiguity.

Each completed leg consists of:

    via -> via -> destination

The route is solved as:

    last real waypoint
        -> via 1
        -> via 2
        -> destination

The order of via points is the order of process_seq.

Coordinate-system handling:
    - Coordinates are read from Feature Services as WGS 84 (4326).
    - Routing inputs are WGS 84.
    - Route output is requested in the native spatial reference
      of the legs layer.
    - Destination points are projected into the native spatial
      reference of the points layer before being added.

IMPORTANT:
    process_seq controls the order in which NEW staging records
    are processed. The existing real waypoint layer is still
    started from the waypoint with the highest existing seq.

    visit_date is retained as the actual date of the destination
    and is written to the real waypoint and leg.

A trailing run of via-points with no destination is left
unprocessed until a destination is added.
"""

import getpass
import json
import os
import requests
from datetime import datetime, timezone
import tempfile
from pathlib import Path

from arcgis.gis import GIS
from arcgis.features import FeatureLayer


# ============================================================
# CONFIGURATION
# ============================================================

AGOL_URL = "https://www.arcgis.com"

USERNAME = "PublicLandLovers"

PROFILE_NAME = "van_life_profile"


POINTS_LAYER_URL = (
    "https://services8.arcgis.com/KzyxLudI6Hn5u85O/"
    "arcgis/rest/services/Janyne_and_Andrew_VanLife/"
    "FeatureServer/0"
)


# Working/calculation legs layer.
# The script continues to use this layer for the internal
# route/calculation workflow.
WORKING_LEGS_LAYER_URL = (
    "https://services8.arcgis.com/KzyxLudI6Hn5u85O/"
    "arcgis/rest/services/van_life_legs/"
    "FeatureServer/0"
)

# Production/published legs layer.
# The finalized route is copied here after the working route
# has been successfully created.
PRODUCTION_LEGS_LAYER_URL = (
    "https://services8.arcgis.com/KzyxLudI6Hn5u85O/"
    "arcgis/rest/services/Janyne_and_Andrew_VanLife/"
    "FeatureServer/1"
)


FIELD_INPUT_LAYER_URL = (
    "https://services8.arcgis.com/KzyxLudI6Hn5u85O/"
    "arcgis/rest/services/van_life_field_input/"
    "FeatureServer/0"
)


# ------------------------------------------------------------
# Fields in the real waypoint layer
# ------------------------------------------------------------

POINTS_FIELDS = {
    "cumulative_miles": "MilesTotal",
    "visit_date": "DateArrived",

    # Field input -> production stop layer
    "shower": "shower",
    "laundry": "laundry",
    "water": "water",
    "nights_in_van": "nights_in_van",
}


# ------------------------------------------------------------
# USER-DEFINED FIELD IN THE STAGING LAYER
#
# This field is now the ONLY field used to determine the
# processing order of unprocessed staging records.
#
# Create this field in the staging layer as a numeric field.
# Example values:
#
#     10
#     20
#     30
#     40
#
# Leaving gaps makes it easy to insert a point later.
# ------------------------------------------------------------

STAGING_ORDER_FIELD = "process_seq"


# ------------------------------------------------------------
# WGS 84
# ------------------------------------------------------------

WGS84_WKID = 4326


# ============================================================
# LOGIN
# ============================================================

def get_gis():
    """
    Connect to ArcGIS Online.

    Priority order:
        1. AGOL_USERNAME / AGOL_PASSWORD environment variables, if both
           are set (this is how GitHub Actions / any non-interactive
           runner logs in — there's no saved profile and nothing to
           type a password into on a fresh CI machine).
        2. A saved local profile (interactive local runs, e.g. from
           your own machine after having logged in once before).
        3. An interactive password prompt as a last resort, also for
           local use.
    """

    env_username = os.environ.get("AGOL_USERNAME")
    env_password = os.environ.get("AGOL_PASSWORD")

    if env_username and env_password:
        gis = GIS(
            AGOL_URL,
            env_username,
            env_password,
        )

        print(
            f"Logged in via environment credentials as "
            f"{gis.users.me.username}"
        )

        return gis

    try:
        gis = GIS(profile=PROFILE_NAME)

        print(
            f"Logged in via cached profile as "
            f"{gis.users.me.username}"
        )

        return gis

    except Exception:
        pass

    password = getpass.getpass(
        f"AGOL password for {USERNAME}: "
    )

    gis = GIS(
        AGOL_URL,
        USERNAME,
        password,
        profile=PROFILE_NAME,
    )

    print(
        f"Logged in and saved profile "
        f"'{PROFILE_NAME}'."
    )

    return gis


# ============================================================
# SPATIAL REFERENCE HELPERS
# ============================================================

def get_layer_spatial_reference(layer, layer_name):
    """
    Return the layer's native spatial reference.

    Uses spatialReference directly instead of extent.
    Some ArcGIS Online layers have extent=None.
    """

    sr = layer.properties.get(
        "spatialReference"
    )

    if not sr:
        raise RuntimeError(
            f"Could not determine spatial reference "
            f"for {layer_name}.\n\n"
            f"Layer properties did not contain "
            f"'spatialReference'."
        )

    wkid = (
        sr.get("latestWkid")
        or sr.get("wkid")
    )

    if not wkid:
        raise RuntimeError(
            f"Could not determine WKID for "
            f"{layer_name}.\n"
            f"Spatial reference returned:\n{sr}"
        )

    return {
        "wkid": wkid,
        "raw": sr,
    }


def print_layer_spatial_references(
    points_layer,
    legs_layer,
    field_input_layer,
):
    """
    Print native spatial references for all layers.
    """

    print("\nSpatial references:")

    for layer, name in [
        (points_layer, "POINTS"),
        (legs_layer, "LEGS"),
        (field_input_layer, "FIELD INPUT"),
    ]:

        sr = get_layer_spatial_reference(
            layer,
            name,
        )

        wkid = sr["wkid"]

        print(
            f"  {name}: WKID {wkid}"
        )

        if wkid in (3857, 102100):
            print(
                "       Web Mercator detected."
            )

        elif wkid == 4326:
            print(
                "       WGS 84 detected."
            )

        else:
            print(
                "       Other coordinate system detected."
            )


# ============================================================
# PROJECT POINT
# ============================================================

def project_point(
    gis,
    x,
    y,
    input_wkid,
    output_wkid,
):
    """
    Project a single point using ArcGIS Online's
    Geometry Service.
    """

    if input_wkid == output_wkid:
        return {
            "x": x,
            "y": y,
            "spatialReference": {
                "wkid": output_wkid
            },
        }

    try:
        geometry_url = (
            gis.properties
            .helperServices
            .geometry.url
        )

    except Exception as exc:
        raise RuntimeError(
            "Could not find the ArcGIS Online "
            "Geometry Service required to project "
            f"coordinates from WKID {input_wkid} "
            f"to WKID {output_wkid}."
        ) from exc

    params = {
        "f": "json",

        "geometries": (
            '{"geometryType":"esriGeometryPoint",'
            '"geometries":['
            f'{{"x":{x},"y":{y}}}'
            "]}"
        ),

        "inSR": input_wkid,

        "outSR": output_wkid,

        "token": gis._con.token,
    }

    response = requests.get(
        f"{geometry_url.rstrip('/')}/project",
        params=params,
        timeout=60,
    )

    response.raise_for_status()

    data = response.json()

    if "error" in data:
        raise RuntimeError(
            "ArcGIS Geometry Service projection "
            f"failed:\n{data['error']}"
        )

    geometries = data.get(
        "geometries",
        [],
    )

    if not geometries:
        raise RuntimeError(
            "Geometry Service returned no "
            "projected geometry."
        )

    projected = geometries[0]

    return {
        "x": projected["x"],
        "y": projected["y"],
        "spatialReference": {
            "wkid": output_wkid
        },
    }


# ============================================================
# COPY FEATURE ATTACHMENTS
# ============================================================

def copy_attachments(source_layer, source_oid, target_layer, target_oid, gis):
    """Copy all Feature Service attachments from staging to production."""
    source_attachments = source_layer.attachments.get_list(oid=source_oid)
    if not source_attachments:
        print(f"  No attachments found for staging OBJECTID {source_oid}.")
        return 0

    print(f"  Found {len(source_attachments)} attachment(s) for staging OBJECTID {source_oid}.")
    copied = 0

    with tempfile.TemporaryDirectory(prefix="quickcapture_attachments_") as temp_dir:
        for attachment in source_attachments:
            attachment_id = attachment.get("id")
            attachment_name = attachment.get("name") or f"attachment_{attachment_id}"
            if attachment_id is None:
                raise RuntimeError(f"Attachment metadata is missing an id: {attachment}")

            url = f"{source_layer.url.rstrip('/')}/{source_oid}/attachments/{attachment_id}"
            response = requests.get(
                url,
                params={"token": gis._con.token, "f": "image"},
                timeout=120,
            )
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "").lower()
            if "json" in content_type or response.text[:20].strip().startswith("{"):
                try:
                    error_data = response.json()
                except Exception:
                    error_data = response.text[:500]
                raise RuntimeError(
                    f"Failed to download attachment {attachment_id} ({attachment_name}) "
                    f"from staging OBJECTID {source_oid}:\n{error_data}"
                )

            safe_name = Path(attachment_name).name
            local_path = Path(temp_dir) / safe_name
            if local_path.exists():
                local_path = Path(temp_dir) / f"{attachment_id}_{safe_name}"
            local_path.write_bytes(response.content)

            add_result = target_layer.attachments.add(target_oid, str(local_path))
            if isinstance(add_result, dict):
                result = add_result.get("addAttachmentResult", add_result)
                success = result.get("success", False)
            else:
                success = bool(add_result)

            if not success:
                raise RuntimeError(
                    f"Failed to add attachment {attachment_id} ({attachment_name}) "
                    f"to production OBJECTID {target_oid}:\n{add_result}"
                )

            copied += 1
            print(f"    Copied attachment {copied}/{len(source_attachments)}: {attachment_name}")

    return copied


# ============================================================
# GET LAST REAL WAYPOINT
# ============================================================

def get_last_point(points_layer):
    """
    Get the last real waypoint.

    The existing waypoint with the highest seq is used as
    the starting point for the newly processed records.

    Geometry is explicitly requested as WGS84.
    """

    result = points_layer.query(
        where="1=1",

        out_fields="*",

        order_by_fields="seq DESC",

        result_record_count=1,

        return_geometry=True,

        out_sr=WGS84_WKID,
    )

    if not result.features:
        raise RuntimeError(
            "Points layer is empty - add at least "
            "one starting point manually first."
        )

    feature = result.features[0]

    attrs = feature.attributes

    geometry = feature.geometry

    return {
        "seq": attrs["seq"],

        "lat": geometry["y"],

        "lon": geometry["x"],

        "cumulative_miles": (
            attrs.get(
                POINTS_FIELDS[
                    "cumulative_miles"
                ]
            )
            or 0.0
        ),

        "name": (
            attrs.get("nameOverride")
            or attrs.get("name")
            or f"Stop {attrs['seq']}"
        ),
    }


# ============================================================
# NEXT SEQUENCE NUMBER
# ============================================================

def next_seq(points_layer):
    """
    Find the next waypoint sequence number.
    """

    result = points_layer.query(
        where="1=1",

        out_fields="seq",

        return_geometry=False,
    )

    if not result.features:
        return 1

    sequences = []

    for feature in result.features:

        value = feature.attributes.get(
            "seq"
        )

        if value is not None:
            sequences.append(value)

    if not sequences:
        return 1

    return max(sequences) + 1


# ============================================================
# VALIDATE COORDINATES
# ============================================================

def validate_coords(
    stop_coords,
    labels,
):
    """
    Validate WGS84 latitude/longitude coordinates.
    """

    problems = []

    for (lat, lon), label in zip(
        stop_coords,
        labels,
    ):

        if lat is None or lon is None:

            problems.append(
                f"  {label}: missing coordinate "
                f"(lat={lat}, lon={lon})"
            )

        elif not (-90 <= lat <= 90):

            problems.append(
                f"  {label}: lat={lat} is out of "
                f"range - did lat/lon get swapped?"
            )

        elif not (-180 <= lon <= 180):

            problems.append(
                f"  {label}: lon={lon} is out of "
                f"range - did lat/lon get swapped?"
            )

        elif lat == 0 and lon == 0:

            problems.append(
                f"  {label}: coordinates are "
                f"exactly (0, 0) - likely failed."
            )

    if problems:

        raise RuntimeError(
            "Bad WGS84 coordinate(s) found "
            "before calling the route service:\n"
            + "\n".join(problems)
        )


# ============================================================
# ROUTE SOLVER
# ============================================================

def solve_multistop(
    gis,
    stop_coords,
    output_wkid,
    labels=None,
):
    """
    Solve a multi-stop route.

    Input:
        WGS84 latitude/longitude.

    Output:
        Route geometry in output_wkid.

    Stops are sent to the routing service in the exact order
    supplied to this function.
    """

    if labels is None:

        labels = [
            f"point {i + 1}"
            for i in range(
                len(stop_coords)
            )
        ]

    validate_coords(
        stop_coords,
        labels,
    )

    # --------------------------------------------------------
    # Routing service
    # --------------------------------------------------------

    try:

        route_url = (
            gis.properties
            .helperServices
            .route.url
        )

    except AttributeError:

        raise RuntimeError(
            "No routing service configured "
            "on this ArcGIS Online organization."
        )

    solve_url = route_url.rstrip("/")

    if not solve_url.endswith("solve"):

        solve_url += "/solve"

    # --------------------------------------------------------
    # Stops
    #
    # ArcGIS expects:
    #
    # longitude,latitude
    # --------------------------------------------------------

    stops = ";".join(
        f"{lon},{lat}"
        for lat, lon in stop_coords
    )

    params = {
        "f": "json",

        "stops": stops,

        "inSR": WGS84_WKID,

        "outSR": output_wkid,

        "returnRoutes": True,

        "returnDirections": False,

        "outputLines": (
            "esriNAOutputLineTrueShape"
        ),

        "token": gis._con.token,
    }

    print(
        f"  Routing WGS84 -> WKID {output_wkid}"
    )

    response = requests.get(
        solve_url,
        params=params,
        timeout=120,
    )

    response.raise_for_status()

    data = response.json()

    if "error" in data:

        coord_dump = "\n".join(
            f"  {label}: ({lat}, {lon})"
            for (lat, lon), label
            in zip(
                stop_coords,
                labels,
            )
        )

        raise RuntimeError(
            "Route service error:\n"
            f"{data['error']}\n\n"
            "Stops sent for this leg:\n"
            f"{coord_dump}"
        )

    # --------------------------------------------------------
    # Route result
    # --------------------------------------------------------

    features = (
        data
        .get("routes", {})
        .get("features", [])
    )

    if not features:

        raise RuntimeError(
            "Route service returned no route."
        )

    route_feature = features[0]

    attrs = route_feature.get(
        "attributes",
        {},
    )

    # --------------------------------------------------------
    # Distance
    # --------------------------------------------------------

    miles = attrs.get(
        "Total_Miles"
    )

    if miles is None:

        km = attrs.get(
            "Total_Kilometers"
        )

        if km is not None:

            miles = (
                km * 0.621371
            )

    if miles is None:

        raise RuntimeError(
            "Could not find distance in "
            f"route result:\n{attrs}"
        )

    # --------------------------------------------------------
    # Geometry
    # --------------------------------------------------------

    geometry = route_feature.get(
        "geometry"
    )

    if not geometry:

        raise RuntimeError(
            "Route result contained no geometry."
        )

    geometry["spatialReference"] = {
        "wkid": output_wkid
    }

    return miles, geometry


# ============================================================
# FETCH UNPROCESSED STAGING POINTS
# ============================================================

def fetch_unprocessed(field_input_layer):
    """
    Fetch all unprocessed staging points.

    PROCESS ORDER:
        process_seq ASC

    process_seq is the sole authority for ordering.

    visit_date is NOT used for sorting.
    OBJECTID is NOT used as a tie-breaker.

    Every unprocessed record must have a numeric process_seq.
    Duplicate process_seq values are rejected.
    """

    result = field_input_layer.query(
        where=(
            "processed = 0 "
            "OR processed IS NULL"
        ),

        out_fields="*",

        order_by_fields=(
            f"{STAGING_ORDER_FIELD} ASC"
        ),

        return_geometry=True,

        out_sr=WGS84_WKID,
    )

    features = result.features

    # --------------------------------------------------------
    # Validate process_seq before doing anything.
    # --------------------------------------------------------

    missing = []
    values = []
    invalid = []

    for feature in features:

        attrs = feature.attributes

        object_id = attrs.get("OBJECTID")

        value = attrs.get(
            STAGING_ORDER_FIELD
        )

        if value is None:
            missing.append(object_id)
            continue

        try:
            numeric_value = float(value)

            if numeric_value != numeric_value:
                raise ValueError

            values.append(
                (numeric_value, object_id)
            )

        except (TypeError, ValueError):
            invalid.append(
                (object_id, value)
            )

    if missing:

        raise RuntimeError(
            f"Unprocessed staging record(s) are missing "
            f"'{STAGING_ORDER_FIELD}': "
            f"{missing}\n\n"
            "Populate process_seq for every unprocessed "
            "record before running the script."
        )

    if invalid:

        details = "\n".join(
            f"  OBJECTID={oid}: "
            f"{value!r}"
            for oid, value in invalid
        )

        raise RuntimeError(
            f"Invalid values found in "
            f"'{STAGING_ORDER_FIELD}':\n"
            f"{details}\n\n"
            "The field must contain numeric values."
        )

    # --------------------------------------------------------
    # Reject duplicate process_seq values.
    # --------------------------------------------------------

    seen = {}

    for value, object_id in values:

        seen.setdefault(
            value,
            []
        ).append(object_id)

    duplicates = {
        value: object_ids
        for value, object_ids in seen.items()
        if len(object_ids) > 1
    }

    if duplicates:

        details = "\n".join(
            f"  process_seq={value}: "
            f"OBJECTIDs={object_ids}"
            for value, object_ids
            in sorted(duplicates.items())
        )

        raise RuntimeError(
            "Duplicate process_seq values found:\n"
            f"{details}\n\n"
            "Each unprocessed staging point must have a "
            "unique process_seq so the route order is "
            "unambiguous."
        )

    # --------------------------------------------------------
    # Print the actual order being used.
    # --------------------------------------------------------

    print(
        f"\nStaging point order "
        f"(controlled by {STAGING_ORDER_FIELD}):"
    )

    for index, feature in enumerate(
        features,
        start=1,
    ):

        attrs = feature.attributes

        print(
            f"  {index}. "
            f"process_seq="
            f"{attrs.get(STAGING_ORDER_FIELD)} "
            f"OBJECTID={attrs.get('OBJECTID')} "
            f"role={attrs.get('role')} "
            f"visit_date={attrs.get('visit_date')}"
        )

    return features


# ============================================================
# GROUP INTO LEGS
# ============================================================

def group_into_legs(features):
    """
    Groups staging points in process_seq order into:

        via
        via
        destination

    A destination closes the current leg.

    A destination with no preceding via points is valid.

    A trailing run of via-points with no destination
    is returned separately and remains unprocessed.
    """

    legs = []

    current_vias = []

    for feature in features:

        role = (
            feature.attributes.get(
                "role"
            )
            or ""
        ).lower()

        if role == "via":

            current_vias.append(
                feature
            )

        elif role == "destination":

            legs.append(
                {
                    "vias": current_vias,
                    "destination": feature,
                }
            )

            current_vias = []

        else:

            print(
                "  Skipping point with "
                f"unrecognized role={role!r} "
                f"(OBJECTID "
                f"{feature.attributes.get('OBJECTID')})"
            )

    leftover_vias = current_vias

    return legs, leftover_vias


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 70)

    print(
        "PROCESS NEW STOPS"
    )

    print("=" * 70)

    # --------------------------------------------------------
    # Connect
    # --------------------------------------------------------

    gis = get_gis()

    # --------------------------------------------------------
    # Feature layers
    # --------------------------------------------------------

    points_layer = FeatureLayer(
        POINTS_LAYER_URL,
        gis=gis,
    )

    working_legs_layer = FeatureLayer(
        WORKING_LEGS_LAYER_URL,
        gis=gis,
    )

    production_legs_layer = FeatureLayer(
        PRODUCTION_LEGS_LAYER_URL,
        gis=gis,
    )

    field_input_layer = FeatureLayer(
        FIELD_INPUT_LAYER_URL,
        gis=gis,
    )

    print("\nAttachment support:")
    print(f"  FIELD INPUT hasAttachments: {field_input_layer.properties.get('hasAttachments')}")
    print(f"  POINTS hasAttachments: {points_layer.properties.get('hasAttachments')}")

    # --------------------------------------------------------
    # Spatial references
    # --------------------------------------------------------

    points_sr = get_layer_spatial_reference(
        points_layer,
        "POINTS",
    )

    working_legs_sr = get_layer_spatial_reference(
        working_legs_layer,
        "WORKING LEGS",
    )

    production_legs_sr = get_layer_spatial_reference(
        production_legs_layer,
        "PRODUCTION LEGS",
    )

    field_input_sr = get_layer_spatial_reference(
        field_input_layer,
        "FIELD INPUT",
    )

    points_wkid = points_sr["wkid"]
    working_legs_wkid = working_legs_sr["wkid"]
    production_legs_wkid = production_legs_sr["wkid"]

    print_layer_spatial_references(
        points_layer,
        working_legs_layer,
        field_input_layer,
    )

    print(
        f"\nInternal routing coordinate system: "
        f"WGS84 ({WGS84_WKID})"
    )

    print(
        f"Destination points will be written "
        f"using WKID {points_wkid}."
    )

    print(
        f"Working leg lines will be written "
        f"using WKID {working_legs_wkid}."
    )

    print(
        f"Production leg lines will be written "
        f"using WKID {production_legs_wkid}."
    )

    # --------------------------------------------------------
    # Fetch staging points
    # --------------------------------------------------------

    staged = fetch_unprocessed(
        field_input_layer
    )

    print(
        f"\nFound {len(staged)} "
        "unprocessed staged point(s)."
    )

    if not staged:

        print(
            "Nothing to process."
        )

        return

    # --------------------------------------------------------
    # Group into legs
    # --------------------------------------------------------

    legs, leftover_vias = group_into_legs(
        staged
    )

    print(
        f"Grouped into {len(legs)} "
        "complete leg(s)."
    )

    if leftover_vias:

        print(
            f"  {len(leftover_vias)} via-point(s) "
            "waiting on a destination - "
            "left unprocessed."
        )

    # --------------------------------------------------------
    # Get last real waypoint
    # --------------------------------------------------------

    last = get_last_point(
        points_layer
    )

    print(
        f"\nStarting from: "
        f"{last['name']} "
        f"(seq {last['seq']})"
    )

    print(
        f"  Coordinates: "
        f"{last['lat']}, {last['lon']}"
    )

    print(
        f"  Cumulative miles: "
        f"{last['cumulative_miles']}"
    )

    # --------------------------------------------------------
    # Track the next seq number locally instead of re-querying
    # the server every leg.
    #
    # AGOL hosted layers can have a brief read-after-write
    # delay, so a fresh query immediately after an add can
    # still return the OLD max. Incrementing locally avoids
    # duplicate seq values during the run.
    # --------------------------------------------------------

    seq_counter = next_seq(points_layer)

    print(
        f"\nStarting seq counter at: {seq_counter}"
    )

    # ========================================================
    # PROCESS EACH LEG
    # ========================================================

    for leg_number, leg in enumerate(
        legs,
        start=1,
    ):

        dest = leg["destination"]

        dest_attrs = dest.attributes

        # ----------------------------------------------------
        # Destination name
        # ----------------------------------------------------

        dest_name = (
            dest_attrs.get("name")
            or f"Stop (OBJECTID "
               f"{dest_attrs.get('OBJECTID')})"
        )

        # ----------------------------------------------------
        # Via coordinates
        #
        # fetch_unprocessed() explicitly requests WGS84.
        #
        # Therefore:
        #     geometry["x"] = longitude
        #     geometry["y"] = latitude
        #
        # Via points are already in process_seq order.
        # ----------------------------------------------------

        via_coords = []

        for via in leg["vias"]:

            geometry = via.geometry

            via_lon = geometry["x"]

            via_lat = geometry["y"]

            via_coords.append(
                (
                    via_lat,
                    via_lon,
                )
            )

        # ----------------------------------------------------
        # Destination coordinates
        # ----------------------------------------------------

        dest_lat = dest.geometry["y"]

        dest_lon = dest.geometry["x"]

        # ----------------------------------------------------
        # Complete ordered stop list
        #
        # IMPORTANT:
        # This order is now controlled by process_seq.
        # ----------------------------------------------------

        stop_coords = (
            [
                (
                    last["lat"],
                    last["lon"],
                )
            ]

            + via_coords

            + [
                (
                    dest_lat,
                    dest_lon,
                )
            ]
        )

        stop_labels = (
            [last["name"]]

            + [
                (
                    f"via-point {i + 1} "
                    f"(process_seq="
                    f"{v.attributes.get(STAGING_ORDER_FIELD)}, "
                    f"OBJECTID="
                    f"{v.attributes.get('OBJECTID')})"
                )

                for i, v in enumerate(
                    leg["vias"]
                )
            ]

            + [dest_name]
        )

        # ----------------------------------------------------
        # Print leg information
        # ----------------------------------------------------

        print(
            "\n" + "-" * 70
        )

        print(
            f"Leg {leg_number}: "
            f"{last['name']} -> {dest_name}"
        )

        print(
            f"  Destination process_seq: "
            f"{dest_attrs.get(STAGING_ORDER_FIELD)}"
        )

        print(
            f"  Visit date: "
            f"{dest_attrs.get('visit_date')}"
        )

        print(
            f"  {len(via_coords)} via-point(s)"
        )

        print(
            f"  Destination WGS84: "
            f"{dest_lat}, {dest_lon}"
        )

        # ----------------------------------------------------
        # Solve route
        # ----------------------------------------------------

        leg_miles, route_geom = solve_multistop(
            gis=gis,

            stop_coords=stop_coords,

            output_wkid=working_legs_wkid,

            labels=stop_labels,
        )

        leg_miles = round(
            leg_miles,
            2,
        )

        new_cumulative = round(
            last["cumulative_miles"]
            + leg_miles,
            2,
        )

        new_seq = seq_counter

        # ----------------------------------------------------
        # Visit date
        # ----------------------------------------------------

        date_epoch_ms = dest_attrs.get(
            "visit_date"
        )

        if date_epoch_ms is None:

            date_epoch_ms = int(
                datetime.now(
                    timezone.utc
                ).timestamp()
                * 1000
            )

            print(
                f"  WARNING: '{dest_name}' "
                "has no visit_date. "
                "Using current date/time."
            )

        # ====================================================
        # PROJECT DESTINATION TO POINTS LAYER
        # ====================================================

        projected_point = project_point(
            gis=gis,

            x=dest_lon,

            y=dest_lat,

            input_wkid=WGS84_WKID,

            output_wkid=points_wkid,
        )

        print(
            f"  Destination projected: "
            f"WGS84 -> WKID {points_wkid}"
        )

        # ====================================================
        # ADD DESTINATION POINT
        # ====================================================

        point_feature = {
            "geometry": projected_point,

            "attributes": {
                "seq": new_seq,

                "name": dest_name,

                "nameOverride": dest_name,

                "description": (
                    dest_attrs.get(
                        "description"
                    )
                    or ""
                ),

                "category": (
                    dest_attrs.get(
                        "category"
                    )
                    or ""
                ),

                "type": (
                    dest_attrs.get(
                        "type"
                    )
                    or ""
                ),

                POINTS_FIELDS[
                    "cumulative_miles"
                ]: new_cumulative,

                POINTS_FIELDS[
                    "visit_date"
                ]: date_epoch_ms,

                "Miles_from_previous": leg_miles,

                # Field input -> production stop layer
                POINTS_FIELDS["shower"]: dest_attrs.get("shower"),
                POINTS_FIELDS["laundry"]: dest_attrs.get("laundry"),
                POINTS_FIELDS["water"]: dest_attrs.get("water"),
                POINTS_FIELDS["nights_in_van"]: dest_attrs.get(
                    "nights_in_van"
                ),
            },
        }

        add_result = points_layer.edit_features(
            adds=[
                point_feature
            ]
        )

        add_results = add_result.get(
            "addResults",
            [],
        )

        if (
            not add_results
            or not add_results[0].get(
                "success"
            )
        ):

            raise RuntimeError(
                "Failed to add destination point:\n"
                f"{add_result}"
            )

        print(
            f"  Added '{dest_name}' "
            f"as seq {new_seq}"
        )

        # QuickCapture/Field Maps photos and files are Feature Service
        # attachments, so they must be copied separately from attributes.
        production_oid = add_results[0].get("objectId")
        if production_oid is None:
            raise RuntimeError(
                f"Destination '{dest_name}' was added, but ArcGIS did not "
                "return its production OBJECTID; attachments cannot be copied safely."
            )

        copied_attachment_count = copy_attachments(
            source_layer=field_input_layer,
            source_oid=dest_attrs["OBJECTID"],
            target_layer=points_layer,
            target_oid=production_oid,
            gis=gis,
        )

        print(
            f"  Copied {copied_attachment_count} attachment(s) "
            f"to production OBJECTID {production_oid}."
        )

        seq_counter += 1

        print(
            f"  Leg distance: "
            f"{leg_miles} miles"
        )

        print(
            f"  Cumulative distance: "
            f"{new_cumulative} miles"
        )

        # ====================================================
        # ADD ROUTE LINE
        # ====================================================

        line_feature = {
            "geometry": route_geom,

            "attributes": {
                "from_seq": last["seq"],

                "to_seq": new_seq,

                "start_location": last["name"],

                "end_location": dest_name,

                "leg_miles": leg_miles,

                "visit_date": date_epoch_ms,

                "notes": (
                    dest_attrs.get(
                        "notes"
                    )
                    or ""
                ),
            },
        }

        # ========================================================
        # ADD ROUTE LINE TO WORKING/CALCULATION LAYER
        # ========================================================

        line_result = working_legs_layer.edit_features(
            adds=[
                line_feature
            ]
        )

        line_results = line_result.get(
            "addResults",
            [],
        )

        if (
            not line_results
            or not line_results[0].get(
                "success"
            )
        ):

            raise RuntimeError(
                "Failed to add leg line to the "
                "working/calculation layer:\n"
                f"{line_result}"
            )

        print(
            f"  Added route line to working layer "
            f"({len(via_coords)} via-point(s) included)."
        )

        # ========================================================
        # PUSH FINAL ROUTE TO PRODUCTION LEGS LAYER
        # ========================================================
        #
        # The route was solved in the working layer's native
        # spatial reference. If the production layer uses a
        # different WKID, project the completed polyline before
        # adding it to the production layer.
        # ========================================================

        production_geometry = route_geom

        if working_legs_wkid != production_legs_wkid:

            try:
                geometry_url = (
                    gis.properties
                    .helperServices
                    .geometry.url
                )
            except Exception as exc:
                raise RuntimeError(
                    "Could not find the ArcGIS Online Geometry "
                    "Service required to project the final route "
                    f"from WKID {working_legs_wkid} to "
                    f"WKID {production_legs_wkid}."
                ) from exc

            projection_params = {
                "f": "json",
                "geometries": json.dumps({
                    "geometryType": "esriGeometryPolyline",
                    "geometries": [route_geom],
                }),
                "inSR": working_legs_wkid,
                "outSR": production_legs_wkid,
                "token": gis._con.token,
            }

            projection_response = requests.get(
                f"{geometry_url.rstrip('/')}/project",
                params=projection_params,
                timeout=60,
            )

            projection_response.raise_for_status()

            projection_data = projection_response.json()

            if "error" in projection_data:
                raise RuntimeError(
                    "ArcGIS Geometry Service projection of the "
                    "production route failed:\n"
                    f"{projection_data['error']}"
                )

            projected_geometries = projection_data.get(
                "geometries",
                [],
            )

            if not projected_geometries:
                raise RuntimeError(
                    "Geometry Service returned no projected "
                    "production route geometry."
                )

            production_geometry = projected_geometries[0]

            production_geometry["spatialReference"] = {
                "wkid": production_legs_wkid
            }

            print(
                f"  Projected final route: "
                f"WKID {working_legs_wkid} -> "
                f"WKID {production_legs_wkid}"
            )

        else:
            production_geometry["spatialReference"] = {
                "wkid": production_legs_wkid
            }

        production_line_feature = {
            "geometry": production_geometry,
            "attributes": dict(
                line_feature["attributes"]
            ),
        }

        production_line_result = (
            production_legs_layer.edit_features(
                adds=[
                    production_line_feature
                ]
            )
        )

        production_line_results = (
            production_line_result.get(
                "addResults",
                [],
            )
        )

        if (
            not production_line_results
            or not production_line_results[0].get(
                "success"
            )
        ):
            raise RuntimeError(
                "Working route was created, but the final "
                "route could not be added to the production "
                "legs layer. Staging records were NOT marked "
                "processed so this can be retried.\n"
                f"{production_line_result}"
            )

        print(
            f"  Pushed final route to production layer "
            f"({len(via_coords)} via-point(s) included)."
        )

        # ====================================================
        # MARK STAGING POINTS PROCESSED
        # ====================================================

        ids_to_mark = (
            [
                v.attributes["OBJECTID"]
                for v in leg["vias"]
            ]

            + [
                dest_attrs["OBJECTID"]
            ]
        )

        updates = [
            {
                "attributes": {
                    "OBJECTID": oid,

                    "processed": 1,
                }
            }

            for oid in ids_to_mark
        ]

        update_result = (
            field_input_layer.edit_features(
                updates=updates
            )
        )

        update_results = update_result.get(
            "updateResults",
            [],
        )

        failed_updates = [
            r
            for r in update_results
            if not r.get("success")
        ]

        if failed_updates:

            raise RuntimeError(
                "Some staging points could not "
                "be marked as processed:\n"
                f"{failed_updates}"
            )

        print(
            f"  Marked {len(ids_to_mark)} "
            "staged point(s) as processed."
        )

        # ----------------------------------------------------
        # Update last waypoint
        #
        # Keep internal coordinates in WGS84.
        # ----------------------------------------------------

        last = {
            "seq": new_seq,

            "lat": dest_lat,

            "lon": dest_lon,

            "cumulative_miles": (
                new_cumulative
            ),

            "name": dest_name,
        }

    # ========================================================
    # COMPLETE
    # ========================================================

    print(
        "\n" + "=" * 70
    )

    print(
        f"Done. Processed {len(legs)} leg(s)."
    )

    print(
        "=" * 70
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()
