"""
DEP-71 — Sprinkenhof: compare current building heating against Hamburg's
official "Zielszenario" (target scenario) district-heating map.

Data source: Hamburg WFS "Zielszenario — kommunale Wärmeplanung Hamburg"
(https://geodienste.hamburg.de/wfs_zielszenario_kwp). This is the layer Sebastian
pointed to directly via the portal's own deep-link (geoportal-hamburg.de/waermeportal-
hamburg/?...LAYERS=[...{"id":"35894","visibility":true}...]) — resolved via Hamburg's
services-internet.json config (https://geoportal-hamburg.de/lgv-config/services-internet.json)
to WMS `wms_zielszenario_kwp` (layer `zielszenario`), and its WFS sibling found by the
same wms_/wfs_ naming pattern used throughout this project.

SUPERSEDES an earlier version of this script that used
"Eignung Wärmenetzversorgung" (wfs_eignung_waermenetzversorgung_kwp) — that layer
is only a *suitability score* (0-3), one of several inputs. This layer,
`zielszenario`, is Hamburg's actual final per-area recommendation (`empfehlung`),
and — critically — is the ONLY layer found so far that carries the year/timing
dimension from Sebastian's original DEP-71 comment ("Fernwärme available at a
different point in time"): the `empfehlung` values include explicit rollout years
("Wärmenetzausbau ab 2026/2030/2035").

`empfehlung` values observed (26,150 Gebietseinheiten, 2026-09-02 fetch):
  Dezentrale Wärmeversorgung                          17348  — decentral recommended
  Vorhandenes Wärmenetz und Wärmenetzverdichtung        5908  — existing network / densification
  Wärmenetzausbau ab 2035                                944  — network expansion from 2035
  Wärmenetzausbau ab 2026                                804  — network expansion from 2026
  Wärmenetzausbau, Eigeninitiative notwendig             503  — network expansion, owner-initiative required
  Wärmenetzausbau ab 2030                                465  — network expansion from 2030
  Für dezentral und Wärmenetz gleichwertig geeignet      178  — equally suited for either
  (10 features have no empfehlung)

Also carries `betreiber` (current network operator) and `betreiber_vorschlag`
(proposed operator for planned expansion) — not in the old layer at all.

Method mirrors deployment_tools/bwv-analysis/fernwaerme/ (Berlin Fernwärme pipeline):
fetch WFS polygons -> point-in-polygon spatial join against building lat/long ->
tag each building -> compare against its current heating source. Spatial-join /
snap-to-nearest-250m logic is unchanged and reused as-is — both WFS layers share
the same Gebietseinheit polygon grid (confirmed: e.g. BG04590 appears in both).

Run:
  conda run -n bwv-analysis python script/analyze_waermeplanung.py
(from the SprinkenhofAnalyzenewdistrictheatingavailabilitymap/ directory)
"""

import logging
import time
from io import BytesIO
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import Point

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IMPORT_DIR = PROJECT_ROOT / "import"
EXPORT_DIR = PROJECT_ROOT / "export"

BUILDINGS_CSV = IMPORT_DIR / "SprinkenhofLangLong.csv"
WFS_CACHE_PATH = IMPORT_DIR / "hh_zielszenario_wfs_cache.geojson"
OUTPUT_XLSX = EXPORT_DIR / "DEP-71_Sprinkenhof_Waermeplanung_Zielszenario.xlsx"

WFS_ENDPOINT = "https://geodienste.hamburg.de/wfs_zielszenario_kwp"
WFS_TYPENAME = "de.hh.up:zielszenario"
WFS_SRS = "EPSG:25832"  # native CRS of the WFS; buildings CSV is EPSG:4326

MAX_RETRIES = 4
BACKOFF_SECONDS = 2.0
PAGE_SIZE = 20000

DISTRICT_HEATING_LABEL = "District heating"

FIELD_COLS = [
    "gebietseinheit", "empfehlung", "betreiber", "betreiber_vorschlag",
    "kommentar", "link_building_website", "farbcode",
]

