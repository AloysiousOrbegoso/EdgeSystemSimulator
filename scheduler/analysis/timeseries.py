"""
Time-series plot regeneration from JSONL event logs.

For every (scenario, algorithm) pair, this module produces:
  - One memory-utilization-over-time line chart per node, per algorithm
  - One CPU-utilization-over-time line chart per node, per algorithm
  - One running-task distribution stacked-area chart per (scenario, algorithm)
  - One memory-utilization OVERLAY chart per scenario per node showing
    both algorithms side by side (the visual artifact for RQ3.2)
  - One CPU-utilization OVERLAY chart per scenario per node (RQ3.1)

Inputs
  results/runs.csv               — to enumerate (scenario, algorithm, run_id)
  results/events/<run_id>.jsonl  — sample events to plot

When multiple trials per (scenario, algorithm) exist, the line is the
mean across trials at each second-offset; the shaded band is min..max.
This is what the thesis defends — central tendency plus dispersion across
the 5-trial repetition.

Outputs go to:
  results/analysis/timeseries/
    <scenario>_<algorithm>_<node>_memory.png
    <scenario>_<algorithm>_<node>_cpu.png
    <scenario>_<algorithm>_distribution.png
    <scenario>_<node>_memory_overlay.png
    <scenario>_<node>_cpu_overlay.png

Matplotlib is the only plotting dependency. No interactive backend is
used — figures are saved straight to disk so this module runs equally
well from CLI, the dashboard, or a CI job.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import matplotlib

# Force a non-interactive backend so this works without a display
# (Docker, CI, headless servers).
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

logger = logging.getLogger(__name__)


# Common second-offset grid (1 Hz sampling × max 600s of trial)
GRID_SECONDS = np.arange(0.0, 600.0 + 1.0, 1.0)


# ---------------------------------------------------------------------------
# Per-trial parsing
# ---------------------------------------------------------------------------


@dataclass
class _TrialSeries:
    """Parsed time-series data for one trial."""

    run_id: str
    scenario: str
    algorithm: str
    # node_id -> list of (t_seconds, pct)
    memory_pct: dict[str, list[tuple[float, float]]]
    cpu_pct: dict[str, list[tuple[float, float]]]
    # list of (t_seconds, {node_id -> running_count})
    distribution: list[tuple[float, dict[str, int]]]


def _parse_trial_jsonl(jsonl_path: Path, scenario: str, algorithm: str, run_id: str) -> _TrialSeries:
    """Read one event log file and pull out the sample streams."""
    memory: dict[str, list[tuple[float, float]]] = defaultdict(list)
    cpu: dict[str, list[tuple[float, float]]] = defaultdict(list)
    distribution: list[tuple[float, dict[str, int]]] = []

    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = float(ev.get("t", 0.0))
            etype = ev.get("type")
            if etype == "memory_sample":
                node_id = ev.get("node_id")
                pct = float(ev.get("pct", 0.0))
                if node_id:
                    memory[node_id].append((t, pct))
            elif etype == "cpu_sample":
                node_id = ev.get("node_id")
                pct = float(ev.get("pct", 0.0))
                if node_id:
                    cpu[node_id].append((t, pct))
            elif etype == "distribution_sample":
                running = ev.get("running") or {}
                # Coerce values to int just in case.
                distribution.append(
                    (t, {str(k): int(v) for k, v in running.items()})
                )

    return _TrialSeries(
        run_id=run_id,
        scenario=scenario,
        algorithm=algorithm,
        memory_pct=dict(memory),
        cpu_pct=dict(cpu),
        distribution=distribution,
    )


# ---------------------------------------------------------------------------
# Across-trial aggregation
# ---------------------------------------------------------------------------


def _resample_to_grid(
    series: list[tuple[float, float]], grid: np.ndarray
) -> np.ndarray:
    """Linearly interpolate (t, value) pairs onto the second-offset grid.

    Returns an array of NaN for offsets beyond the last sample, so mean
    aggregation across trials of different lengths handles this correctly.
    """
    if not series:
        return np.full_like(grid, np.nan, dtype=float)
    ts = np.array([t for t, _ in series])
    vs = np.array([v for _, v in series])
    # Sort defensively.
    order = np.argsort(ts)
    ts = ts[order]
    vs = vs[order]
    out = np.interp(grid, ts, vs, left=vs[0], right=np.nan)
    # np.interp's `right=` only kicks in when grid > ts.max(); make that
    # NaN explicitly so trials of different length don't contaminate the
    # mean past their actual end.
    out = np.where(grid > ts.max(), np.nan, out)
    return out


def _aggregate_per_node_pct(
    trials: list[_TrialSeries],
    pick: str,
    grid: np.ndarray,
) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Aggregate memory_pct or cpu_pct across trials for each node.

    Returns node_id -> (mean, lo, hi) arrays. lo and hi are the per-grid
    nanmin / nanmax across trials, suitable for fill_between bands.
    """
    assert pick in {"memory_pct", "cpu_pct"}

    # node_id -> list of resampled arrays, one per trial
    by_node: dict[str, list[np.ndarray]] = defaultdict(list)
    for tr in trials:
        node_map = getattr(tr, pick)
        for node_id, samples in node_map.items():
            by_node[node_id].append(_resample_to_grid(samples, grid))

    result: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for node_id, arrays in by_node.items():
        stack = np.vstack(arrays)
        with np.errstate(all="ignore"):
            mean = np.nanmean(stack, axis=0)
            lo = np.nanmin(stack, axis=0)
            hi = np.nanmax(stack, axis=0)
        result[node_id] = (mean, lo, hi)
    return result


