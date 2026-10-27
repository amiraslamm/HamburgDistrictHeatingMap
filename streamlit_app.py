"""
DEP-71 — Sprinkenhof district-heating map analysis, interactive viewer.

Wraps script/analyze_waermeplanung.py (fetch + spatial join + classify) with a
Streamlit UI: plain-language legend, summary metrics, a filterable/searchable
table, and an interactive Folium map that uses Hamburg's own live WMS as the
background layer (the exact official rendering) instead of redrawing 26k
polygons client-side.

Data source: Hamburg's official "Zielszenario" (target scenario) layer — see
script/analyze_waermeplanung.py's module docstring for how this was confirmed
(Sebastian's direct portal deep-link, resolved via Hamburg's services-internet.json).

Run:
  conda run -n bwv-analysis streamlit run streamlit_app.py
(from this project's root directory)
"""

import sys
from io import BytesIO
from pathlib import Path

import folium
from folium.plugins import Fullscreen
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "script"))
import analyze_waermeplanung as core  # noqa: E402


def _bootstrap_data_from_secrets() -> None:
    """
    import/ and export/ are gitignored (real building addresses/operators
    shouldn't live in the repo), so a fresh deploy (e.g. Streamlit Community
    Cloud) starts with neither the buildings CSV nor the precomputed Excel on
    disk. Restore the buildings CSV from Streamlit secrets (base64, set via
    the app's "Secrets" settings — never committed to git) if it's missing,
    then run the live analysis once to produce the output Excel.
    """
    core.IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    if not core.BUILDINGS_CSV.exists():
        b64 = st.secrets.get("buildings_csv_b64")
        if not b64:
            return
        import base64
        core.BUILDINGS_CSV.write_bytes(base64.b64decode(b64))

    if not core.OUTPUT_XLSX.exists() and core.BUILDINGS_CSV.exists():
        with st.spinner("First run: fetching Hamburg's live heat-planning data (~30-40s)..."):
            buildings = core.load_buildings()
            polygons = core.fetch_zielszenario()
            joined = core.buildings_within_zielszenario(buildings, polygons)
            result = core.classify(joined)
            output_cols = [
                "building_id", "address", "longitude", "latitude", "heating_type",
                "gebietseinheit", "match_method", "category", "expected_availability",
                "impact", "proposed_action",
                "betreiber", "betreiber_vorschlag", "kommentar", "link_building_website",
            ]
            core.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
            result[output_cols].to_excel(core.OUTPUT_XLSX, index=False)


_bootstrap_data_from_secrets()


WMS_ATTRIBUTION = "© Freie und Hansestadt Hamburg, BUKEA (Datenlizenz Deutschland Namensnennung 2.0)"
ZIELSZENARIO_WMS_URL = "https://geodienste.hamburg.de/wms_zielszenario_kwp"
ZIELSZENARIO_WMS_LAYER = "zielszenario"

# Hamburg's own official colours (from the WFS's own `farbcode` field).
CATEGORY_COLOURS = {
    "Existing network / densification": "#8d0015",
    "Network expansion from 2026": "#fb6a4a",
    "Network expansion from 2030": "#fcae91",
    "Network expansion from 2035": "#ffdcc7",
    "Network expansion (owner-initiative required)": "#ffdb99",
    "Equally suited for decentral or network": "#b50294",
    "Decentral heat supply": "#0000ff",
    "Outside Hamburg coverage": "#555555",
    "No map match (outside suitability layer)": "#000000",
}
# Hamburg's own official Zielszenario legend, exactly as shown in their portal
# (https://geoportal-hamburg.de/waermeportal-hamburg/) — same order, same
# German labels, same colours (from `farbcode`), plus an English gloss. This
# is the legend for the WMS *background* colours, not for our map dots/impact
# buckets (see IMPACT_BUCKET_STYLE / _LEGEND_ROWS below for those).
ZIELSZENARIO_LEGEND = [
    ("#8d0015", "Vorhandenes Wärmenetz und Wärmenetzverdichtung", "Existing network & densification"),
    ("#fb6a4a", "Wärmenetzausbau ab 2026", "Network expansion from 2026"),
    ("#fcae91", "Wärmenetzausbau ab 2030", "Network expansion from 2030"),
    ("#ffdcc7", "Wärmenetzausbau ab 2035", "Network expansion from 2035"),
    ("#ffdb99", "Wärmenetzausbau, Eigeninitiative notwendig", "Network expansion, owner-initiative required"),
    ("#b50294", "Für dezentral und Wärmenetz gleichwertig geeignet", "Equally suited for decentral or network"),
    ("#0000ff", "Dezentrale Wärmeversorgung", "Decentral heat supply"),
]
# Hatched overlay in Hamburg's own legend — not one of the `empfehlung`
# categories in our data, shown only so the legend matches their portal 1:1.
ZIELSZENARIO_LEGEND_HATCHED = ("Stadtentwicklungsgebiete", "Urban development areas")

