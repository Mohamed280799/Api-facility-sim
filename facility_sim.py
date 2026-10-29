"""
API facility capacity & utility simulation (discrete-event, SimPy).

A small upstream/downstream production area for a biologic API:

    bioreactor:  fermentation -> harvest (needs a free purification line) -> CIP -> SIP
    purification line:  purification -> CIP
    CIP skids:  shared by all equipment; every cleaning draws WFI (water for injection)
    SIP:  steam-in-place of the bioreactor before the next batch; draws clean steam

The model answers typical early-design questions:
    * How many batches per year can this layout deliver?
    * Which equipment is the bottleneck?
    * What peak WFI and steam demand must the utility systems be sized for?
    * What is the effect of adding a reactor, a purification line or a CIP skid,
      or of staggering batch starts?

All durations and utility figures are illustrative, not data from a real facility.

Usage:
    python facility_sim.py                 # run all scenarios, write results/ and figures
    python facility_sim.py --reps 20       # more replications per scenario
"""
import argparse
import os
from dataclasses import dataclass, replace

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import simpy

HOURS_PER_YEAR = 365 * 24
WARMUP_H = 30 * 24          # ignore the first 30 days (empty-plant start-up)
RESULTS = "results"


# ----------------------------------------------------------------- parameters
@dataclass(frozen=True)
class Scenario:
    name: str
    reactors: int = 3
    purification_lines: int = 1
    cip_skids: int = 1
    min_start_gap_h: float = 0.0      # minimum time between two batch starts (staggering)
    # durations [h] as (min, mode, max) for a triangular distribution
    fermentation: tuple = (150, 168, 190)
    harvest: tuple = (6, 8, 10)
    purification: tuple = (44, 50, 60)
    cip_reactor: tuple = (4, 5, 6)
    cip_line: tuple = (3, 4, 5)
    sip: tuple = (3, 4, 5)
    # utilities
    wfi_cip_reactor_m3: float = 12.0  # WFI used by one reactor CIP
    wfi_cip_line_m3: float = 6.0      # WFI used by one purification-line CIP
    steam_sip_kg_h: float = 250.0     # clean steam during SIP


SCENARIOS = [
    Scenario("Base: 3 reactors, 1 line, 1 CIP"),
    Scenario("+1 reactor (4 reactors)", reactors=4),
    Scenario("+1 purification line", purification_lines=2),
    Scenario("+1 line and +1 reactor", reactors=4, purification_lines=2),
    Scenario("+1 line, +1 reactor, +1 CIP", reactors=4, purification_lines=2, cip_skids=2),
    Scenario("+1 line, +1 reactor, +1 CIP, staggered 24 h", reactors=4, purification_lines=2,
             cip_skids=2, min_start_gap_h=24),
]


def tri(rng, p):
    lo, mode, hi = p
    return rng.triangular(lo, mode, hi)


# ----------------------------------------------------------------- model
class Facility:
    def __init__(self, env, sc: Scenario, rng):
        self.env, self.sc, self.rng = env, sc, rng
        self.lines = simpy.Store(env)              # free purification lines, by id
        for i in range(1, sc.purification_lines + 1):
            self.lines.put(i)
        self.cip = simpy.Resource(env, sc.cip_skids)
        self.start_gate = simpy.Resource(env, 1)   # enforces the minimum start gap
        self.last_start = -1e9
        self.log = []          # (unit, activity, start, end)
        self.utility = []      # (utility, start, end, rate per hour)
        self.completed = []    # batch completion times

    def record(self, unit, activity, start, end):
        if end > start:
            self.log.append((unit, activity, start, end))

    def cip_clean(self, unit, duration_p, wfi_m3):
        t_wait = self.env.now
        with self.cip.request() as req:
            yield req
            self.record(unit, "wait CIP", t_wait, self.env.now)
            t0 = self.env.now
            yield self.env.timeout(tri(self.rng, duration_p))
            self.record(unit, "CIP", t0, self.env.now)
            self.utility.append(("WFI", t0, self.env.now, wfi_m3 / (self.env.now - t0)))

    def reactor(self, rid):
        unit = f"Reactor {rid}"
        while True:
            # staggering: keep a minimum gap between batch starts across all reactors
            with self.start_gate.request() as gate:
                yield gate
                gap = self.sc.min_start_gap_h - (self.env.now - self.last_start)
                if gap > 0:
                    t_wait = self.env.now
                    yield self.env.timeout(gap)
                    self.record(unit, "wait start", t_wait, self.env.now)
                self.last_start = self.env.now

            t0 = self.env.now
            yield self.env.timeout(tri(self.rng, self.sc.fermentation))
            self.record(unit, "Fermentation", t0, self.env.now)

            # harvest needs a free purification line: the reactor is blocked until one is free
            t_wait = self.env.now
            line_id = yield self.lines.get()
            self.record(unit, "blocked (no line)", t_wait, self.env.now)
            t0 = self.env.now
            yield self.env.timeout(tri(self.rng, self.sc.harvest))
            self.record(unit, "Harvest", t0, self.env.now)
            self.env.process(self.purify(line_id))

            yield from self.cip_clean(unit, self.sc.cip_reactor, self.sc.wfi_cip_reactor_m3)
            t0 = self.env.now
            yield self.env.timeout(tri(self.rng, self.sc.sip))
            self.record(unit, "SIP", t0, self.env.now)
            self.utility.append(("Steam", t0, self.env.now, self.sc.steam_sip_kg_h))

    def purify(self, line_id):
        unit = f"Purification {line_id}"
        t0 = self.env.now
        yield self.env.timeout(tri(self.rng, self.sc.purification))
        self.record(unit, "Purification", t0, self.env.now)
        self.completed.append(self.env.now)
        yield from self.cip_clean(unit, self.sc.cip_line, self.sc.wfi_cip_line_m3)
        yield self.lines.put(line_id)


