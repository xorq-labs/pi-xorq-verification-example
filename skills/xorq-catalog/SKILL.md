---
name: xorq-catalog
description: Discover and load pre-computed tables from a xorq semantic catalog. Use when working with data files, analysis, feature engineering, or answering data questions with a catalog present.
---

# xorq catalog

This project has a **xorq catalog** — a git-backed, re-runnable ledger of named
expressions. Prefer catalog entries over loading raw files: they are cached,
versioned, and encode the joins/aggregations/domain logic already.

The catalog is the git-backed directory **`.xorq/catalog`** (the default path).
It is managed **only** by the `xorq catalog` CLI — never hand-edit a
`catalog.yaml`; a hand-written `entries:/aliases:` YAML is not a catalog and
errors (`unsupported operand type(s) for +: 'dict' and 'str'`).

## Discover — BEFORE ingesting anything

Orient first, plan second. Even when the question hands you source URLs, do
NOT start by ingesting them — the catalog may already hold what the question
needs, and the semantic-model check below is the whole reason this flow exists:

```
xorq_semantic_models   catalog_path=.xorq/catalog   # reviewed measures FIRST
```

If a listed model's measure matches the question (a rate, a total, a
per-capita — read the measure names), you answer by querying that measure BY
NAME — the semantic-model skill has the pattern — and you do not ingest the
raw sources at all, even though the prompt cites them: the measure's reviewed
definition already encodes the scope the prompt leaves unstated, and
re-deriving it from the raw files is how defensible-looking wrong answers
happen. Query the measure that answers DIRECTLY, never its components: a
measure queried with no dimensions is already the grand total at the model's
full reviewed scope — nothing to aggregate, compose, or divide afterward.
Only with no matching measure do you continue here:

```bash
xorq catalog -p .xorq/catalog list-aliases   # what's available
xorq catalog -p .xorq/catalog info           # entry/alias counts, remotes
```

`info` succeeding means the catalog exists — do **not** run `init` on it, not
even defensively (`init` on an existing catalog just errors, and the noise reads
as a failure). The catalog is normally created before you start; `init` only
when `info` itself fails.

## Inspect a source — through the catalog, never `curl`

Do **not** `curl | head/tail/wc` a source to learn its shape: every such call
re-downloads the whole file (fatal at parquet sizes), and eyeballing raw bytes is
a side-channel around the lineage this catalog exists to keep. The raw-ingest
script below needs **no schema knowledge** — `xorq build` infers the schema from
the source itself. So when a source has no alias yet, ingest it raw FIRST, then
inspect the *entry*:

```bash
xorq catalog -p .xorq/catalog schema orders   # exact columns + dtypes — no re-fetch
```

(or the `xorq_catalog_schema` tool — both read the entry's serialized metadata,
no execution), and peek rows with `xorq_select` composing `source.limit(5)` if
you must see values — peeks are cheap: the alias's sources are snapshotted
locally on the first select and every later compose reuses the snapshot, while
verification always re-runs from the real sources. Write any transform or join
**after** reading the schema from the entry — a guessed column name is the #1
cause of failed builds. And match the aggregation to the table's **grain**: in a
per-group rollup (one row per group, e.g. `…-by-region.csv`), a whole-population
total is `source.<count_col>.sum()` — `source.count()` counts the *groups*, a
different (and usually wrong) number.

## Ingest a source (no alias yet? build one — do NOT fall back to pandas/csv)

If the data you need has no catalog alias, add it to the catalog first, then
answer against the alias. **Read it at its source and keep the source in the
lineage** — never `curl`/download it locally, never snapshot a remote read, and
never hand-type values into a file. The alias must re-read from the real source.

Write build scripts under **`.xorq/scripts/`** (`mkdir -p .xorq/scripts` first)
and build into **`.xorq/builds`** — both live under the gitignored `.xorq/`, so
repeated runs never litter the repo root or `git status`. The catalog archives
each added build, so nothing outside `.xorq/` needs to survive.

**A remote CSV/URL** — read it directly (no local download) so `xorq build`
serializes the URL into the expression and the alias re-fetches from source on
every run. Use the pandas backend and carry any required HTTP header in
`storage_options` as a **`FrozenDict`** so it rides in the lineage:

```python
# .xorq/scripts/build_orders.py
import urllib.request as u
# Schema is sampled over http at BUILD time (infer_csv_schema_pandas ignores
# kwargs) and some hosts 403 urllib's default User-Agent — give the build a real
# one so that sample succeeds. Execute-time reads use storage_options below.
_o = u.build_opener(); _o.addheaders = [("User-Agent", "Mozilla/5.0")]; u.install_opener(_o)

from xorq.vendor.ibis.common.collections import FrozenDict
import xorq.api as xo
con = xo.pandas.connect()
expr = xo.deferred_read_csv(
    "https://host/path/data.csv", con, table_name="orders",
    storage_options=FrozenDict({"User-Agent": "Mozilla/5.0"}),  # rides in lineage → used at run/verify
)
```