# Single source of truth for the map-dot colour per impact bucket — used both
# for the actual map markers and for the legend table's "Map dot" column, so
# the two never drift apart.
IMPACT_BUCKET_STYLE = {
    "No change": ("#8d0015", 5, 1),
    "Possible gain": ("#00cccc", 7, 2),
    "Decentral (LWP proposed)": ("#0000ff", 5, 1),
    "Possible loss": ("#ff00ff", 9, 3),
    "Equally suited": ("#b50294", 6, 1),
    "Outside coverage": ("#555555", 5, 1),
    "No map match": ("#000000", 5, 1),
}

st.set_page_config(page_title="DEP-71 — Sprinkenhof Wärmeplanung", layout="wide")


# ---------------------------------------------------------------------------
# Data loading / refresh
# ---------------------------------------------------------------------------
AVAILABILITY_STATUS_MAP = {
    # Only buildings that genuinely already have district heating today.
    "Already available": "Available",
    # Network exists in the area but this building isn't connected, plus any
    # path that requires a future step (expansion year, or owner-initiated
    # request) — a plan/opportunity, not district heating in hand yet.
    "Available now — not yet connected": "Planned",
    "2026": "Planned",
    "2030": "Planned",
    "2035": "Planned",
    "No fixed date — requires owner request": "Planned",
    "Not planned (decentral recommended)": "Not Planned",
    "Uncertain — equally suited either way": "Not Planned",
    "Not applicable": "Not Planned",
}


def availability_status(expected_availability: str) -> str:
    return AVAILABILITY_STATUS_MAP.get(str(expected_availability), "Not Planned")


# Only the three expansion-year buckets have an actual fixed year; the other
# two "Planned" paths (network already physically there / owner-initiative,
# no rollout year) get a descriptive placeholder instead of a year — and
# non-"Planned" rows get a blank, since the question doesn't apply to them.
PLANNED_YEAR_MAP = {
    "2026": "2026",
    "2030": "2030",
    "2035": "2035",
    "Available now — not yet connected": "No fixed year (network exists, connection needed)",
    "No fixed date — requires owner request": "No fixed year (owner-initiative required)",
}


def planned_year(expected_availability: str) -> str:
    return PLANNED_YEAR_MAP.get(str(expected_availability), "")


@st.cache_data
def load_results() -> pd.DataFrame:
    df = pd.read_excel(core.OUTPUT_XLSX)
    df["availability_status"] = df["expected_availability"].apply(availability_status)
    df["planned_year"] = df["expected_availability"].apply(planned_year)

    def bucket(impact: str) -> str:
        s = str(impact)
        if "possible loss" in s.lower():
            return "Possible loss"
        if s == "Decentral per map":
            return "Decentral (LWP proposed)"
        if s == "No change (already district heating)":
            return "No change"
        if "equally suited" in s.lower():
            return "Equally suited"
        if "possible gain" in s.lower():
            return "Possible gain"
        if "outside hamburg" in s.lower():
            return "Outside coverage"
        if "no recommendation data" in s.lower():
            return "No map match"
        return "Other / no data"

    df["impact_bucket"] = df["impact"].apply(bucket)
    return df


REQUIRED_BUILDING_COLUMNS = {"building_id", "address", "longitude", "latitude", "heating_type"}


