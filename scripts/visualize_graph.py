#!/usr/bin/env python3
"""Generate a knowledge-graph visualization from ~/.mnemon/mnemon.db for README."""

import math
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import networkx as nx
import numpy as np

DB_PATH = Path.home() / ".mnemon" / "data" / "default" / "mnemon.db"
OUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "diagrams" / "10-mnemon-graph.jpg"

# ── colour palette ──────────────────────────────────────────────────
CATEGORY_COLOURS = {
    "fact":       "#58a6ff",   # blue
    "insight":    "#bc8cff",   # purple
    "decision":   "#f78166",   # orange
    "context":    "#3fb950",   # green
    "preference": "#f9e2af",   # yellow
    "general":    "#8b949e",   # grey
}

EDGE_COLOURS = {
    "temporal":  "#3d444d",
    "entity":    "#58a6ff",
    "semantic":  "#bc8cff",
    "causal":    "#f78166",
    "narrative": "#3fb950",
}

BG_COLOUR = "#0d1117"


def load_data():
    """Return nodes dict and edges grouped by type."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    nodes = {}
    for r in conn.execute(
        "SELECT id, category, importance, access_count "
        "FROM insights WHERE deleted_at IS NULL"
    ):
        nodes[r["id"]] = dict(r)

    edges_by_type = defaultdict(list)  # type -> [(src, tgt, weight)]
    for r in conn.execute("SELECT source_id, target_id, edge_type, weight FROM edges"):
        src, tgt = r["source_id"], r["target_id"]
        if src in nodes and tgt in nodes:
            edges_by_type[r["edge_type"]].append((src, tgt, r["weight"]))

    conn.close()
    return nodes, edges_by_type


def build_layout_graph(nodes, edges_by_type):
    """Build a weighted graph for layout (merge all edge types)."""
    G = nx.Graph()
    for nid in nodes:
        G.add_node(nid)

    # Accumulate weights: entity and semantic edges matter more for layout
    type_weight = {"temporal": 0.3, "entity": 1.0, "semantic": 1.5,
                   "causal": 2.0, "narrative": 1.0}
    pair_weights = defaultdict(float)
    for etype, elist in edges_by_type.items():
        w = type_weight.get(etype, 1.0)
        for src, tgt, ew in elist:
            key = (min(src, tgt), max(src, tgt))
            pair_weights[key] += w * ew

    for (u, v), w in pair_weights.items():
        G.add_edge(u, v, weight=w)

    return G


def draw(nodes, edges_by_type) -> None:
    fig, ax = plt.subplots(figsize=(16, 10), facecolor=BG_COLOUR)
    ax.set_facecolor(BG_COLOUR)

    G = build_layout_graph(nodes, edges_by_type)

    # ── layout ──────────────────────────────────────────────────────
    # Use spring layout then pull isolated nodes closer to the centre
    pos = nx.spring_layout(G, k=1.4 / math.sqrt(max(len(G), 1)),
                           iterations=300, seed=42, weight="weight")

    # pull low-degree nodes toward centre of mass
    cx = np.mean([p[0] for p in pos.values()])
    cy = np.mean([p[1] for p in pos.values()])
    for nid in pos:
        deg = G.degree(nid)
        if deg <= 2:
            strength = 0.55
            pos[nid] = np.array([
                pos[nid][0] + (cx - pos[nid][0]) * strength,
                pos[nid][1] + (cy - pos[nid][1]) * strength,
            ])

    # normalise to [margin, 1-margin] filling the 16:10 canvas
    xs = np.array([p[0] for p in pos.values()])
    ys = np.array([p[1] for p in pos.values()])
    pad = 0.06
    xmin, xmax = xs.min(), xs.max()
    ymin, ymax = ys.min(), ys.max()
    xr = (xmax - xmin) or 1
    yr = (ymax - ymin) or 1
    for nid in pos:
        pos[nid] = np.array([
            (pos[nid][0] - xmin) / xr * (1 - 2 * pad) + pad,
            (pos[nid][1] - ymin) / yr * (1 - 2 * pad) + pad,
        ])

    # ── draw edges by type (layered, temporal at bottom) ────────────
    edge_style = {
        #              alpha  width
        "temporal":  (0.04,  0.2),
        "entity":    (0.12,  0.4),
        "semantic":  (0.25,  0.7),
        "narrative": (0.30,  0.8),
        "causal":    (0.45,  1.0),
    }
    edge_order = ["temporal", "entity", "semantic", "narrative", "causal"]

    total_edge_count = 0
    for etype in edge_order:
        elist = edges_by_type.get(etype, [])
        if not elist:
            continue
        total_edge_count += len(elist)
        alpha, width = edge_style[etype]
        colour = EDGE_COLOURS[etype]
        # deduplicate for undirected drawing
        seen = set()
        draw_list = []
        for src, tgt, w in elist:
            key = (min(src, tgt), max(src, tgt))
            if key not in seen:
                seen.add(key)
                draw_list.append((src, tgt))
        nx.draw_networkx_edges(G, pos, edgelist=draw_list, ax=ax,
                               edge_color=colour, alpha=alpha, width=width)

    # ── node glow (larger translucent circles beneath) ──────────────
    node_list = list(nodes.keys())
    colours = [CATEGORY_COLOURS.get(nodes[n].get("category", "general"), "#8b949e")
               for n in node_list]
    sizes = [50 + 5 * nodes[n].get("access_count", 0) for n in node_list]
    glow_sizes = [s * 3.5 for s in sizes]

    nx.draw_networkx_nodes(G, pos, nodelist=node_list, ax=ax,
                           node_color=colours, node_size=glow_sizes,
                           alpha=0.08, linewidths=0)

    # ── actual nodes ────────────────────────────────────────────────
    nx.draw_networkx_nodes(G, pos, nodelist=node_list, ax=ax,
                           node_color=colours, node_size=sizes,
                           edgecolors="#30363d", linewidths=0.4, alpha=0.92)

    # ── legend ──────────────────────────────────────────────────────
    cat_patches = [mpatches.Patch(color=c, label=l.capitalize())
                   for l, c in CATEGORY_COLOURS.items()]
    edge_lines = [plt.Line2D([0], [0], color=EDGE_COLOURS[t], lw=2, label=t.capitalize())
                  for t in ["entity", "semantic", "causal"]]

    legend1 = ax.legend(handles=cat_patches, loc="upper left",
                        fontsize=8, title="Node category", title_fontsize=9,
                        facecolor="#161b22", edgecolor="#30363d",
                        labelcolor="#c9d1d9", framealpha=0.92)
    legend1.get_title().set_color("#c9d1d9")
    ax.add_artist(legend1)

    legend2 = ax.legend(handles=edge_lines, loc="lower left",
                        fontsize=8, title="Edge type", title_fontsize=9,
                        facecolor="#161b22", edgecolor="#30363d",
                        labelcolor="#c9d1d9", framealpha=0.92)
    legend2.get_title().set_color("#c9d1d9")

    # ── stats annotation ────────────────────────────────────────────
    n_nodes = len(nodes)
    stats_text = f"{n_nodes} insights  ·  {total_edge_count} edges"
    ax.text(0.99, 0.02, stats_text, transform=ax.transAxes,
            fontsize=9, color="#8b949e", ha="right", va="bottom",
            fontfamily="monospace")

    ax.axis("off")
    plt.tight_layout(pad=0.5)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(OUT_PATH), dpi=200, facecolor=BG_COLOUR,
                bbox_inches="tight", pad_inches=0.3)
    plt.close(fig)
    print(f"✓ Saved → {OUT_PATH}  ({os.path.getsize(OUT_PATH) / 1024:.0f} KB)")


if __name__ == "__main__":
    nodes, edges_by_type = load_data()
    total = sum(len(v) for v in edges_by_type.values())
    print(f"Loaded {len(nodes)} nodes, {total} edges")
    for t, e in sorted(edges_by_type.items()):
        print(f"  {t}: {len(e)}")
    draw(nodes, edges_by_type)