Why this exact shape:
- `deferred_read_csv` (not `con.read_csv`, and **no** `ParquetSnapshotCache`) keeps
  the URL in `expr.yaml`, so re-runs re-fetch from source — lineage, not a snapshot.
- `storage_options` as a `FrozenDict` is *hashable* (xorq serializes it into the
  op) **and** a Mapping (pandas honors it at read), so the header travels with the
  expression — verify / `catalog run` in any process sends the UA with no global
  opener. A plain `dict` fails with `unhashable type: 'dict'`.
- The build-time `install_opener` is only for the schema-inference sample; the
  serialized `storage_options` covers every execute.
- Prefer the **pandas** backend for a URL: the default (datafusion) backend's
  object-store does a HEAD probe that some CDNs answer without `Content-Length`,
  which it rejects; a plain GET (pandas) is fine. No `duckdb` needed.

This is not just etiquette: under the `no_local_sources` lineage policy the checker
reads each alias's *actual* sources from its op-tree and **fails** any alias whose
source is a local/scratch path or an in-memory/hand-typed table (the `remote_sources`
check). A URL-backed alias passes; a downloaded-and-hand-added one does not.

**A local file** — same shape with a local path; a snapshot cache is fine here
because the file itself is the source:

```python
import os, xorq.api as xo
from xorq.caching import ParquetSnapshotCache
con = xo.connect()
expr = xo.deferred_read_csv(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "orders.csv"),
    con, table_name="orders",
).cache(ParquetSnapshotCache.from_kwargs(source=con))
```

Build and add (either case):

```bash
xorq build .xorq/scripts/build_orders.py --builds-dir .xorq/builds --emit-build-path-to /tmp/bp.txt
xorq catalog -p .xorq/catalog info >/dev/null 2>&1 \
  || xorq catalog -p .xorq/catalog init                    # init only when missing — re-init errors
xorq catalog -p .xorq/catalog add "$(cat /tmp/bp.txt)" -a orders --no-sync
xorq catalog -p .xorq/catalog list-aliases                 # confirm: orders
```

Three mechanical rules, all from real failed runs:

- **Run catalog writes (`add`/`compose`/`remove-alias`) one at a time, never as
  parallel tool calls.** Every write commits into the catalog's git repo; two in
  flight contend on its `index.lock` and one fails (`Lock … could not be
  obtained`). If you hit that error, just re-run the failed command — do not
  delete the lock file. The collision can also surface as a **bare `Error:`
  with no message**, leaving a HALF-WRITTEN catalog: `catalog.yaml` lists the
  entries and the alias symlinks exist, but every later command
  (`list-aliases`, `schema`, another `add`) errors the same way, and re-running
  does NOT help. Recovery: `rm -rf` the catalog directory, `init` it fresh, and
  re-`add` every build it held ONE AT A TIME — the builds under `.xorq/builds`
  are intact, nothing needs rebuilding (if the catalog was pre-seeded for you,
  re-run the seeding too).

- **Run `xorq build` and `catalog add` from the same directory** (the repo root,
  as above — no `cd`). The emitted build path is *relative to where `xorq build`
  ran*; changing directories between the two lets a stale `builds/` dir elsewhere
  satisfy the path and silently `add` the WRONG build. After every add,
  `xorq catalog schema <alias>` must show the columns your script just declared —
  a mismatch means a stale build got in; remove the alias and re-add the right path.
- **No `con.execute(...)` / `.to_pandas()` inside a build script.** A build
  serializes the expression; evaluating it at build time fails (`No translation
  rule for Read`) and would bake a snapshot even if it worked. Everything stays
  expression-level: aggregate / filter / join / mutate.

Now `orders` is a declared alias you can compose/verify on. **Never**
answer a data question from raw `pandas`/`csv`, a local download, or a hand-typed
value — those are the unverified paths this catalog exists to replace. If you
truly cannot get the data into the catalog, say the answer is **UNVERIFIED**; do
not present unchecked numbers as fact.

## Join two sources into a derived metric (the cross-dataset ratio pattern)

A metric that needs a value from *another* dataset — a per-capita, a rate whose
denominator lives in a second table (e.g. orders **per 1k headcount**, needing a
headcount source) — is the canonical two-table case. Ingest each source raw first
and read both schemas from the entries (see Inspect above); then do the metric in
**one build script** that reads BOTH sources and **joins the column in** — never
hand-type the second value, never bake a constant into a `mutate`, never
`curl`-peek. The join keeps BOTH source URLs in the lineage, so it passes
value-verify AND the `no_local` lineage check; a hardcoded constant fails the
latter. The raw aliases stay in the catalog as provenance.