def _aggregate_distribution(
    trials: list[_TrialSeries], grid: np.ndarray
) -> dict[str, np.ndarray]:
    """Aggregate distribution samples across trials.

    Returns node_id -> array of running-task counts (mean across trials)
    on the standard grid. Used for stacked-area plots.
    """
    # Collect per-node series per trial.
    per_trial: list[dict[str, np.ndarray]] = []
    for tr in trials:
        if not tr.distribution:
            continue
        # Build per-node (t, count) from the trial.
        node_series: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for t, running in tr.distribution:
            for node_id, count in running.items():
                node_series[node_id].append((t, float(count)))
        # Resample each node onto the grid.
        per_trial.append(
            {nid: _resample_to_grid(s, grid) for nid, s in node_series.items()}
        )

    if not per_trial:
        return {}

    # Union of all nodes seen across trials.
    all_nodes: set[str] = set()
    for d in per_trial:
        all_nodes.update(d.keys())

    out: dict[str, np.ndarray] = {}
    for node_id in sorted(all_nodes):
        # Stack the trials' arrays for this node, treating missing trials
        # as NaN for clean nanmean.
        arrays: list[np.ndarray] = []
        for d in per_trial:
            arrays.append(d.get(node_id, np.full_like(grid, np.nan, dtype=float)))
        stack = np.vstack(arrays)
        with np.errstate(all="ignore"):
            out[node_id] = np.nanmean(stack, axis=0)
    return out


# ---------------------------------------------------------------------------
# Plot rendering
# ---------------------------------------------------------------------------


