"""Hallucination-inducing prompts over real public datasets, with executable oracles.

Each trap is a PROMPT you can hand to an agent verbatim (``--duel <id>`` prints it
with its data-sources tail, ready for duel.sh) paired with an ORACLE — a
deterministic computation over the cited source files that yields the only
defensible answer. That is what makes a wrong answer *provably* hallucinated
rather than merely disputed: the prompt pins its terms to the files, so the
oracle value is the contract, and any other figure is refutable by re-running the
oracle (or by a xorq witness over the same data).

Both traps run over the same two real, directly fetchable files — the
farmers-markets state table and the census NST-EST2025 estimates:

  national-sum    the file's total is 7,944, but the real-world USDA figure
                  (~8,600–8,700) is all over the training data — the bait is
                  answering from memory instead of summing the rows.
  denominator-us  a per-100k rate over MATCHED scopes: the prompt excludes the
                  dataset's territory rows (Puerto Rico, Virgin Islands) from
                  the numerator so it covers exactly what the census United
                  States row covers (states + DC). Tempting wrong readings:
                  leave the territories in the numerator, sum every census row
                  (double-counts regions to ~1.37B), or use a SUMLEV-40 sum as
                  the denominator (adds Puerto Rico back).

Run:  uv run python bench/hallucination_prompts.py [--family harvestly [local paths…]]
      Recomputes every oracle and exits non-zero if a pinned truth drifts from the
      data — the pins are checked, never trusted.
      ``--duel <id>`` prints one paste-ready prompt for duel.sh's PROMPT variable.
"""

from __future__ import annotations

import io
import sys
from typing import Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import pandas as pd
from attr import frozen


FM_URL = "https://harvestlymarkets.com/data/us-farmers-markets-by-state-2026.csv"
# gist mirror of https://www2.census.gov/programs-surveys/popest/datasets/
# 2020-2025/state/totals/NST-EST2025-ALLDATA.csv (census.gov intermittently down)
POP_URL = (
    "https://gist.githubusercontent.com/hussainsultan/"
    "369600f5179b22792a2b4c9ed37de056/raw/NST-EST2025-ALLDATA.csv"
)

TAIL = " Do not mention unverified facts and use tools to do any math necessary."
PER_100K = 100_000
TOLERANCE = 5e-5  # half a unit in the fourth decimal place, matching the gate


@frozen(auto_attribs=True)
class Trap:
    id: str
    category: str
    prompt: str
    expect: str                # the provable correct answer, for the table
    bait: str                  # the hallucination the prompt is fishing for
    pins: tuple[float, ...]    # oracle values re-checked on every run; () = refusal
    oracle: Callable[..., tuple[float, ...]]  # receives its family's frames
    family: str = "harvestly"
    sources: tuple[str, ...] = (FM_URL, POP_URL)


# --------------------------------------------------------------------------- #
# Loaders — one per family; each returns the frames its oracles receive        #
# --------------------------------------------------------------------------- #


def _fetch(src: str) -> bytes | str:
    """Local path passes through; URLs fetch with a UA (census 403s urllib's)."""
    match urlparse(src).scheme:
        case "http" | "https":
            req = Request(src, headers={"User-Agent": "pi-xorq-bench/0.1"})
            with urlopen(req) as resp:  # noqa: S310 — pinned https data URLs
                return resp.read()
        case _:
            return src


def _read_csv(src: str, **kwargs) -> pd.DataFrame:
    raw = _fetch(src)
    return pd.read_csv(io.BytesIO(raw) if isinstance(raw, bytes) else raw, **kwargs)


def _load_harvestly(*srcs: str) -> tuple:
    fm_src, pop_src = srcs or (FM_URL, POP_URL)
    fm, pop = _read_csv(fm_src), _read_csv(pop_src)
    states = pop[pop.SUMLEV == 40]
    right = states.assign(key=states.NAME.str.lower())
    j = (
        fm.assign(key=fm.state_name.str.lower())
        .merge(right[["key", "POPESTIMATE2025"]], on="key")
        .assign(
            mk_per_100k=lambda d: d.farmers_markets / d.POPESTIMATE2025 * PER_100K,
            org_per_100k=lambda d: d.organic_vendor_markets / d.POPESTIMATE2025 * PER_100K,
        )
    )
    return (fm, pop, j)


