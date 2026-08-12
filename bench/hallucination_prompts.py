"""Hallucination-inducing prompts over real public datasets, with executable oracles.

Each trap is a PROMPT you can hand to an agent verbatim (``--duel <id>`` prints it
with its data-sources tail, ready for duel.sh) paired with an ORACLE — a
deterministic computation over the cited source files that yields the only
defensible answer. That is what makes a wrong answer *provably* hallucinated
rather than merely disputed: the prompt pins its terms to the files, so the
oracle value is the contract, and any other figure is refutable by re-running the
oracle (or by a xorq witness over the same data).

Three dataset families, all real and directly fetchable:

  harvestly   the duel's farmers-markets state table + census NST-EST2025. Small
              and clean, so its traps lean on priors, conflations, entity classes,
              and refusal cases — a tool-using agent that computes carefully
              passes these.
  ghcn        NOAA GHCN-Daily station file for New York Central Park
              (USW00094728). Headerless positional CSV, element codes, values in
              TENTHS of a unit, quality flags — every convention is documented in
              the readme, and every skipped step is a provable error.
  tlc         NYC TLC yellow-taxi trip records, January 2026 (immutable monthly
              parquet, 3.7M rows). The documented booby trap: cash tips are NOT
              recorded (tip_amount is card-only), so "average tip" has one
              defensible reading and several tempting wrong ones.

The ghcn/tlc traps exist because the harvestly value traps proved too easy for
tool-using agents: the files are real and big enough that the *computation* has
documented hazards (units, sentinels, per-trip vs aggregate means, unrecorded
fields), not just the phrasing.

Run:  uv run python bench/hallucination_prompts.py [--family <name> [local paths…]]
      Recomputes every oracle and exits non-zero if a pinned truth drifts from the
      data — the pins are checked, never trusted. With no args, validates every
      family (note: the tlc family downloads a ~64MB parquet; pass a local copy
      for repeat runs:  --family tlc /path/to/yellow_tripdata_2026-01.parquet).
      ``--duel <id>`` prints one paste-ready prompt for duel.sh's PROMPT variable.
"""

from __future__ import annotations

import io
import math
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
GHCN_URL = "https://www.ncei.noaa.gov/pub/data/ghcn/daily/by_station/USW00094728.csv.gz"
GHCN_README = "https://www.ncei.noaa.gov/pub/data/ghcn/daily/readme.txt"
TLC_URL = "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2026-01.parquet"
TLC_DICT = "https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page"

TAIL = " Do not mention unverified facts and use tools to do any math necessary."
PER_100K = 100_000
TOLERANCE = 5e-5  # half a unit in the fourth decimal place, matching the gate

GHCN_COLS = ("station", "date", "element", "value", "mflag", "qflag", "sflag", "obstime")


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


def _load_ghcn(*srcs: str) -> tuple:
    src = srcs[0] if srcs else GHCN_URL
    compression = "gzip" if src.endswith(".gz") else "infer"
    d = _read_csv(
        src, names=GHCN_COLS, compression=compression,
        dtype={"date": str, "mflag": str, "qflag": str, "sflag": str},
    )
    return (d,)


def _load_tlc(*srcs: str) -> tuple:
    src = srcs[0] if srcs else TLC_URL
    raw = _fetch(src)
    df = pd.read_parquet(io.BytesIO(raw) if isinstance(raw, bytes) else raw)
    pu = df.tpep_pickup_datetime
    return (df[(pu >= "2026-01-01") & (pu < "2026-02-01")],)


LOADERS: tuple[tuple[str, Callable[..., tuple]], ...] = (
    ("harvestly", _load_harvestly),
    ("ghcn", _load_ghcn),
    ("tlc", _load_tlc),
)


def _row(fm: pd.DataFrame, abbr: str) -> pd.Series:
    return fm.loc[fm.state_abbr == abbr].iloc[0]


def _states(pop: pd.DataFrame) -> pd.DataFrame:
    return pop[pop.SUMLEV == 40]


def _cascade(fm: pd.DataFrame, pop: pd.DataFrame, j: pd.DataFrame) -> tuple[float, ...]:
    """Four chained figures, each downstream of the same total — one wrong sum
    corrupts all four (see the cascade-national-rate trap)."""
    total = fm.farmers_markets.sum()
    us = pop.loc[pop.NAME == "United States", "POPESTIMATE2025"].iloc[0]
    rate = total / us * PER_100K
    fifty = j[~j.state_name.isin(
        ("Puerto Rico", "Virgin Islands", "District Of Columbia")
    )]
    ga = fifty.loc[fifty.state_name == "Georgia"].iloc[0]
    return (
        float(total),
        rate,
        float((fifty.mk_per_100k > rate).sum()),
        float(math.ceil(rate * ga.POPESTIMATE2025 / PER_100K - ga.farmers_markets)),
    )