def _plot_pct_per_node(
    title: str,
    out_path: Path,
    grid: np.ndarray,
    per_node: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]],
    saturation_pct: float = 80.0,
) -> None:
    """Multi-line chart, one line per node, with min..max band per node."""
    fig, ax = plt.subplots(figsize=(10, 5))
    for node_id in sorted(per_node.keys()):
        mean, lo, hi = per_node[node_id]
        line = ax.plot(grid, mean, label=node_id)[0]
        ax.fill_between(grid, lo, hi, alpha=0.15, color=line.get_color())
    ax.axhline(saturation_pct, color="red", linestyle="--", linewidth=0.8,
               label=f"saturation ({saturation_pct:.0f}%)")
    ax.set_xlabel("seconds since trial start")
    ax.set_ylabel("utilization (%)")
    ax.set_ylim(0, 105)
    ax.set_title(title)
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def _plot_overlay(
    title: str,
    out_path: Path,
    grid: np.ndarray,
    per_algo: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]],
    saturation_pct: float = 80.0,
) -> None:
    """Two-line chart comparing the two algorithms on the same node."""
    fig, ax = plt.subplots(figsize=(10, 5))
    for algo in sorted(per_algo.keys()):
        mean, lo, hi = per_algo[algo]
        line = ax.plot(grid, mean, label=algo)[0]
        ax.fill_between(grid, lo, hi, alpha=0.18, color=line.get_color())
    ax.axhline(saturation_pct, color="red", linestyle="--", linewidth=0.8,
               label=f"saturation ({saturation_pct:.0f}%)")
    ax.set_xlabel("seconds since trial start")
    ax.set_ylabel("utilization (%)")
    ax.set_ylim(0, 105)
    ax.set_title(title)
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def _plot_distribution_stacked(
    title: str,
    out_path: Path,
    grid: np.ndarray,
    per_node: dict[str, np.ndarray],
) -> None:
    """Stacked-area chart of running-task counts per node over time."""
    if not per_node:
        return
    fig, ax = plt.subplots(figsize=(10, 5))
    node_ids = sorted(per_node.keys())
    arrays = [np.nan_to_num(per_node[nid], nan=0.0) for nid in node_ids]
    ax.stackplot(grid, arrays, labels=node_ids, alpha=0.85)
    ax.set_xlabel("seconds since trial start")
    ax.set_ylabel("running tasks")
    ax.set_title(title)
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def regenerate_plots(
    results_dir: Path | str = "results",
    out_dir: Path | str | None = None,
    grid: np.ndarray | None = None,
) -> dict[str, list[Path]]:
    """Read runs.csv and per-trial JSONL logs, write all PNGs.

    Returns a dict {plot_kind: [paths]} so the caller can list what was
    produced. Plot kinds: "memory_per_algo", "cpu_per_algo",
    "distribution_per_algo", "memory_overlay", "cpu_overlay".
    """
    results_dir = Path(results_dir)
    out_root = Path(out_dir) if out_dir else results_dir / "analysis" / "timeseries"
    grid = grid if grid is not None else GRID_SECONDS

    runs_csv = results_dir / "runs.csv"
    if not runs_csv.exists():
        logger.warning("runs.csv not found at %s; nothing to plot", runs_csv)
        return {}

    df = pd.read_csv(runs_csv)
    if df.empty:
        return {}

    # Group trials by (scenario, algorithm).
    grouped: dict[tuple[str, str], list[_TrialSeries]] = defaultdict(list)
    for _, row in df.iterrows():
        run_id = str(row["run_id"])
        scenario = str(row["scenario_name"])
        algorithm = str(row["algorithm"])
        jsonl_path = results_dir / "events" / f"{run_id}.jsonl"
        if not jsonl_path.exists():
            logger.info("Skipping run %s: event log missing", run_id)
            continue
        try:
            series = _parse_trial_jsonl(jsonl_path, scenario, algorithm, run_id)
        except Exception:
            logger.exception("Failed to parse %s; skipping", jsonl_path)
            continue
        grouped[(scenario, algorithm)].append(series)

    produced: dict[str, list[Path]] = defaultdict(list)

    # Per-(scenario, algorithm) plots.
    for (scenario, algorithm), trials in grouped.items():
        mem_per_node = _aggregate_per_node_pct(trials, "memory_pct", grid)
        cpu_per_node = _aggregate_per_node_pct(trials, "cpu_pct", grid)
        dist_per_node = _aggregate_distribution(trials, grid)

        if mem_per_node:
            p = out_root / f"{scenario}_{algorithm}_memory.png"
            _plot_pct_per_node(
                title=f"{scenario} / {algorithm} — memory utilization",
                out_path=p,
                grid=grid,
                per_node=mem_per_node,
            )
            produced["memory_per_algo"].append(p)

        if cpu_per_node:
            p = out_root / f"{scenario}_{algorithm}_cpu.png"
            _plot_pct_per_node(
                title=f"{scenario} / {algorithm} — CPU utilization",
                out_path=p,
                grid=grid,
                per_node=cpu_per_node,
            )
            produced["cpu_per_algo"].append(p)

        if dist_per_node:
            p = out_root / f"{scenario}_{algorithm}_distribution.png"
            _plot_distribution_stacked(
                title=f"{scenario} / {algorithm} — running tasks per node",
                out_path=p,
                grid=grid,
                per_node=dist_per_node,
            )
            produced["distribution_per_algo"].append(p)

    # Per-scenario overlay plots: one per node, both algorithms together.
    by_scenario: dict[str, list[tuple[str, list[_TrialSeries]]]] = defaultdict(list)
    for (scenario, algorithm), trials in grouped.items():
        by_scenario[scenario].append((algorithm, trials))

    for scenario, algos in by_scenario.items():
        if len(algos) < 2:
            continue  # nothing to overlay
        # Build per-node, per-algorithm aggregates.
        all_nodes: set[str] = set()
        algo_to_mem: dict[str, dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]] = {}
        algo_to_cpu: dict[str, dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]] = {}
        for algorithm, trials in algos:
            algo_to_mem[algorithm] = _aggregate_per_node_pct(trials, "memory_pct", grid)
            algo_to_cpu[algorithm] = _aggregate_per_node_pct(trials, "cpu_pct", grid)
            all_nodes.update(algo_to_mem[algorithm].keys())
            all_nodes.update(algo_to_cpu[algorithm].keys())

        for node_id in sorted(all_nodes):
            mem_overlay = {
                a: algo_to_mem[a][node_id]
                for a in algo_to_mem
                if node_id in algo_to_mem[a]
            }
            cpu_overlay = {
                a: algo_to_cpu[a][node_id]
                for a in algo_to_cpu
                if node_id in algo_to_cpu[a]
            }
            if len(mem_overlay) >= 2:
                p = out_root / f"{scenario}_{node_id}_memory_overlay.png"
                _plot_overlay(
                    title=f"{scenario} / {node_id} — memory: algorithms compared",
                    out_path=p,
                    grid=grid,
                    per_algo=mem_overlay,
                )
                produced["memory_overlay"].append(p)
            if len(cpu_overlay) >= 2:
                p = out_root / f"{scenario}_{node_id}_cpu_overlay.png"
                _plot_overlay(
                    title=f"{scenario} / {node_id} — CPU: algorithms compared",
                    out_path=p,
                    grid=grid,
                    per_algo=cpu_overlay,
                )
                produced["cpu_overlay"].append(p)

    total = sum(len(v) for v in produced.values())
    logger.info("Regenerated %d plots under %s", total, out_root)
    return dict(produced)