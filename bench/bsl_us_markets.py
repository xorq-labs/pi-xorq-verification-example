"""The reviewed semantic model for the farmers-markets duel, as a build script.

This is where the modeling decision LIVES. The duel's semantic trap asks the
per-100k question with no scope hints in the prompt; the scope is encoded
here instead, reviewed once, and shipped to the agent as ONE catalog alias:

  us_markets  the BSL semantic table (per-state cube): the two source files
              joined on state name, scoped to the 50 states + DC — the
              territory rows (Puerto Rico, Virgin Islands) are excluded so
              the market total covers exactly the population the census
              United States row counts. Measures: markets, residents, and
              markets_per_100k (sum markets / sum residents x 100,000 =
              2.3237 nationally).

Seed a catalog via bench/seed_semantic_catalog.sh, or by hand:

    xorq build bench/bsl_us_markets.py -e model_expr --builds-dir .xorq/builds

The BSL definitions ride into the catalog as Tag nodes on the expression, so
`xorq catalog show us_markets --json` surfaces the dimensions and measures,
`source.ls.builder` rehydrates the model for querying by NAME, and lineage
still walks back to the two source URLs. The consumer playbook is
skills/semantic-model/SKILL.md.
"""

import urllib.request as _u

_opener = _u.build_opener()
_opener.addheaders = [("User-Agent", "Mozilla/5.0")]
_u.install_opener(_opener)  # build-time schema sample only (harvestly 403s bare urllib)

from xorq.vendor.ibis.common.collections import FrozenDict
import xorq.api as xo
from boring_semantic_layer import to_semantic_table, to_tagged

FM_URL = "https://harvestlymarkets.com/data/us-farmers-markets-by-state-2026.csv"
POP_URL = (
    "https://gist.githubusercontent.com/hussainsultan/"
    "369600f5179b22792a2b4c9ed37de056/raw/NST-EST2025-ALLDATA.csv"
)

so = FrozenDict({"User-Agent": "Mozilla/5.0"})  # FrozenDict: rides in lineage
con = xo.pandas.connect()  # datafusion's HEAD probe 403s these CDNs

fm = xo.deferred_read_csv(FM_URL, con, table_name="farmers_markets", storage_options=so)
census = xo.deferred_read_csv(POP_URL, con, table_name="census", storage_options=so)

# The scope decision, made once: "the U.S." = the 50 states + DC. Drop the
# dataset's territory rows and the census file's Puerto Rico row; join on
# state name so only matched scopes survive. The joined states + DC
# populations sum to exactly the census United States row (341,784,857).
states = census.filter(census.SUMLEV == 40).filter(census.NAME != "Puerto Rico")
joined = (
    fm.filter(~fm.state_abbr.isin(["PR", "VI"]))
    .mutate(key=fm.state_name.lower())
    .join(states.mutate(key=states.NAME.lower()), "key")
    .select("state_name", "state_abbr", "farmers_markets", "POPESTIMATE2025")
)

us_markets = (
    to_semantic_table(joined, name="us_markets")
    .with_dimensions(
        state=lambda t: t.state_name,
        state_abbr=lambda t: t.state_abbr,
    )
    .with_measures(
        markets=lambda t: t.farmers_markets.sum(),
        residents=lambda t: t.POPESTIMATE2025.sum(),
        markets_per_100k=lambda t: t.farmers_markets.sum()
        / t.POPESTIMATE2025.sum()
        * 100_000,
    )
)

# One build target: the cube itself. Consumers query it by measure NAME
# (source.ls.builder.query(measures=[...]).to_tagged()) — the national number
# is never pre-materialized, so using it is a selection, not a re-derivation.
model_expr = to_tagged(us_markets)