NO_MATCH_CATEGORY = "No map match (outside suitability layer)"
OUTSIDE_COVERAGE_CATEGORY = "Outside Hamburg coverage"

# Hamburg's `empfehlung` values, translated to English. This IS the category —
# no thresholding/bucketing needed, Hamburg's own recommendation is already
# the right granularity (and includes the rollout year where relevant).
EMPFEHLUNG_TRANSLATION = {
    "Dezentrale Wärmeversorgung": "Decentral heat supply",
    "Vorhandenes Wärmenetz und Wärmenetzverdichtung": "Existing network / densification",
    "Wärmenetzausbau ab 2026": "Network expansion from 2026",
    "Wärmenetzausbau ab 2030": "Network expansion from 2030",
    "Wärmenetzausbau ab 2035": "Network expansion from 2035",
    "Wärmenetzausbau, Eigeninitiative notwendig": "Network expansion (owner-initiative required)",
    "Für dezentral und Wärmenetz gleichwertig geeignet": "Equally suited for decentral or network",
}
DECENTRAL_CATEGORY = "Decentral heat supply"
EQUAL_CATEGORY = "Equally suited for decentral or network"
EIGENINITIATIVE_CATEGORY = "Network expansion (owner-initiative required)"
EXISTING_NETWORK_CATEGORY = "Existing network / densification"

# Direct answer to "when will this building get district heating" — one clean
# value per category, independent of the longer impact/category text.
CATEGORY_TO_AVAILABILITY = {
    # Reached only when the building itself is NOT currently on district
    # heating (the is_dh branch above already returns "Already available"
    # for buildings that genuinely have it) — so this must describe the
    # opportunity, not imply the building already has it.
    EXISTING_NETWORK_CATEGORY: "Available now — not yet connected",
    "Network expansion from 2026": "2026",
    "Network expansion from 2030": "2030",
    "Network expansion from 2035": "2035",
    EIGENINITIATIVE_CATEGORY: "No fixed date — requires owner request",
    DECENTRAL_CATEGORY: "Not planned (decentral recommended)",
    EQUAL_CATEGORY: "Uncertain — equally suited either way",
    OUTSIDE_COVERAGE_CATEGORY: "Not applicable",
    NO_MATCH_CATEGORY: "Not applicable",
}

# The Gebiete polygons are built from building footprints with non-built land
# (streets, courtyards, green strips) cut out, so a geocoded building point can
# legitimately fall a few metres to ~250m outside its own Gebiet. Snap those to
# the nearest polygon. Anything farther is treated as genuinely outside coverage
# (e.g. Neuwerk island, or buildings outside the Hamburg city state like Geesthacht).
SNAP_TOLERANCE_M = 250


def _write_cache(gdf: gpd.GeoDataFrame, cache_path: Path) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        gdf.to_file(cache_path, driver="GeoJSON")
    except Exception as exc:  # caching is best-effort
        log.warning("Could not write WFS cache to %s: %s", cache_path, exc)