def run_once(sc: Scenario, seed: int):
    env = simpy.Environment()
    rng = np.random.default_rng(seed)
    fac = Facility(env, sc, rng)
    for rid in range(1, sc.reactors + 1):
        env.process(fac.reactor(rid))
    env.run(until=WARMUP_H + HOURS_PER_YEAR)
    return fac


# ----------------------------------------------------------------- analysis
def clip(start, end, lo=WARMUP_H, hi=WARMUP_H + HOURS_PER_YEAR):
    return max(0.0, min(end, hi) - max(start, lo))


def utility_profile(fac, utility):
    """Hourly demand over the measured year (m3/h for WFI, kg/h for steam)."""
    prof = np.zeros(HOURS_PER_YEAR)
    for u, s, e, rate in fac.utility:
        if u != utility:
            continue
        for h in range(int(max(s, WARMUP_H)), int(min(np.ceil(e), WARMUP_H + HOURS_PER_YEAR))):
            overlap = clip(s, e, h, h + 1)
            prof[h - WARMUP_H] += rate * overlap
    return prof


def summarise(fac, sc):
    log = pd.DataFrame(fac.log, columns=["unit", "activity", "start", "end"])
    log["hours"] = [clip(s, e) for s, e in zip(log.start, log.end)]
    productive = log[~log.activity.str.startswith(("wait", "blocked"))]
    util = productive.groupby("unit").hours.sum() / HOURS_PER_YEAR
    reactors = util[util.index.str.startswith("Reactor")]
    lines = util[util.index.str.startswith("Purification")]
    blocked = log[log.activity == "blocked (no line)"].hours.sum() / (HOURS_PER_YEAR * sc.reactors)
    cip_busy = log[log.activity == "CIP"].hours.sum() / (HOURS_PER_YEAR * sc.cip_skids)
    wfi = utility_profile(fac, "WFI")
    steam = utility_profile(fac, "Steam")
    done = [t for t in fac.completed if WARMUP_H <= t < WARMUP_H + HOURS_PER_YEAR]
    return {
        "batches_per_year": len(done),
        "reactor_util": reactors.mean(),
        "line_util": lines.mean(),
        "cip_util": cip_busy,
        "reactor_blocked": blocked,
        "peak_wfi_m3_h": wfi.max(),
        "wfi_m3_year": wfi.sum(),
        "peak_steam_kg_h": steam.max(),
    }


def bottleneck(row):
    cands = {"Bioreactors": row.reactor_util, "Purification": row.line_util, "CIP skids": row.cip_util}
    return max(cands, key=cands.get)


# ----------------------------------------------------------------- figures
COLORS = {"Fermentation": "#2f6db3", "Harvest": "#7fb3e6", "Purification": "#2e9e6a",
          "CIP": "#e39a2d", "SIP": "#c2504a", "blocked (no line)": "#9a9a9a",
          "wait CIP": "#d9d9d9", "wait start": "#ececec"}


