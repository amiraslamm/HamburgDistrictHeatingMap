"""
DEP-71 — render a map of the Sprinkenhof buildings against Hamburg's real
"Zielszenario" (target scenario) polygons, in the same style as the Berlin BWV
Fernwärme map (deployment_tools/bwv-analysis/fernwaerme/plotting.py): coloured
Zielszenario polygons + building dots coloured by category + marker overlays
for buildings worth double-checking.

Run (after analyze_waermeplanung.py has produced the Excel + cached WFS):
  conda run -n bwv-analysis python script/plot_waermeplanung_map.py
"""

from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IMPORT_DIR = PROJECT_ROOT / "import"
EXPORT_DIR = PROJECT_ROOT / "export"

RESULTS_XLSX = EXPORT_DIR / "DEP-71_Sprinkenhof_Waermeplanung_Zielszenario.xlsx"
WFS_CACHE_PATH = IMPORT_DIR / "hh_zielszenario_wfs_cache.geojson"
OUTPUT_MAP = EXPORT_DIR / "DEP-71_Sprinkenhof_Waermeplanung_map.jpg"

# Hamburg's own official colours for each Zielszenario recommendation (from the
# WFS's own `farbcode` field) — not colours I chose.
CATEGORY_COLOURS = {
    "Existing network / densification": "#8d0015",
    "Network expansion from 2026": "#fb6a4a",
    "Network expansion from 2030": "#fcae91",
    "Network expansion from 2035": "#ffdcc7",
    "Network expansion (owner-initiative required)": "#ffdb99",
    "Equally suited for decentral or network": "#b50294",
    "Decentral heat supply": "#0000ff",
}
OUTSIDE_LABEL = "Outside Hamburg coverage"
DEFAULT_COLOUR = "#000000"

POINT_SIZE = 35


def main() -> None:
    results = pd.read_excel(RESULTS_XLSX)
    polygons = gpd.read_file(WFS_CACHE_PATH)  # already EPSG:4326 (cached post-reproject)

    on_map = results[results["category"] != OUTSIDE_LABEL].copy()
    off_map = results[results["category"] == OUTSIDE_LABEL]

    fig, ax = plt.subplots(figsize=(22, 22), dpi=300)

    # Background: the portal's own Zielszenario colour scheme (red shades = network
    # timing tiers, blue = decentral, magenta = equally suited).
    for farbe, group in polygons.dropna(subset=["farbcode"]).groupby("farbcode"):
        group.plot(ax=ax, color=farbe, edgecolor="none", alpha=0.45, zorder=1)

    point_colours = on_map["category"].map(CATEGORY_COLOURS).fillna(DEFAULT_COLOUR)
    ax.scatter(
        on_map["longitude"], on_map["latitude"],
        c=point_colours, s=POINT_SIZE, marker="o",
        edgecolor="white", linewidths=0.5, zorder=5,
    )

    # Overlay: currently district-heated, area plan recommends decentral (possible loss)
    wegfall = on_map[on_map["impact"].str.contains("possible loss", case=False, na=False)]
    if len(wegfall):
        ax.scatter(
            wegfall["longitude"], wegfall["latitude"],
            c="#ff00ff", s=POINT_SIZE * 5, marker="X",
            edgecolor="black", linewidths=0.7, zorder=8,
        )

    # Overlay: not currently on Fernwärme, area plan says network exists/expanding (possible gain)
    zugewinn = on_map[on_map["impact"].str.contains("possible gain", case=False, na=False)]
    if len(zugewinn):
        ax.scatter(
            zugewinn["longitude"], zugewinn["latitude"],
            c="#00ffff", s=POINT_SIZE * 2.2, marker="D",
            edgecolor="black", linewidths=0.4, zorder=7,
        )

    # Fix extent to the Hamburg building/polygon area — Neuwerk (~90km out) and
    # Geesthacht (~5km outside the city state) are excluded from the frame (see
    # off-map stats box) so they don't collapse the rest of the map to a dot.
    minx, miny, maxx, maxy = on_map["longitude"].min(), on_map["latitude"].min(), \
        on_map["longitude"].max(), on_map["latitude"].max()
    pad_x, pad_y = (maxx - minx) * 0.03, (maxy - miny) * 0.03
    ax.set_xlim(minx - pad_x, maxx + pad_x)
    ax.set_ylim(miny - pad_y, maxy + pad_y)

    counts = on_map["category"].value_counts()
    stats_lines = [f"{cat}: {counts.get(cat, 0)}" for cat in CATEGORY_COLOURS]
    stats_lines.append(f"Possible loss (X): {len(wegfall)}")
    stats_lines.append(f"Possible gain (◇): {len(zugewinn)}")
    stats_lines.append(f"Outside map extent (Neuwerk/Geesthacht/etc.): {len(off_map)}")
    ax.text(
        0.98, 0.02, "\n".join(stats_lines),
        transform=ax.transAxes, fontsize=13,
        verticalalignment="bottom", horizontalalignment="right",
        bbox=dict(boxstyle="round,pad=0.5", facecolor="white", alpha=0.85),
        zorder=9,
    )

    handles = [
        plt.Line2D([0], [0], marker="o", color="w", markerfacecolor=colour,
                   markersize=12, label=label, markeredgecolor="black")
        for label, colour in CATEGORY_COLOURS.items()
    ]
    if len(wegfall):
        handles.append(plt.Line2D([0], [0], marker="X", color="w", markerfacecolor="#ff00ff",
                                   markersize=13, label="Possible loss (DH → decentral)",
                                   markeredgecolor="black"))
    if len(zugewinn):
        handles.append(plt.Line2D([0], [0], marker="D", color="w", markerfacecolor="#00ffff",
                                   markersize=11, label="Possible gain (non-DH → network)",
                                   markeredgecolor="black"))
    ax.legend(handles=handles, fontsize=11, loc="upper left")

    ax.set_title(
        "DEP-71 — Sprinkenhof Buildings vs. Hamburg's Zielszenario (Target Scenario)",
        fontsize=16,
    )
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")

    plt.savefig(OUTPUT_MAP, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"Map saved to {OUTPUT_MAP}")
    print(f"({len(off_map)} buildings excluded from map extent — see Excel for Neuwerk/Geesthacht/etc.)")


if __name__ == "__main__":
    main()