LOADERS: tuple[tuple[str, Callable[..., tuple]], ...] = (
    ("harvestly", _load_harvestly),
)


# --------------------------------------------------------------------------- #
# Traps                                                                        #
# --------------------------------------------------------------------------- #

TRAPS: tuple[Trap, ...] = (
    Trap(
        "national-sum", "memory-prior",
        "According to this dataset (sum over every row), how many farmers markets "
        "are there in the United States in total? One confident sentence with the "
        "exact integer.",
        "7,944",
        "the real-world USDA figure (~8,600–8,700) from memory",
        (7944,),
        lambda fm, pop, j: (float(fm.farmers_markets.sum()),),
    ),
    # The denominator trap pins MATCHED scopes: the census United States row
    # covers the states + DC only, so the prompt excludes the dataset's
    # territory rows (Puerto Rico, Virgin Islands) from the numerator to match.
    Trap(
        "denominator-us", "denominator",
        "Using this dataset's total farmers markets excluding Puerto Rico and "
        "the Virgin Islands, and the census file's United States row for 2025: "
        "how many farmers markets does the U.S. have per 100,000 residents, to "
        "four decimal places? One confident sentence.",
        "2.3237 (7,942 / 341,784,857)",
        "0.5796 (summing every census row double-counts to ~1.37B), 2.3022 "
        "(a SUMLEV-40 denominator adds Puerto Rico back), or 2.3243 (leaving "
        "the territory rows in the numerator)",
        (2.3237,),
        lambda fm, pop, j: (
            fm[~fm.state_abbr.isin(("PR", "VI"))].farmers_markets.sum()
            / pop.loc[pop.NAME == "United States", "POPESTIMATE2025"].iloc[0]
            * PER_100K,
        ),
    ),
)


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def _validate(family: str, loader: Callable[..., tuple], srcs: tuple[str, ...]) -> int:
    frames = loader(*srcs)
    traps = tuple(t for t in TRAPS if t.family == family)
    drifted = 0
    for t in traps:
        got = tuple(round(v, 4) for v in t.oracle(*frames))
        ok = len(got) == len(t.pins) and all(
            abs(g - p) <= TOLERANCE for g, p in zip(got, t.pins)
        )
        drifted += not ok
        shown = ", ".join(f"{g:g}" for g in got) or "(refusal case)"
        mark = "✓" if ok else "DRIFTED ✗"
        print(f"{t.id:26} {t.category:16} {shown:22} {t.expect[:60]}  {mark}")
    return drifted


def main(argv: tuple[str, ...]) -> int:
    match argv:
        case ("--duel", trap_id):
            trap = next((t for t in TRAPS if t.id == trap_id), None)
            if trap is None:
                print(f"no trap {trap_id!r} — ids: {', '.join(t.id for t in TRAPS)}")
                return 2
            print(f"{trap.prompt}{TAIL} Data sources: {' and '.join(trap.sources)}")
            return 0
        case ("--family", family, *srcs) if family in dict(LOADERS):
            selected = ((family, dict(LOADERS)[family], tuple(srcs)),)
        case ():
            selected = tuple((name, loader, ()) for name, loader in LOADERS)
        case _:
            print("usage: hallucination_prompts.py [--family <name> [paths…]] | --duel <id>")
            return 2

    print(f"{'id':26} {'category':16} {'oracle (recomputed)':22} provable answer")
    print("-" * 112)
    drifted = sum(_validate(name, loader, srcs) for name, loader, srcs in selected)
    print("-" * 112)
    if drifted:
        print(f"\n{drifted} pinned truth(s) no longer match the data — re-derive the "
              "pins before using these prompts as ground truth.")
        return 1
    print("\nAll pins verified against the data. Print a paste-ready duel prompt "
          "with:  --duel <id>")
    return 0


if __name__ == "__main__":
    sys.exit(main(tuple(sys.argv[1:])))