def run_live_refresh(csv_path: Path) -> None:
    with st.spinner("Fetching live data from Hamburg WFS and re-running the analysis (~30-40s)..."):
        buildings = core.load_buildings(csv_path)
        polygons = core.fetch_zielszenario()
        joined = core.buildings_within_zielszenario(buildings, polygons)
        result = core.classify(joined)
        output_cols = [
            "building_id", "address", "longitude", "latitude", "heating_type",
            "gebietseinheit", "match_method", "category", "expected_availability",
            "impact", "proposed_action",
            "betreiber", "betreiber_vorschlag", "kommentar", "link_building_website",
        ]
        result[output_cols].to_excel(core.OUTPUT_XLSX, index=False)
    load_results.clear()
    st.success("Refreshed from Hamburg's live heat-planning data.")


# ---------------------------------------------------------------------------
# Header + plain-language legend
# ---------------------------------------------------------------------------
st.title("DEP-71 — Sprinkenhof vs. Hamburg's District Heating Map")
st.caption(
    "Every Sprinkenhof building checked against Hamburg's official "
    "Zielszenario (target scenario) — the city's final per-area heat-supply recommendation."
)

def _dot(colour: str) -> str:
    return (
        f'<span style="display:inline-block;width:12px;height:12px;border-radius:50%;'
        f'background:{colour};border:1px solid black;"></span>'
    )


_LEGEND_ROWS = [
    ("No change", "Building already has district heating (Fernwärme), and the area plan agrees it should keep it. Nothing to do."),
    ("Possible gain", "Building does <b>not</b> have district heating today, but the area plan recommends it — either the network already exists there, expansion is planned (see Category/Impact for the specific year, e.g. 2026/2030/2035), or it's available via an owner-initiative request. Worth checking."),
    ("Decentral (LWP proposed)", "Building doesn't have district heating, and the area plan recommends decentral supply instead — a heat pump (LWP) is proposed."),
    ("Possible loss ⚠️", "Building <b>currently has</b> district heating, but the area plan recommends decentral supply for this area going forward — the ones most worth double-checking manually."),
    ("Equally suited", "The area plan rates district heating and decentral supply as equally good options here — no strong recommendation either way."),
    ("Outside coverage", "Building sits outside Hamburg's heat-planning coverage area (e.g. Neuwerk island, Geesthacht) — the map has no data for it at all, and it isn't plotted by default."),
]

