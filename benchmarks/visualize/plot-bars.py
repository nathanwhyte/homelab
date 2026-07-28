#!/usr/bin/env python3
"""Clean "marketing-style" bar charts for the Ollama concurrency report.

Mimics the minimal bar-chart look used on Apple/Qwen benchmark pages:
no gridlines, bold value labels, muted accent colors, title + gray
subtitle, optional footer caption. Reads `summary.csv` from a report
directory and writes PNGs back into it.

Run: uv run --with matplotlib python3 benchmarks/visualize/plot-bars.py \
    --report-dir benchmarks/results/report-2026-07-01
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt

INK = "#1d1d1f"
GRAY_TEXT = "#86868b"
GRID_GRAY = "#d2d2d7"

# Below this share of usable answers, a throughput bar is annotated as suspect.
# Rows from before the quality columns existed have no usable_rate and are left
# unannotated rather than assumed bad.
USABLE_RATE_WARN = 0.9

PALETTE = {
    "purple": "#c4b8f5",
    "green": "#b7f0c9",
    "pink": "#f7ccd6",
    "gray_dark": "#8e8e93",
    "gray_light": "#e5e5ea",
}


def style_axes(ax) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.spines["bottom"].set_color(GRID_GRAY)
    ax.tick_params(colors=GRAY_TEXT, labelsize=9)
    ax.set_facecolor("white")


def load_rows(csv_path: Path) -> list[dict]:
    with csv_path.open() as f:
        return list(csv.DictReader(f))


def pick(rows: list[dict], run: str, concurrency: int) -> dict:
    for r in rows:
        if r["run"] == run and int(r["concurrency"]) == concurrency:
            return r
    raise KeyError(f"no row for {run} @ conc={concurrency}")


def horizontal_bar_chart(
    outpath: Path,
    title: str,
    subtitle: str,
    footer: str,
    labels: list[str],
    values: list[float],
    colors: list[str],
    unit: str,
) -> None:
    fig, ax = plt.subplots(figsize=(7.5, 0.9 + 0.9 * len(labels)))
    fig.patch.set_facecolor("white")

    y = range(len(labels))
    bars = ax.barh(list(y), values, color=colors, height=0.55, zorder=3)

    for bar, val in zip(bars, values):
        ax.text(
            bar.get_width() + max(values) * 0.015,
            bar.get_y() + bar.get_height() / 2,
            f"{val:.1f} {unit}",
            va="center",
            ha="left",
            fontsize=11,
            fontweight="bold",
            color=INK,
        )

    ax.set_yticks(list(y))
    ax.set_yticklabels(labels, fontsize=11, color=INK)
    ax.invert_yaxis()
    ax.set_xlim(0, max(values) * 1.22)
    style_axes(ax)
    ax.tick_params(axis="y", length=0, labelsize=11, colors=INK)

    longest_label = max(len(label) for label in labels)
    left_margin = min(0.45, 0.16 + 0.011 * longest_label)
    fig.subplots_adjust(left=left_margin, right=0.93, top=0.68, bottom=0.24)
    fig.text(0.06, 0.9, title, fontsize=16, fontweight="bold", color=INK)
    fig.text(0.06, 0.8, subtitle, fontsize=10, color=GRAY_TEXT)
    fig.text(0.06, 0.03, footer, fontsize=8.5, color=GRAY_TEXT, wrap=True)

    fig.savefig(outpath, dpi=200)
    plt.close(fig)


def grid_bar_chart(
    outpath: Path,
    panels: list[dict],
    series_labels: list[str],
    series_colors: list[str],
) -> None:
    n = len(panels)
    fig, axes = plt.subplots(1, n, figsize=(4.2 * n, 4.6))
    fig.patch.set_facecolor("white")
    if n == 1:
        axes = [axes]

    for ax, panel in zip(axes, panels):
        values = panel["values"]
        x = range(len(values))
        bars = ax.bar(list(x), values, color=series_colors, width=0.6, zorder=3)

        best_idx = panel.get("highlight_idx")
        if best_idx is not None:
            bars[best_idx].set_edgecolor(INK)
            bars[best_idx].set_linewidth(1.4)

        for bar, val in zip(bars, values):
            weight = (
                "bold"
                if bar is bars[best_idx]
                else "normal"
                if best_idx is not None
                else "normal"
            )
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + max(values) * 0.02,
                f"{val:.1f}",
                ha="center",
                va="bottom",
                fontsize=10,
                fontweight=weight,
                color=INK,
            )

        ax.set_ylim(0, max(values) * 1.25)
        ax.set_xticks([])
        style_axes(ax)
        ax.set_title(
            panel["title"],
            fontsize=12.5,
            fontweight="bold",
            color=INK,
            loc="left",
            pad=14,
        )
        ax.text(
            0.0,
            1.03,
            panel["subtitle"],
            transform=ax.transAxes,
            fontsize=9,
            color=GRAY_TEXT,
            ha="left",
        )

    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in series_colors]
    fig.legend(
        handles,
        series_labels,
        loc="lower center",
        ncol=len(series_labels),
        frameon=False,
        fontsize=9.5,
        bbox_to_anchor=(0.5, -0.02),
    )

    fig.subplots_adjust(top=0.82, bottom=0.16, wspace=0.35)
    fig.savefig(outpath, dpi=200, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args()

    rows = load_rows(args.report_dir / "summary.csv")

    # Chart A: single-slot generation speed, pop vs. cluster (mirrors the
    # "with/without MTP" style — same shape of claim, different hardware).
    pop = pick(rows, "ollama-pop-np1-default", 1)
    cluster = pick(rows, "ollama-cluster-vulkan-default", 1)
    horizontal_bar_chart(
        outpath=args.report_dir / "single-slot-speed.png",
        title="Single-slot generation speed",
        subtitle="tokens/s · higher is better",
        footer=(
            "Pop (M5 Max, Gemma 4 MLX) generates nearly 2x faster than the cluster\n"
            "(RX 9070 XT, Gemma 4 GGUF, Vulkan) at concurrency=1. gemma4:12b, mixed_mem0 workload."
        ),
        labels=["Pop (M5 Max)", "Cluster (RX 9070 XT, Vulkan)"],
        values=[float(pop["p50_gen_tps"]), float(cluster["p50_gen_tps"])],
        colors=[INK, PALETTE["gray_light"]],
        unit="tok/s",
    )

    # Chart B: peak-throughput comparison across the tested runs.
    runs = [
        ("Pop mixed_mem0 (np=1)", "ollama-pop-np1-default", 1),
        ("Cluster mixed_mem0 (np=8, Vulkan)", "ollama-cluster-vulkan-default", 8),
        ("Cluster agentic (np=8, Vulkan)", "ollama-cluster-vulkan-agentic", 8),
    ]
    labels = [r[0] for r in runs]
    picked = [pick(rows, r[1], r[2]) for r in runs]
    values = [float(p["aggregate_tok_s"]) for p in picked]
    # Throughput without usability is a tall bar for work that never happened:
    # a model can burn its whole budget reasoning and return nothing. Flag any
    # run whose answers were mostly unusable rather than plotting it bare.
    labels = [
        f"{lbl}  ⚠ {float(p['usable_rate']):.0%} usable"
        if "usable_rate" in p
        and p["usable_rate"] not in ("", None)
        and float(p["usable_rate"]) < USABLE_RATE_WARN
        else lbl
        for lbl, p in zip(labels, picked)
    ]
    colors = [PALETTE["green"], PALETTE["purple"], PALETTE["pink"]]
    horizontal_bar_chart(
        outpath=args.report_dir / "peak-throughput.png",
        title="Peak aggregate throughput",
        subtitle="tokens/s · higher is better · best concurrency level per run",
        footer=(
            "Default (short-context) workload keeps scaling to conc=8 on the cluster.\n"
            "Agentic (32K context) saturates earlier; pop's single slot is fastest per-request.\n"
            "Cluster runs on the Vulkan backend (production default since 2026-07-01)."
        ),
        labels=labels,
        values=values,
        colors=colors,
        unit="tok/s",
    )

    # Chart C: 3-panel grid at conc=8 (or each run's max), mirroring the
    # multi-benchmark grid layout.
    series = [
        (
            "Cluster agentic (np=8, Vulkan)",
            "ollama-cluster-vulkan-agentic",
            8,
            PALETTE["pink"],
        ),
        (
            "Cluster mixed_mem0 (np=8, Vulkan)",
            "ollama-cluster-vulkan-default",
            8,
            PALETTE["purple"],
        ),
        ("Pop mixed_mem0 (np=1)", "ollama-pop-np1-default", 1, PALETTE["green"]),
    ]
    series_rows = [pick(rows, run, conc) for _, run, conc, _ in series]
    series_labels = [label for label, *_ in series]
    series_colors = [color for *_, color in series]

    panels = [
        {
            "title": "Aggregate throughput",
            "subtitle": "tok/s · peak per run · higher is better",
            "values": [float(r["aggregate_tok_s"]) for r in series_rows],
            "highlight_idx": 1,
        },
        {
            "title": "Per-request speed",
            "subtitle": "tok/s · peak per run · higher is better",
            "values": [float(r["p50_gen_tps"]) for r in series_rows],
            "highlight_idx": 2,
        },
        {
            "title": "P95 time-to-first-token",
            "subtitle": "seconds · peak per run · lower is better",
            "values": [float(r["p95_ttft_s"]) for r in series_rows],
            "highlight_idx": 2,
        },
    ]
    grid_bar_chart(
        outpath=args.report_dir / "peak-comparison-grid.png",
        panels=panels,
        series_labels=series_labels,
        series_colors=series_colors,
    )

    print(f"Wrote {args.report_dir / 'single-slot-speed.png'}")
    print(f"Wrote {args.report_dir / 'peak-throughput.png'}")
    print(f"Wrote {args.report_dir / 'peak-comparison-grid.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