def fetch_zielszenario(cache_path: Path = WFS_CACHE_PATH) -> gpd.GeoDataFrame:
    """
    Download all "Zielszenario" polygons from Hamburg's WFS, paginating past the
    server's per-request cap. Retries on transient network errors; falls back to
    the disk cache if the server is unreachable.

    Returns a GeoDataFrame in EPSG:4326.
    """
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            frames = []
            start = 0
            while True:
                params = {
                    "service": "WFS",
                    "version": "2.0.0",
                    "request": "GetFeature",
                    "typeNames": WFS_TYPENAME,
                    "count": PAGE_SIZE,
                    "startIndex": start,
                }
                r = requests.get(WFS_ENDPOINT, params=params, timeout=120)
                r.raise_for_status()
                page = gpd.read_file(BytesIO(r.content))
                log.info("Fetched %d features starting at %d", len(page), start)
                if len(page) == 0:
                    break
                frames.append(page)
                start += len(page)
                if len(page) < PAGE_SIZE:
                    break

            gdf = pd.concat(frames, ignore_index=True)
            gdf = gpd.GeoDataFrame(gdf, geometry="geometry")
            gdf.crs = WFS_SRS
            gdf = gdf.to_crs(4326)
            _write_cache(gdf, cache_path)
            return gdf
        except (requests.exceptions.RequestException, OSError) as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                wait = BACKOFF_SECONDS * (2 ** (attempt - 1))
                log.warning(
                    "WFS fetch attempt %d/%d failed (%s); retrying in %.0fs...",
                    attempt, MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)

    if cache_path.exists():
        log.warning("WFS unreachable after %d attempts; using cached data. Last error: %s",
                     MAX_RETRIES, last_exc)
        return gpd.read_file(cache_path).to_crs(4326)

    raise RuntimeError(
        f"Failed to fetch Zielszenario polygons after {MAX_RETRIES} attempts, "
        f"and no local cache exists at {cache_path}. Last error: {last_exc}"
    ) from last_exc


def load_buildings(csv_path: Path = BUILDINGS_CSV) -> pd.DataFrame:
    return pd.read_csv(csv_path)


def buildings_within_zielszenario(
    df: pd.DataFrame, gdf_polygons: gpd.GeoDataFrame
) -> pd.DataFrame:
    """
    Point-in-polygon spatial join: tag each building with the Zielszenario fields
    of the Gebietseinheit it falls inside. Buildings that miss every polygon
    (footprint gaps — streets/courtyards were cut out of the Gebiete) are then
    snapped to the nearest polygon within SNAP_TOLERANCE_M; anything farther is
    left unmatched and flagged as outside Hamburg's Wärmeportal coverage.
    """
    df = df.dropna(subset=["latitude", "longitude"]).copy()

    gdf_buildings = gpd.GeoDataFrame(
        df,
        geometry=[Point(lon, lat) for lon, lat in zip(df["longitude"], df["latitude"])],
        crs="EPSG:4326",
    )

    gdf_poly = gdf_polygons[FIELD_COLS + ["geometry"]].copy()

    joined = gpd.sjoin(gdf_buildings, gdf_poly, how="left", predicate="within")
    joined = joined[~joined.index.duplicated(keep="first")]
    for col in FIELD_COLS:
        df[col] = joined[col]
    df["match_method"] = df["gebietseinheit"].notna().map({True: "within", False: None})

    missing_mask = df["gebietseinheit"].isna()
    n_missing = int(missing_mask.sum())
    if n_missing:
        log.info("%d buildings had no direct polygon match; snapping to nearest within %dm",
                  n_missing, SNAP_TOLERANCE_M)
        gdf_missing = gpd.GeoDataFrame(
            df.loc[missing_mask, []],
            geometry=gdf_buildings.loc[missing_mask, "geometry"],
            crs="EPSG:4326",
        ).to_crs(25832)
        gdf_poly_m = gdf_poly.to_crs(25832)

        nearest = gpd.sjoin_nearest(
            gdf_missing, gdf_poly_m[FIELD_COLS + ["geometry"]],
            max_distance=SNAP_TOLERANCE_M, distance_col="snap_distance_m",
        )
        nearest = nearest[~nearest.index.duplicated(keep="first")]

        for col in FIELD_COLS:
            df.loc[nearest.index, col] = nearest[col]
        df.loc[nearest.index, "match_method"] = (
            "snapped_" + nearest["snap_distance_m"].round(0).astype(int).astype(str) + "m"
        )

    still_missing = df["gebietseinheit"].isna()
    df.loc[still_missing, "match_method"] = "outside_coverage"

    return df


