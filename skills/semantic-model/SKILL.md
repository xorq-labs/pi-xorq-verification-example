---
name: semantic-model
description: Find BSL semantic models in the xorq catalog FIRST, read their dimensions and measures, and answer by querying reviewed measures by name. Use before ingesting sources or composing metrics for any data question with a catalog present.
---

# Semantic models first

A catalog alias can be more than a table: a **BSL semantic model** is a
reviewed set of dimensions and measures whose definitions already encode the
modeling decisions — scopes, joins, weightings, exclusions — that a question
leaves unstated. When a measure matches the question, the right answer is a
**selection of that reviewed measure by name**, never your own re-derivation.

Check for semantic models BEFORE ingesting sources and BEFORE composing your
own joins or aggregates. Re-deriving a metric an existing measure already
defines is how defensible-looking wrong answers happen: your improvised scope
can differ from the reviewed one and still discharge cleanly.

## 1. Find the semantic models

```
xorq_semantic_models   catalog_path=.xorq/catalog
```

Lists every alias carrying a BSL model with its dimensions and measures. The
measures are the menu of reviewed metrics; the dimensions are what you may
slice them by. Then, for the model you will use:

```
xorq_semantic_schema   catalog_path=.xorq/catalog  alias=<alias>
```

(`xorq_catalog_schema` shows only the base table's columns — the reviewed
definitions live in the model's tag, which these two tools read.) No semantic
models listed → fall back to the ordinary xorq-catalog flow (ingest, compose,
declare).

## 2. Read measures by NAME

Use `xorq_semantic_select` — names in, CSV out, no compose string to build:

```
xorq_semantic_select  alias=<alias>  measures=["markets_per_100k"]
xorq_semantic_select  alias=<alias>  dimensions=["state"]  measures=["markets_per_100k"]
```

**Grain: no dimensions = the grand total.** A measure queried with NO
dimensions returns ONE row — the measure evaluated over the model's full
reviewed scope. A rate or total measure queried bare IS the national/overall
answer, already at the question's grain. Do not add dimensions and aggregate,
do not `compose` a total, do not fetch components and combine them — if you
catch yourself planning "query the pieces, then sum/divide," stop and re-read
the measure list: the measure that answers directly is almost certainly there.

(The equivalent `xorq_select` compose, when you need it:
`source.ls.builder.query(measures=['markets_per_100k']).to_tagged()` — names
as strings, `.to_tagged()` last, nothing chained after.)

Rules:

- **Pick the measure that answers the question directly.** If the question
  asks for a rate and `markets_per_100k` exists, query THAT — never fetch
  `markets` and `residents` and divide them yourself. The checker enforces
  this: arithmetic that matches no declared measure fails `selection_only`.
- **Never rebuild a measure from the alias's raw columns** (e.g. your own
  `source.aggregate(x=source.a.sum() / source.b.sum())`) when a measure of
  that meaning exists — the measure's definition is the reviewed one; yours
  is a guess that may scope differently.
- Names are strings from the model's schema, not expressions or lambdas.
- The model's row scope (which rows are in, which are excluded) is part of
  the reviewed definition — do not "correct" it with extra filters unless the
  question explicitly asks for a different scope.

## Failure modes (each of these has been hit; don't rediscover them)

- `source.select('<measure>')` → *column not found*. Measures are NOT columns
  of `source`; they exist only through `source.ls.builder.query(...)`.
- Chaining after the query — `....to_tagged().aggregate(...)` or referencing
  `source.<measure>` → *AttributeError*. The query result is finished output;
  ask for the measure/dimension set you want in ONE `query(...)` call.
- Hand-writing the obligation's `expression` as your own aggregate →
  `✗ selection_only`. A semantic value takes NO expression at all — declare
  `predicate.measures` and the checker synthesizes the reviewed query (§3).
- Verifying component measures separately and stating a hand-divided ratio →
  the ratio itself has no discharged witness and the banner stays NOT
  VERIFIED. Read and declare the ratio measure itself.
- `xorq catalog compose <alias>` without `-p .xorq/catalog` → *Entry not
  found* (it consulted the default catalog, not this one). Always pass `-p`.

## 3. Verify what you state

A semantic-model value is declared by NAME too — `predicate.measures` — and
the checker synthesizes the model's own query as the witness. No
`expression`, no `population`:

```json
{
  "id": "markets-per-100k",
  "kind": "scalar",
  "surface": "2.3237",
  "on": "us_markets",
  "predicate": {"measures": ["markets_per_100k"], "select": "markets_per_100k"},
  "value_type": {"kind": "decimal", "tolerance": "0.00005"}
}
```

(Display rounded to 4 decimals → tolerance `"0.00005"`, half a unit at the
last shown place. For a sliced value, add `"dimensions": ["state"]` and
ground the row with `entity_col`/`entity_val`.) Then `xorq_check_lineage`
the alias as usual — a semantic model's lineage walks back to its source
URLs like any other entry.