```python
# .xorq/scripts/build_orders_per_1k.py
import urllib.request as u
_o = u.build_opener(); _o.addheaders = [("User-Agent", "Mozilla/5.0")]; u.install_opener(_o)

from xorq.vendor.ibis.common.collections import FrozenDict
import xorq.api as xo
so = FrozenDict({"User-Agent": "Mozilla/5.0"})
con = xo.pandas.connect()

fact = xo.deferred_read_csv(
    "https://host/path/orders-by-region.csv",
    con, table_name="orders", storage_options=so)
ref = xo.deferred_read_csv(
    "https://host/path/regions.csv",
    con, table_name="regions", storage_options=so)

# join on the shared key (here fact.region == ref.region_name), then derive the
# metric FROM COLUMNS of both tables — the denominator is fact's partner column,
# not a literal. Reference each side's columns explicitly (fact.x, ref.y).
expr = (
    fact.join(ref, fact.region == ref.region_name)
      .mutate(orders_per_1k=fact.orders / ref.headcount * 1000)
      .select("region", "orders", "headcount", "orders_per_1k")
)
```

```bash
xorq build .xorq/scripts/build_orders_per_1k.py --builds-dir .xorq/builds --emit-build-path-to /tmp/bp.txt
xorq catalog -p .xorq/catalog add "$(cat /tmp/bp.txt)" -a orders-per-1k --no-sync
```

Then `xorq_select` the metric cell from `orders-per-1k`, declare a `scalar`
obligation on it, and `xorq_check_lineage` it — the verdict lists **both** source
URLs. Notes: use the **pandas** backend (its join + http read work without
`duckdb`); reference columns as `fact.col` / `ref.col` to avoid ambiguity after the
join.

**No shared key — a whole-table total against a single reference row.** When the
metric divides an aggregate of one table by one row of another (a grand total over
the fact table, the reference filtered to its one relevant row), there is no key
to join on: give both single-row tables a constant key and join on that.

```python
totals = fact.aggregate(total_orders=fact.orders.sum()).mutate(k=xo.literal(1))
denom = (ref.filter(ref.region_name == "TOTAL")
            .select("headcount").mutate(k=xo.literal(1)))
expr = (
    totals.join(denom, totals.k == denom.k)
      .mutate(orders_per_1k=totals.total_orders / denom.headcount * 1000)
      .select("total_orders", "headcount", "orders_per_1k")
)
```

Two dead ends to skip: `join(..., literal=True)` is **not an API** — it fails the
build; and reading the denominator once and baking it into the formula as
`xo.literal(<value>)` drops the second source from the op-tree — the value looks
right but lineage now has one source, and the `no_local`/`no_magic_constants`
checks fail it. The constant key is scaffolding for the join; the *data* values
must both arrive as columns.

**Never `xorq catalog compose` when two datasets meet** — not to join them, and
not to stage one side's value for a later metric (`compose regions -c
"…headcount" -a total_headcount`). Compose re-materializes its input as a
`RemoteTable` snapshot of whatever was fetched at compose time: the URL drops out
of the op-tree, so `xorq_check_lineage` with `no_local` returns **DISCREPANCY**
("local source not allowed: RemoteTable(…) reads <hash>") and the answer gate
blocks the banner. Compose also cannot join — extra entries chain as *transforms*
of the first, never as a second source. It is fine for reshaping ONE declared
alias (a verify witness, a filter). The moment a metric needs a second dataset's
column, it must be a build script that reads BOTH sources, as above — and if that
build errors, fix the build; compose is not the fallback.

**Precision comes from raw counts, not pre-rounded columns.** Derive a share/ratio
from its counts (`fact.returned_orders / fact.orders` = `0.1708`), never
by converting a column the source already rounded (`return_pct = 17.1` → `0.1710`
invents a digit). The checker will still pass the padded value (it's faithful to the
rounded cell), so precision is your responsibility — compute from the counts when
more decimals are asked for. And when you verify a value you display **rounded**,
set the obligation's `value_type.tolerance` to half a unit at that decimal place (4
decimals → `"0.00005"`); tolerance `"0"` refutes the rounding against the exact cell.

## Load / run

Compose on a declared alias and run it — never recompute logic that an alias
already encodes:

```bash
xorq catalog -p <catalog_path> compose <alias> --alias verify_x \
  -c "source.order_by(source.n.desc()).limit(1)"
```

The source entry is a positional argument; inline code is `-c`/`--code` and
references it as `source`. One alias in, one reshaped alias out — compose
snapshots its input, so a composed entry is never the lineage of a
cross-dataset metric (see the cross-dataset ratio pattern above for that).

## Verifying an answer

When a data answer states quantitative facts, verify it against the catalog
rather than eyeballing it. Deterministically (no LLM):

```bash
pi-xorq-check gate request.json     # declared obligations → verdict + exit code
```

Or drive a pi session with the analyst role prompt (the single prompt — it both
produces verified answers and checks answers it did not produce). Set it up once
with `pi-xorq-check init` (writes the role into `AGENTS.md`, which pi auto-loads),
or pass it inline:

```bash
pi --append-system-prompt <(pi-xorq-check prompt) \
   "Verify this answer against <catalog_path>: <answer>"
```

Verification turns the answer into proof obligations, discharges each by
selecting the claimed value from a re-run of the (checker-synthesized) expression,
and returns a structured certificate (`VERIFIED | DISCREPANCY | COULD-NOT-VERIFY |
NO-OP`). See `docs/adr/0001` for the model.
