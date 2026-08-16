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

model_expr = to_tagged(us_markets)