def gantt(fac, title, path, days=60):
    lo, hi = WARMUP_H, WARMUP_H + days * 24
    units = sorted({u for u, *_ in fac.log}, key=lambda u: (not u.startswith("Reactor"), u))
    fig, ax = plt.subplots(figsize=(12, 0.55 * len(units) + 1.5))
    for i, unit in enumerate(units):
        for u, act, s, e in fac.log:
            if u == unit and e > lo and s < hi:
                ax.barh(i, (min(e, hi) - max(s, lo)) / 24, left=(max(s, lo) - lo) / 24,
                        color=COLORS.get(act, "#bbb"), edgecolor="white", linewidth=0.4)
    ax.set_yticks(range(len(units)), units)
    ax.invert_yaxis()
    ax.set_xlabel("Day (after 30-day warm-up)")
    ax.set_title(title)
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in COLORS.values()]
    ax.legend(handles, COLORS.keys(), ncol=4, fontsize=8, loc="upper center",
              bbox_to_anchor=(0.5, -0.18), frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def wfi_plot(profiles, path, days=60):
    fig, ax = plt.subplots(figsize=(12, 3.6))
    t = np.arange(days * 24) / 24
    for (label, prof), c in zip(profiles, ["#2f6db3", "#e39a2d"]):
        ax.plot(t, prof[: days * 24], lw=1, color=c, alpha=0.85, label=f"{label} (peak {prof.max():.1f} m³/h)")
    ax.set_xlabel("Day")
    ax.set_ylabel("WFI demand [m³/h]")
    ax.set_title("WFI demand, 4 reactors + 2 lines: a second CIP skid lets cleanings overlap")
    ax.legend(fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def scenario_plot(df, path):
    fig, ax = plt.subplots(figsize=(10, 4))
    y = np.arange(len(df))
    ax.barh(y, df.batches_mean, xerr=df.batches_sd, color="#2f6db3", capsize=3)
    ax.set_yticks(y, df.scenario)
    ax.invert_yaxis()
    ax.set_xlabel("Batches per year (mean ± 1 sd over replications)")
    ax.set_title("Throughput by layout")
    for yi, v in zip(y, df.batches_mean):
        ax.text(v + 4, yi, f"{v:.1f}", va="center", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="API facility capacity & utility simulation")
    ap.add_argument("--reps", type=int, default=10, help="replications per scenario (default 10)")
    args = ap.parse_args()
    os.makedirs(RESULTS, exist_ok=True)

    rows = []
    for sc in SCENARIOS:
        runs = pd.DataFrame([summarise(run_once(sc, seed), sc) for seed in range(args.reps)])
        m = runs.mean()
        rows.append({
            "scenario": sc.name,
            "batches_mean": m.batches_per_year, "batches_sd": runs.batches_per_year.std(),
            "reactor_util": m.reactor_util, "line_util": m.line_util, "cip_util": m.cip_util,
            "reactor_blocked": m.reactor_blocked,
            "peak_wfi_m3_h": m.peak_wfi_m3_h, "wfi_m3_year": m.wfi_m3_year,
            "peak_steam_kg_h": m.peak_steam_kg_h,
        })
    df = pd.DataFrame(rows)
    df["bottleneck"] = df.apply(bottleneck, axis=1)
    df.round(3).to_csv(f"{RESULTS}/scenario_results.csv", index=False)

    pd.set_option("display.width", 200)
    show = df.copy()
    for c in ["reactor_util", "line_util", "cip_util", "reactor_blocked"]:
        show[c] = (show[c] * 100).round(0).astype(int).astype(str) + " %"
    show["batches"] = show.batches_mean.round(1).astype(str) + " ± " + show.batches_sd.round(1).astype(str)
    print(show[["scenario", "batches", "reactor_util", "reactor_blocked", "line_util", "cip_util",
                "bottleneck", "peak_wfi_m3_h", "peak_steam_kg_h"]].round(1).to_string(index=False))

    four_r, best, two_cip = SCENARIOS[1], SCENARIOS[3], SCENARIOS[4]
    gantt(run_once(four_r, 0), "4 reactors, 1 purification line: reactors blocked (grey) waiting for the line",
          f"{RESULTS}/gantt_4_reactors_1_line.png")
    gantt(run_once(best, 0), "4 reactors + 2 purification lines: blocking removed",
          f"{RESULTS}/gantt_4_reactors_2_lines.png")
    wfi_plot([("1 CIP skid", utility_profile(run_once(best, 0), "WFI")),
              ("2 CIP skids", utility_profile(run_once(two_cip, 0), "WFI"))],
             f"{RESULTS}/wfi_demand.png")
    scenario_plot(df, f"{RESULTS}/throughput_by_scenario.png")
    print(f"\nResults and figures written to ./{RESULTS}/")


if __name__ == "__main__":
    main()