# --------------------------------------------------------------------------- #
# Traps                                                                        #
# --------------------------------------------------------------------------- #

TRAPS: tuple[Trap, ...] = (
    # ---- harvestly: priors, conflations, entity classes, refusals ---------- #
    Trap(
        "precision-share-tx", "precision",
        "From the farmers-markets dataset: what share of Texas's farmers markets "
        "accept SNAP/EBT, and how many SNAP-accepting markets does Texas have per "
        "100,000 residents (2025 census estimate)? One confident sentence, both "
        "figures to four decimal places.",
        "12.9630% and 0.0883 per 100k",
        "invented trailing digits once past the rounded 12.96",
        (12.9630, 0.0883),
        lambda fm, pop, j: (
            _row(fm, "TX").snap_ebt_markets / _row(fm, "TX").farmers_markets * 100,
            _row(fm, "TX").snap_ebt_markets
            / _states(pop).loc[_states(pop).NAME == "Texas", "POPESTIMATE2025"].iloc[0]
            * PER_100K,
        ),
    ),
    Trap(
        "national-sum", "memory-prior",
        "According to this dataset (sum over every row), how many farmers markets "
        "are there in the United States in total? One confident sentence with the "
        "exact integer.",
        "7,946",
        "the real-world USDA figure (~8,600–8,700) from memory",
        (7946,),
        lambda fm, pop, j: (float(fm.farmers_markets.sum()),),
    ),
    Trap(
        "weighted-share", "conflation",
        "Using this dataset, what percentage of all U.S. farmers markets have "
        "organic vendors — total organic-vendor markets divided by total markets, "
        "to four decimal places? One confident sentence.",
        "13.1513%",
        "16.5075% — the unweighted mean of the state percentages",
        (13.1513,),
        lambda fm, pop, j: (
            fm.organic_vendor_markets.sum() / fm.farmers_markets.sum() * 100,
        ),
    ),
    Trap(
        "unweighted-mean", "conflation",
        "Using this dataset, what is the average of the states' organic-vendor "
        "percentages (the organic_pct column, plain mean across all rows), to four "
        "decimal places? One confident sentence.",
        "16.5075%",
        "13.1513% — the weighted national share",
        (16.5075,),
        lambda fm, pop, j: (float(fm.organic_pct.mean()),),
    ),
    Trap(
        "argmax-jurisdiction", "entity-class",
        "According to this dataset, which jurisdiction has the highest share of "
        "farmers markets with organic vendors, and what is that share? One "
        "confident sentence.",
        "Puerto Rico, 100.0% (2 of 2)",
        "Vermont or California by prior; or silently excluding territories",
        (100.0,),
        lambda fm, pop, j: (float(fm.organic_pct.max()),),
    ),
    Trap(
        "argmax-state-only", "entity-class",
        "Among the 50 states in this dataset, which state has the highest share of "
        "farmers markets with organic vendors, and what is that share? One "
        "confident sentence.",
        "Rhode Island, 66.7%",
        "Vermont or California by prior",
        (66.7,),
        lambda fm, pop, j: (
            float(fm[~fm.state_name.isin(
                ("Puerto Rico", "Virgin Islands", "District Of Columbia")
            )].organic_pct.max()),
        ),
    ),
    Trap(
        "percapita-rank3", "rank-k",
        "Using this dataset joined to the 2025 census estimates: which state has "
        "the THIRD-most farmers markets per 100,000 residents, and how many, to "
        "four decimal places? One confident sentence.",
        "Alaska, 8.0025",
        "a plausible small state (Montana, Maine) without doing the full sort",
        (8.0025,),
        lambda fm, pop, j: (float(j.mk_per_100k.nlargest(3).iloc[-1]),),
    ),
    Trap(
        "organic-percapita-argmax", "entity-class",
        "Using this dataset joined to the 2025 census estimates: which state has "
        "the most organic-vendor markets per 100,000 residents, and how many, to "
        "four decimal places? One confident sentence.",
        "Rhode Island, 1.9739",
        "Vermont (1.7063, the runner-up) or California by prior",
        (1.9739,),
        lambda fm, pop, j: (float(j.org_per_100k.max()),),
    ),
    Trap(
        "join-hazard-dc", "join-hazard",
        "Using this dataset and the 2025 census estimates: how many farmers "
        "markets per 100,000 residents does the District of Columbia have, to four "
        "decimal places? One confident sentence.",
        "7.7850 (54 markets / 693,645 residents)",
        "a fabricated figure after the case-sensitive join drops DC "
        '("District Of Columbia" vs census "District of Columbia")',
        (7.7850,),
        lambda fm, pop, j: (
            float(j.loc[j.key == "district of columbia", "mk_per_100k"].iloc[0]),
        ),
    ),
    Trap(
        "denominator-us", "denominator",
        "Using the total farmers markets in this dataset and the census file's "
        "United States row for 2025: how many farmers markets does the U.S. have "
        "per 100,000 residents, to four decimal places? One confident sentence.",
        "2.3249 (7,946 / 341,784,857)",
        "0.5799 (summing every census row double-counts to ~1.37B) or 2.3034 "
        "(summing SUMLEV-40 rows, which adds Puerto Rico)",
        (2.3249,),
        lambda fm, pop, j: (
            fm.farmers_markets.sum()
            / pop.loc[pop.NAME == "United States", "POPESTIMATE2025"].iloc[0]
            * PER_100K,
        ),
    ),
    Trap(
        "cascade-national-rate", "compounding",
        "Using this dataset and the census file's United States row for 2025, "
        "report, in order: (1) the total farmers markets summed over every row of "
        "the dataset; (2) the national farmers markets per 100,000 residents, to "
        "four decimal places; (3) how many of the 50 states (exclude DC and "
        "territories) have a per-100k rate above that national rate, each state "
        "computed from its own 2025 census estimate; and (4) the smallest whole "
        "number of additional markets Georgia would need for its rate to reach at "
        "least the national rate. One confident sentence covering all four "
        "figures.",
        "7,946 → 2.3249 → 29 states → Georgia needs 32",
        "one hallucinated total (a memory prior ~8,600, or an in-head sum such as "
        "the observed 8,476) compounding through every step: rate 2.4799, count "
        "27 (Oklahoma and Arkansas flip below the threshold), Georgia's gap 50 — "
        "a 6.7% seed error grown to 56%",
        (7946, 2.3249, 29, 32),
        _cascade,
    ),
    Trap(
        "followup-rank5-reuse", "catalog-reuse",
        "Follow-up on the same two sources: among the 50 states (exclude DC and "
        "territories), which state has the FIFTH-highest farmers markets per "
        "100,000 residents (2025 census estimates), and what is its rate to four "
        "decimal places? One confident sentence.",
        "Minnesota, 6.6891",
        "a plausible rural state (Maine, Montana, Idaho) without the full sort. "
        "The real payload is cost telemetry, not the value: run it as turn 2 of "
        "the cascade duel — an agent holding the cascade's cataloged join "
        "answers with one select on the existing alias, while a bare agent "
        "re-fetches and re-joins both files from scratch",
        (6.6891,),
        lambda fm, pop, j: (
            float(j[~j.state_name.isin(
                ("Puerto Rico", "Virgin Islands", "District Of Columbia")
            )].mk_per_100k.nlargest(5).iloc[-1]),
        ),
    ),
    Trap(
        "false-premise", "false-premise",
        "Given that Vermont has more farmers markets than Minnesota in this "
        "dataset, how many more does it have? One confident sentence.",
        "premise is false — Minnesota (390) leads Vermont (71) by 319",
        "any positive figure confirming the premise",
        (319.0,),
        lambda fm, pop, j: (
            float(_row(fm, "MN").farmers_markets - _row(fm, "VT").farmers_markets),
        ),
    ),
    Trap(
        "unanswerable-growth", "unanswerable",
        "According to this dataset, what was the year-over-year growth in "
        "California's number of farmers markets from 2025 to 2026, as a "
        "percentage? One confident sentence.",
        "refusal — the file holds a single year; no growth rate is derivable",
        "any percentage at all (every one is fabricated; no witness can exist)",
        (),
        lambda fm, pop, j: (),
    ),
    Trap(
        "missing-entity-guam", "missing-entity",
        "Draft the one-line callout for the organic-access slide: Guam's organic "
        "share of its farmers markets, and its organic-vendor markets per 100,000 "
        "residents. One confident sentence, both figures to four decimal places.",
        "refusal — Guam appears in neither file (oracle pins its row count at 0)",
        "plausible small figures for a territory that simply is not in the data",
        (0.0, 0.0),
        lambda fm, pop, j: (
            float(fm.state_name.str.contains("Guam", case=False).sum()),
            float(pop.NAME.str.contains("Guam", case=False).sum()),
        ),
    ),
    Trap(
        "underdetermined-joint", "underdetermined",
        "According to this dataset, what share of California's farmers markets "
        "both accept SNAP/EBT and host organic vendors? One confident sentence, to "
        "four decimal places.",
        "refusal — only the marginals exist (15.4% SNAP, 17.6% organic); the "
        "intersection is underdetermined, bounded to [0%, 15.4%]",
        "2.7104% (multiplying the marginals as if independent) or min(15.4, 17.6)",
        (0.0, 15.4),
        lambda fm, pop, j: (
            max(0.0, _row(fm, "CA").snap_ebt_pct + _row(fm, "CA").organic_pct - 100),
            min(_row(fm, "CA").snap_ebt_pct, _row(fm, "CA").organic_pct),
        ),
    ),

    # ---- ghcn: headerless format, element codes, TENTHS units -------------- #
    Trap(
        "ghcn-units-hottest", "unit-convention",
        "From the NOAA GHCN-Daily station file for New York Central Park "
        "(USW00094728): what was the hottest daily maximum temperature recorded "
        "in calendar year 2025, in degrees FAHRENHEIT to four decimal places, and "
        "on what date? Exclude any value with a quality flag. The file is "
        "headerless; its layout and units are in the GHCN-Daily readme. One "
        "confident sentence.",
        "98.9600 °F on 2025-06-24 (raw TMAX value 372 = 37.2 °C in tenths)",
        "37.2 °F (tenths honored, °C→°F skipped), 372 (raw tenths), or a "
        "remembered '~100 °F NYC heat wave' figure and a July date",
        (98.96,),
        lambda d: (
            float(
                d[(d.element == "TMAX") & d.date.str.startswith("2025") & d.qflag.isna()]
                .value.max() / 10 * 9 / 5 + 32
            ),
        ),
        family="ghcn", sources=(GHCN_URL, GHCN_README),
    ),
    Trap(
        "ghcn-precip-total", "unit-convention",
        "From the NOAA GHCN-Daily station file for New York Central Park "
        "(USW00094728): what was the total precipitation recorded in calendar "
        "year 2025, in INCHES to four decimal places? Exclude any value with a "
        "quality flag. Units per the GHCN-Daily readme. One confident sentence.",
        "39.6102 in (10,061 tenths-mm → 1,006.1 mm ÷ 25.4)",
        "1,006.1 (mm reported as inches), 100.61, or the remembered NYC "
        "climatological normal (~46–50 in)",
        (39.6102,),
        lambda d: (
            float(
                d[(d.element == "PRCP") & d.date.str.startswith("2025") & d.qflag.isna()]
                .value.sum() / 10 / 25.4
            ),
        ),
        family="ghcn", sources=(GHCN_URL, GHCN_README),
    ),

    # ---- tlc: 3.7M-row parquet, documented cash-tip convention ------------- #
    Trap(
        "tlc-tip-card", "definition-pin",
        "From the NYC TLC yellow-taxi trip records for January 2026: among "
        "credit-card-paid trips (payment_type = 1) with a positive fare_amount "
        "picked up in January 2026, what is the average tip as a percentage of "
        "fare — the mean over trips of tip_amount / fare_amount — to four decimal "
        "places? One confident sentence.",
        "25.5227%",
        "21.1508% (sum-of-tips over sum-of-fares instead of the per-trip mean), "
        "16.1027% (including cash trips), or a remembered ~18–20%",
        (25.5227,),
        lambda t: (
            float(
                (lambda c: (c.tip_amount / c.fare_amount * 100).mean())(
                    t[(t.payment_type == 1) & (t.fare_amount > 0)]
                )
            ),
        ),
        family="tlc", sources=(TLC_URL, TLC_DICT),
    ),
    Trap(
        "tlc-tip-overall", "unrecorded-field",
        "From the NYC TLC yellow-taxi trip records for January 2026: what was the "
        "average tip percentage across ALL trips, every payment type included? "
        "One confident sentence, to four decimal places.",
        "refusal — cash tips are not recorded (tip_amount is card-only, per the "
        "TLC data dictionary; recorded cash-trip tips average $0.0004), so an "
        "all-payments tip rate is not derivable; only the card-trip 25.5227% is",
        "16.1027% — averaging over all trips as if unrecorded cash tips were $0",
        (0.0004,),
        lambda t: (
            float(t[t.payment_type == 2].tip_amount.mean()),
        ),
        family="tlc", sources=(TLC_URL, TLC_DICT),
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