def classify(df: pd.DataFrame) -> pd.DataFrame:
    """Add `category`, `impact`, and `proposed_action` columns per Sebastian's ask (DEP-71)."""
    df = df.copy()
    df["empfehlung"] = df["empfehlung"].replace(EMPFEHLUNG_TRANSLATION)

    is_dh = df["heating_type"].astype(str).str.strip().str.casefold() == DISTRICT_HEATING_LABEL.casefold()
    outside = df["match_method"] == "outside_coverage"
    no_data = df["empfehlung"].isna() & ~outside

    def _category(row_outside, row_no_data, row_empfehlung):
        if row_outside:
            return OUTSIDE_COVERAGE_CATEGORY
        if row_no_data:
            return NO_MATCH_CATEGORY
        return row_empfehlung

    df["category"] = [
        _category(o, n, e) for o, n, e in zip(outside, no_data, df["empfehlung"])
    ]

    def _impact(idx):
        cat = df.at[idx, "category"]
        if cat == OUTSIDE_COVERAGE_CATEGORY:
            return "Outside Hamburg's heat-planning coverage area"
        if cat == NO_MATCH_CATEGORY:
            return "No recommendation data for this location — check manually"

        dh = is_dh[idx]

        if cat == DECENTRAL_CATEGORY:
            return ("Warning: currently district heating, area plan recommends decentral "
                    "supply (possible loss)") if dh else "Decentral per map"

        if dh:
            return "No change (already district heating)"

        if cat == EQUAL_CATEGORY:
            return "Equally suited for district heating or decentral — not currently on either"
        if cat == "Existing network / densification":
            return "District heating network already exists here, not currently used (possible gain)"
        if cat.startswith("Network expansion from "):
            year = cat.replace("Network expansion from ", "")
            return f"District heating network expansion planned from {year} — not currently available (future possible gain)"
        if cat == EIGENINITIATIVE_CATEGORY:
            return ("District heating expansion possible via owner-initiative request — "
                    "not currently available (possible gain, action required)")
        return f"Not currently district heating; area plan: {cat}"

    df["impact"] = [_impact(i) for i in df.index]

    def _action(idx):
        cat = df.at[idx, "category"]
        if cat == DECENTRAL_CATEGORY:
            return "Propose LWP — decision: Kristian Duus"
        if cat == EIGENINITIATIVE_CATEGORY:
            operator = df.at[idx, "betreiber_vorschlag"]
            operator_txt = f" (proposed operator: {operator})" if pd.notna(operator) else ""
            return f"Consider requesting district-heating connection (Eigeninitiative){operator_txt}"
        return ""

    df["proposed_action"] = [_action(i) for i in df.index]

    def _expected_availability(idx):
        cat = df.at[idx, "category"]
        if is_dh[idx] and cat not in (OUTSIDE_COVERAGE_CATEGORY, NO_MATCH_CATEGORY):
            # Already receiving district heating today — the area's rollout
            # year (if any) describes the broader area, not this building.
            return "Already available"
        return CATEGORY_TO_AVAILABILITY.get(cat, "Not applicable")

    df["expected_availability"] = [_expected_availability(i) for i in df.index]
    return df


def main() -> None:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    log.info("Loading buildings from %s", BUILDINGS_CSV)
    buildings = load_buildings()
    log.info("Loaded %d buildings", len(buildings))

    log.info("Fetching Zielszenario polygons from Hamburg WFS...")
    polygons = fetch_zielszenario()
    log.info("Fetched %d polygons", len(polygons))

    joined = buildings_within_zielszenario(buildings, polygons)
    result = classify(joined)

    output_cols = [
        "building_id", "address", "longitude", "latitude", "heating_type",
        "gebietseinheit", "match_method", "category", "expected_availability",
        "impact", "proposed_action",
        "betreiber", "betreiber_vorschlag", "kommentar", "link_building_website",
    ]
    result = result[output_cols]
    result.to_excel(OUTPUT_XLSX, index=False)
    log.info("Wrote %s", OUTPUT_XLSX)

    summary = result["category"].value_counts()
    log.info("Category counts:\n%s", summary.to_string())
    impact_summary = result["impact"].value_counts()
    log.info("Impact counts:\n%s", impact_summary.to_string())


if __name__ == "__main__":
    main()