with st.expander("What does this mean?", expanded=True):
    rows_html = ""
    for label, meaning in _LEGEND_ROWS:
        key = label.replace(" ⚠️", "")
        colour = IMPACT_BUCKET_STYLE[key][0]
        rows_html += (
            f"<tr><td style='text-align:center'>{_dot(colour)}</td>"
            f"<td><b>{label}</b></td><td>{meaning}</td></tr>"
        )
    st.markdown(
        f"""
<table>
<tr><th>Map dot</th><th>You'll see in Impact</th><th>What it means</th></tr>
{rows_html}
</table>

Background map colour = Hamburg's own Zielszenario recommendation per area (dark red = existing network, lighter oranges = planned expansion by year, blue = decentral, purple = equally suited). Not a color scheme I chose — it's their official legend, loaded live from their WMS.
        """,
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
st.sidebar.header("Controls")

st.sidebar.subheader("Buildings data")
uploaded = st.sidebar.file_uploader(
    "Upload a new buildings CSV",
    type=["csv"],
    help=f"Must have columns: {', '.join(sorted(REQUIRED_BUILDING_COLUMNS))}",
)
if uploaded is not None:
    dest = core.IMPORT_DIR / uploaded.name
    dest.write_bytes(uploaded.getvalue())
    st.sidebar.success(f"Saved as `{dest.name}` — select it below and click Refresh.")

csv_files = sorted(core.IMPORT_DIR.glob("*.csv"))
default_idx = next(
    (i for i, p in enumerate(csv_files) if p.name == core.BUILDINGS_CSV.name), 0
)
selected_csv = st.sidebar.selectbox(
    "Buildings CSV to use",
    options=csv_files,
    index=default_idx,
    format_func=lambda p: p.name,
)

# Validate columns before allowing a run against this file.
try:
    _preview = pd.read_csv(selected_csv, nrows=5)
    _missing = REQUIRED_BUILDING_COLUMNS - set(_preview.columns)
except Exception as exc:
    _preview, _missing = None, None
    st.sidebar.error(f"Could not read `{selected_csv.name}`: {exc}")

if _missing:
    st.sidebar.error(f"`{selected_csv.name}` is missing required column(s): {', '.join(sorted(_missing))}")
elif _preview is not None:
    with st.sidebar.expander(f"Preview `{selected_csv.name}`"):
        st.dataframe(_preview, hide_index=True)

if st.sidebar.button(
    "Refresh from Hamburg (live, ~30s)", type="primary", disabled=bool(_missing)
):
    run_live_refresh(selected_csv)

df = load_results()

bucket_options = ["All"] + sorted(df["impact_bucket"].unique().tolist())
selected_bucket = st.sidebar.selectbox("Filter by impact", bucket_options, index=0)

search = st.sidebar.text_input("Search address", "")

show_zielszenario_wms = st.sidebar.checkbox("Show Hamburg's Zielszenario layer (live WMS)", value=True)

filtered = df.copy()
if selected_bucket != "All":
    filtered = filtered[filtered["impact_bucket"] == selected_bucket]
if search:
    filtered = filtered[filtered["address"].str.contains(search, case=False, na=False)]

st.sidebar.caption(f"{len(filtered)} of {len(df)} buildings shown")

# ---------------------------------------------------------------------------
# Summary metrics
# ---------------------------------------------------------------------------
counts = df["impact_bucket"].value_counts()
cols = st.columns(5)
cols[0].metric("Total buildings", len(df))
cols[1].metric("Possible loss ⚠️", int(counts.get("Possible loss", 0)))
cols[2].metric("Possible gain", int(counts.get("Possible gain", 0)))
cols[3].metric("Decentral (LWP)", int(counts.get("Decentral (LWP proposed)", 0)))
cols[4].metric("Outside coverage", int(counts.get("Outside coverage", 0)))

# ---------------------------------------------------------------------------
# Interactive map
# ---------------------------------------------------------------------------
st.subheader("Map")

showing_outside_only = selected_bucket == "Outside coverage"

if showing_outside_only:
    # These buildings are far from the rest of the portfolio (Neuwerk island,
    # Geesthacht, etc.) — when the user has filtered specifically to see them,
    # show them for real and zoom the map to fit their actual location(s),
    # instead of the usual exclude-and-center-on-Hamburg behaviour.
    on_map = filtered.copy()
else:
    on_map = filtered[filtered["category"] != "Outside Hamburg coverage"].copy()

if len(on_map):
    center_lat, center_lon = on_map["latitude"].mean(), on_map["longitude"].mean()
else:
    center_lat, center_lon = 53.55, 10.0

if show_zielszenario_wms:
    map_col, legend_col = st.columns([4, 1])
else:
    map_col, legend_col = st.container(), None

m = folium.Map(location=[center_lat, center_lon], zoom_start=11, tiles="OpenStreetMap")

if showing_outside_only and len(on_map):
    bounds = [
        [on_map["latitude"].min(), on_map["longitude"].min()],
        [on_map["latitude"].max(), on_map["longitude"].max()],
    ]
    m.fit_bounds(bounds, padding=(40, 40))

if show_zielszenario_wms:
    folium.raster_layers.WmsTileLayer(
        url=ZIELSZENARIO_WMS_URL,
        layers=ZIELSZENARIO_WMS_LAYER,
        fmt="image/png",
        transparent=True,
        version="1.3.0",
        attr=WMS_ATTRIBUTION,
        name="Hamburg: Zielszenario",
        overlay=True,
        control=True,
    ).add_to(m)

for _, row in on_map.iterrows():
    bucket = row["impact_bucket"]
    fallback = (CATEGORY_COLOURS.get(row["category"], "#000000"), 5, 1)
    colour, radius, weight = IMPACT_BUCKET_STYLE.get(bucket, fallback)

    popup_html = (
        f"<b>{row['address']}</b><br>"
        f"Current heating: {row['heating_type']}<br>"
        f"<b>Category:</b> {row['category']}<br>"
        f"<b>Expected availability:</b> {row.get('expected_availability', 'n/a')}<br>"
        f"<b>Impact:</b> {row['impact']}<br>"
        + (f"<b>Proposed action:</b> {row['proposed_action']}<br>" if row.get("proposed_action") else "")
    )
    folium.CircleMarker(
        location=[row["latitude"], row["longitude"]],
        radius=radius,
        color="black",
        weight=weight,
        fill=True,
        fill_color=colour,
        fill_opacity=0.9,
        popup=folium.Popup(popup_html, max_width=300),
        tooltip=row["address"],
    ).add_to(m)

folium.LayerControl().add_to(m)
Fullscreen(
    position="topleft",
    title="Enlarge map",
    title_cancel="Exit fullscreen",
    force_separate_button=True,
).add_to(m)

off_map_count = len(filtered) - len(on_map)

with map_col:
    if off_map_count:
        st.caption(f"{off_map_count} building(s) outside Hamburg's coverage area are not plotted on the map — see the table below.")
    st_folium(m, use_container_width=True, height=650, returned_objects=[])

if legend_col is not None:
    with legend_col:
        st.markdown("**Legende**")
        st.caption("Zielszenario — Hamburg's official background-colour legend")
        rows_html = "".join(
            f"<div style='display:flex;align-items:flex-start;gap:6px;margin-bottom:6px;'>"
            f"<span style='flex:0 0 auto;width:14px;height:14px;margin-top:2px;"
            f"background:{colour};border:1px solid #333;'></span>"
            f"<span style='font-size:0.78rem;line-height:1.15;'>{de}<br>"
            f"<span style='color:#777'>{en}</span></span></div>"
            for colour, de, en in ZIELSZENARIO_LEGEND
        )
        hatch_de, hatch_en = ZIELSZENARIO_LEGEND_HATCHED
        rows_html += (
            "<div style='display:flex;align-items:flex-start;gap:6px;'>"
            "<span style='flex:0 0 auto;width:14px;height:14px;margin-top:2px;border:1px solid #333;"
            "background:repeating-linear-gradient(45deg,#fff,#fff 2px,#999 2px,#999 3px);'></span>"
            f"<span style='font-size:0.78rem;line-height:1.15;'>{hatch_de}<br>"
            f"<span style='color:#777'>{hatch_en}</span></span></div>"
        )
        st.markdown(rows_html, unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Table + downloads
# ---------------------------------------------------------------------------
st.subheader(f"Buildings ({len(filtered)})")

# Display-only renaming/reordering — the underlying Excel/data columns keep
# their original technical names (building_id, gebietseinheit, ...); this is
# purely how the on-screen table presents them.
TABLE_COLUMN_ORDER = [
    "building_id", "address", "heating_type",
    "category", "expected_availability", "availability_status", "planned_year", "impact_bucket", "impact", "proposed_action",
    "betreiber", "betreiber_vorschlag",
    "gebietseinheit", "match_method", "longitude", "latitude",
]
TABLE_COLUMN_LABELS = {
    "building_id": "Building ID",
    "address": "Address",
    "heating_type": "Current Heating",
    "category": "Category",
    "expected_availability": "Expected Availability",
    "availability_status": "Availability Status",
    "planned_year": "Planned Year",
    "impact_bucket": "Impact Category",
    "impact": "Impact",
    "proposed_action": "Proposed Action",
    "betreiber": "Current Network Operator",
    "betreiber_vorschlag": "Proposed Operator (Expansion)",
    "gebietseinheit": "Area ID",
    "match_method": "Match Method",
    "longitude": "Longitude",
    "latitude": "Latitude",
}

st.dataframe(
    filtered[TABLE_COLUMN_ORDER],
    column_config={
        col: st.column_config.Column(label=label)
        for col, label in TABLE_COLUMN_LABELS.items()
    },
    use_container_width=True,
    hide_index=True,
)

dl_cols = st.columns(2)

full_buf = BytesIO()
df.to_excel(full_buf, index=False)
dl_cols[0].download_button(
    "Download full results (.xlsx)",
    data=full_buf.getvalue(),
    file_name=core.OUTPUT_XLSX.name,
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

filtered_buf = BytesIO()
filtered.to_excel(filtered_buf, index=False)
dl_cols[1].download_button(
    f"Download filtered view ({len(filtered)} rows) (.xlsx)",
    data=filtered_buf.getvalue(),
    file_name=f"DEP-71_filtered_{selected_bucket.replace(' ', '_')}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
