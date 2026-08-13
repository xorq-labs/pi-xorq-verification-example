# ADR-0001: Verification as proof-obligation discharge over the catalog

- **Status:** Accepted — decision procedure implemented in
  `src/pi_xorq_verifier/checker.py` and exercised end-to-end against a live
  catalog (see Consequences). **Update 2026-07-03:** the `pi-subagents` dependency
  was removed. The trust root is the deterministic checker; the tools ship as a
  plain pi extension; the analyst is a single role prompt shipped as package data
  (`pi-xorq-check init` writes it into a consumer project's `AGENTS.md`, which pi
  auto-loads; see §7). pi-subagents remains a valid *optional*
  way to package these as restricted subagents, but nothing here depends on it.
- **Date:** 2026-07-02
- **Deciders:** Hussain Sultan
- **Supersedes prior art:** xorq-desktop ADR-0005 (verification engine over the
  catalog) and its `verification-model.md`. This ADR keeps their invariants and
  formalizes their mechanism.

## Context

xorq-desktop already verifies data answers by re-checking prose against the
catalog: it *discovers* the claims with a linguistic parser (`parse_claims.py`,
spaCy), the verifier LLM *guesses* which alias witnesses each claim, composes a
`verify_*` expression on top of it, **selects** the claimed value (never computes
it — the "no arithmetic" rule), and a host gate hardens the result into a verdict
(`VERIFIED | DISCREPANCY | COULD-NOT-VERIFY | NO-OP`).

That system works, but its formality is thin in three places, and those are
exactly where trust leaks:

1. **Claims are discovered, not declared.** NLP extraction over prose is a
   heuristic; the *set* of things being verified is fuzzy.
2. **The claim→expression binding is model-mediated.** The verifier LLM decides
   which alias and what to compose. The trust root includes the model's judgment.
3. **The rigor lives in bolted-on lints.** Circularity, superlative "witnesses",
   and row-grounding are special cases layered onto a prose verdict rather than
   consequences of one semantics.

We are building `pi-xorq-verifier` on nicobailon's `pi-subagents` (a generic
`subagent` tool that spawns child pi processes and can enforce `outputSchema`).
That gives us a clean seam to make the verifier **formal** rather than port the
informal version.

## Decision drivers

- Keep the load-bearing invariant: **a fact counts as verified only when its
  value is *selected* from a re-run — never computed, never taken on the model's
  word.** Extend it, don't bypass it.
- The catalog is already the re-runnable ledger (xorq-desktop ADR-0004/0005). A
  verdict must be a **re-checkable certificate** that attaches to the catalog,
  not a prose line.
- No silent pass: a verifier that *cannot* confirm must render distinctly, never
  as a green check.
- Shrink the model's role to something it is good at (proposing a query) and move
  the decision to deterministic code.

## Decision

Model verification as **discharging proof obligations against the catalog.** The
producer (main agent) declares obligations — a claim *paired with* the expression
meant to witness it — and the verifier is reduced to a **checker** with a
decision procedure that either discharges each obligation or fails closed. The
LLM verifier becomes a *witness synthesizer* feeding the deterministic checker;
the checker is the trust root.

### 1. The formal object: a claim is a typed obligation

A claim is a judgment `⟦witness⟧_Cat ⊨ φ` — "evaluating this expression against
the live catalog satisfies this predicate":

```
Obligation ::= ⟨ id,
                 kind,          -- scalar | argmax | argmin | compare | count | membership
                                --        | table | metric | metadata | provenance
                 surface,       -- the value verbatim as written in the answer ("17,875")
                 witness,       -- an expression composed over a DECLARED alias (the query q)
                 predicate φ,   -- a decidable relation the witness result must satisfy
                 value_type,    -- int | decimal(ε) | percent | currency | date | categorical
                 requires_sources ⟩  -- lineage constraint: sources the witness must draw from
```

The producer declares `(surface, witness, predicate)`. The binding is **given**,
not inferred — this is the whole difference from prior art.

### 2. Semantics

An obligation *holds* iff three independent conditions are all true:

```
  eval:         R = ⟦witness⟧_Cat                       -- run against the live catalog
  predicate:    φ(R)                                     -- R satisfies φ
  faithfulness: normalize_type(surface) ≡_ε select(R)    -- the answer's value renders the cell
  provenance:   lineage(witness) ⊨ requires_sources      -- sources match the prose attribution
```

`≡_ε` is **typed equality with declared tolerance** — a total normalization per
`value_type` (strip thousands separators, unit-normalize percent, ε-round
decimals). This replaces ad-hoc numeric matching with a semantics.

### 3. Decision procedure + soundness rules

The checker is **selection-only**: it reads cells and compares to literals; it
has *no arithmetic capability*. That restriction is what makes it a *faithfulness*
checker (does the data say this?), formalizing "no arithmetic" as a property of
the checker, not a prompt rule.

The three prior-art lints become **derived from the type + the witness AST**:

| xorq-desktop lint | Formalized as |
|---|---|
| superlative "witness" heuristic | `argmax`/`argmin` = a maximality quantifier `∀x. m(x) ≤ m(e)`, discharged by an `order_by(m desc).limit(1)` witness whose row shows `(entity, value)` together. A *kind*, not a lint. |
| anti-circularity regex | **Well-formedness on the witness:** the claimed literal may not appear as an equality constant in any `filter` of the witness. Inequality analysis parameters are fine. A syntactic, decidable check. |
| row-level grounding | The entity-claim predicate requires the entity and value **co-selected in one row** — a structural shape obligation on the witness. |

Plus a well-formedness gate every obligation passes before evaluation: the
witness composes only on **declared aliases** (never a bare raw source unless the
claim is about raw data), and satisfies the **shape required by its kind**
(`compare` → two selected cells; `count` → an aggregate; `metric` → a scorer
expression composed on a prediction alias).

**Binding convention.** The composed expression's bound table is `source`;
witnesses are written `source.<col>` (`xorq catalog run -c` does not bind ibis's
deferred `_`, so the checker also rewrites a standalone `_.` to `source.`). A
`count` witness must be an aggregate that returns a table
(`source.aggregate(n=source.count())`); a bare `source.count()` is a scalar and
does not run.

### 4. Verdict = a certificate over a monotone lattice

Each obligation gets a status on `DISCHARGED ⊏ COULD-NOT-DISCHARGE ⊏ REFUTED`.
The turn verdict is a **monotone fold** — the model can never upgrade it:

```
all DISCHARGED                    → VERIFIED
any REFUTED                       → DISCREPANCY
else any COULD-NOT-DISCHARGE      → COULD-NOT-VERIFY   (conservative top)
no obligations                    → NO-OP
```

The certificate carries each witness as re-runnable code plus a content hash,
alongside the `surface` it checked, the `value_type`/`tolerance` it was judged
under, and its selected cell — and pins the `catalog_state` it ran against. The
population is not a separate field: it lives in `witness_code` (the synthesized
ranking over `witness.compose`), so re-running the code shows exactly the universe
it was judged over. Every producer-declared dial that widened acceptance is
therefore disclosed on the certificate, so anyone can re-run it *and* see under
what tolerance and population it passed, without holding the original request. Because the catalog is the ledger,
a `VERIFIED` obligation becomes a re-derivable property of the entry, not a one-off
assertion.

### 5. Soundness boundary + coverage (the honest limits)

*(Reframed 2026-07-07 in the vocabulary of certifying algorithms — Mehlhorn et
al.; see References. The guarantee is unchanged; the statement is sharper.)*

- **The checker is a certifying algorithm for expression evaluation.** The
  certified function is `f(x) = ⟦expression⟧_Cat` where the input
  `x = (expression, predicate φ, catalog state)` — the catalog pin is *part of
  the input*, which is what makes `x` well-defined and the certificate
  re-decidable (`catalog_state`; the remote-sources policy exists to keep `x`
  re-obtainable, not as decoration). The answer's claim is `y`; the witness `w`
  is the certificate's evidence (selected cell, co-selected extremum row,
  recomputed extremum, check vector). `W(x, y, w) ⟹ y = f(x)` has an elementary
  proof (typed equality over a selection), and the checker decides `W`
  deterministically, fail-closed.
- **The NL question is not `x`. It is the modeling layer, outside the certified
  function** — exactly as a certified LP solver certifies "this solution is
  optimal for *this* LP," never "this LP models your factory." Question →
  expression is the LLM's (or analyst's) modeling step; it is uncertified by
  construction and *disclosed* for human audit (`witness_code`, the population,
  the tolerance — the certificate is designed to make reviewing the model cheap,
  like a published LP formulation). A wrong-but-runnable expression is therefore
  a modeling error, not a leak in the certifying core; the certificate's
  `soundness` string ("faithful-to-declared-expressions; not a correctness
  re-derivation") states this boundary on every run.
- **Well-formedness keeps `W` non-vacuous.** A circular witness
  (`filter(col == v)` under "the value is v") evaluates *faithfully* — the
  danger is not a false `y = f(x)` but a degenerate `x` whose certified reading
  collapses to "v exists somewhere" while presenting as the substantive claim.
  The circularity / clean-restriction / rooted-on-alias gates restrict the
  admissible instance space so a passing witness always proves the substantive
  reading of its kind — they are what makes `W` "deserve its name," not
  intent-checking.
- **Checking-by-recomputation is legitimate, not a compromise.** Extrema admit
  no sublinear witness (verifying "no row beats this one" inherently touches
  every row), so the maximality recompute is the optimal check; and Mehlhorn's
  resource condition (checker ≤ constant × solver) holds absurdly well when the
  untrusted solver is an LLM — one extra aggregate query is noise.
- **The residual trust base inside the certified core is the engine-as-oracle**:
  deciding `W` evaluates `f` through xorq, which is exactly the CPLEX-shaped
  software Mehlhorn warns about. Mitigations, in order of leverage: re-execute
  `witness_code` on a *different backend* (it is portable xorq — the single
  oracle becomes cross-checking independent ones), and formally verify the pure
  layer (`checker.py` is small, frozen, functional — a realistic target).
- **Completeness** is bounded by the obligation set, so the NLP parser is
  **demoted to a coverage auditor**: every quantitative surface token in the
  answer must be covered by some obligation; an uncovered value forces
  `COULD-NOT-VERIFY`. spaCy stops being the trust root and becomes a completeness
  check (and is optional — a simpler tokenizer suffices to seed coverage). In the
  certifying-algorithm frame this is the **binding layer**, and it exists only
  because `y` here is prose carrying *many* alleged `(x, y)` pairs where a
  classical certifying algorithm returns one typed value: coverage (and the
  answer gate's claim/superlative matching) ensures every claim the prose ships
  corresponds to some certified instance.

### 6. The two artifacts

**Request** (declared expressions + obligations):

```json
{
  "catalog_path": ".xorq/catalog",
  "expressions": [{"alias": "flights-by-origin", "lineage": ["flights.csv"]}],
  "reply_values": ["17,875"],
  "obligations": [{
    "id": "c1", "kind": "argmax", "surface": "17,875",
    "value_type": {"kind": "int", "tolerance": "0"},
    "witness": {"on": "flights-by-origin", "compose": ""},
    "predicate": {"select": "n", "entity_col": "origin", "entity_val": "ATL", "metric_col": "n"},
    "requires_sources": ["flights.csv"]
  }]
}
```

`compose` is empty: for a declared alias the checker *synthesizes* the canonical
`order_by(metric desc).limit(1)` ranking from the predicate and reports it as
`witness_code` — a producer never hand-writes the ranking (that would reopen the
maximality hole). A non-empty `compose` is only a population *restriction* (a
filter) the synthesis then ranks over.

The `expressions[].lineage` grounds provenance: `requires_sources` must be a
subset of the declared lineage of the alias the witness composes on. (xorq
exposes no lineage CLI, and a build's Read nodes reflect physical inputs — a
`memtable` reads as `InMemoryTable`, not `flights.csv` — so the *declared*
lineage in the request is the provenance oracle, exactly as this artifact carries
it.)

**Certificate** (verdict):

```json
{
  "verdict": "VERIFIED",
  "obligations": [{
    "id": "c1", "status": "DISCHARGED",
    "surface": "17,875", "selected_cell": "17875",
    "value_type": {"kind": "int", "tolerance": "0"},
    "witness_alias": "flights-by-origin",
    "witness_code": "source.order_by(source.n.desc()).limit(1).select('origin', 'n')",
    "witness_hash": "sha256:a57283c762df6462", "witness_ref": "",
    "sources": ["flights.csv"], "detail": "",
    "checks": {"witness_on_declared_alias": true, "noncircular": true,
               "selection_only": true, "shape:argmax": true, "row_grounding": true,
               "typed_eq": true, "maximality": true, "provenance": true}
  }],
  "coverage": {"uncovered": []},
  "catalog_state": "sha256:2c0c1fc1736e2064",
  "soundness": "faithful-to-declared-expressions; not a correctness re-derivation"
}
```

### 7. Division of labor in `pi-xorq-verifier`

- **Deterministic checker** (`src/pi_xorq_verifier/checker.py` — the pure
  decision layer — plus `src/pi_xorq_verifier/witness.py`, the xorq-backed layer):
  typed-value equality, the monotone fold, op-tree well-formedness, and in-process
  witness evaluation (selection-only — it reads cells from the result, never
  computes them). This is the trust root. Every step fails closed to
  `COULD-NOT-DISCHARGE`, so a witness that will not build or evaluate can never
  become a false pass. `scalar`/`count`/`argmax`/`argmin`/`membership` discharge
  fully today (see the 2026-07-03 op-tree update for how maximality is now
  discharged rather than asserted); `compare`/`metric`/`metadata`/`provenance` are
  declared but fail closed until their predicate models land.
- **Analyst role prompt** (`src/pi_xorq_verifier/prompts/analyst.md`, the single
  prompt, shipped as package data): pi auto-loads it from a consumer's `AGENTS.md`
  after `pi-xorq-check init` (or `pi --append-system-prompt <(pi-xorq-check prompt)`).
  The analyst obtains values via
  `xorq_select`, declares a *predicate* per number, and self-verifies via
  `xorq_verify`; the same prompt also checks an answer it did not produce (declare
  its numbers as obligations, discharge). It does not decide the verdict — the
  checker folds it. (Originally two prompts + `pi-subagents` agents/chains; the
  dependency was dropped 2026-07-03, and the separate verifier prompt was removed
  once the deterministic checker — not an LLM — became the trust root, so a second
  agent added no soundness. See the 2026-07-03 op-tree update.)
- **Enforcement:** the certificate shape is fixed by the deterministic checker,
  which emits it directly (schemas ship under `schemas/`). The load-bearing gate
  is deterministic — `pi-xorq-check gate request.json` exits non-zero unless the
  verdict clears the gate — so correctness never depends on what an LLM narrates.
  (An earlier iteration also enforced the LLM's final-message shape via a
  `pi-subagents` chain + `structured_output`; that was cosmetic on top of the
  deterministic gate and was removed with the dependency.)

### Every ADR-0005 mode is now a `kind`

ML-metric verification is `kind: metric` (compose a scorer on a prediction
alias, select the metric cell). Leakage is a `provenance`/`metric` predicate:
assert the scorer's rows carry the test partition and share no membership with
the fit partition (set-disjointness over split-lineage columns). Provenance
attribution is `kind: provenance`. No new machinery — new predicates.

## Alternatives considered

### Port xorq-desktop's verifier as-is (LLM fact-checker + host hardener)

**Rejected.** It works but leaves claim extraction and the claim→expression
binding model-mediated, and encodes rigor as special-case lints. We can do better
at the seam pi-subagents gives us; porting the informal version forecloses that.

### Keep claim discovery as the primary mechanism (NLP-first)

**Rejected as the primary path; kept as coverage.** Discovering claims from prose
is inherently fuzzy. Declared obligations make the checked set explicit and
deterministic; the parser is still valuable as the *completeness* auditor, so it
is retained in that demoted role.

### A second store / experiment ledger for verdicts

**Rejected** for the same reason xorq-desktop ADR-0005 rejected it: the catalog
is the ledger. Certificates attach to the alias and are re-derivable; a second
store reintroduces a divergence we do not want.

## Consequences

### Positive

- The model moves from *judge* to *witness-proposer*; the verdict is decided by
  deterministic code and is re-checkable.
- Circularity / superlative / row-grounding fall out of one typed predicate
  algebra + AST well-formedness instead of being maintained as separate lints.
- Typed value semantics with declared tolerance replaces ad-hoc numeric matching.
- `COULD-NOT-VERIFY` is a lattice element, not a prompt convention; no silent
  upgrade is possible.
- The soundness boundary (faithfulness ≠ correctness) is explicit.
- ADR-0005's modes (ML metric, leakage, provenance) are new `kind`s, not new
  machinery.

### Negative

- Obligations must be *produced*. Either the main agent emits them (a typed
  generalization of xorq-desktop's ```facts appendix) or the verifier elaborates
  them from the answer — the latter re-admits some model judgment at extraction
  time, bounded by the coverage audit.
- Witness evaluation imports `xorq` in-process (see the 2026-07-03 op-tree
  update), so the checker now has a hard dependency on the `xorq` package. Its
  structural checks are coupled to xorq's operation-graph API (`walk_nodes`, the
  `ops.*` node classes) — a more version-sensitive surface than the CLI, though
  `walk_nodes` is a public, ADR-cited utility. A failed import fails closed:
  every obligation becomes `COULD-NOT-DISCHARGE` (no false pass, but also no
  verification).
- Provenance is grounded in the request's *declared* `expressions[].lineage`, not
  re-derived from the catalog (xorq has no lineage CLI, and build Read nodes
  reflect physical inputs). A wrong or absent declaration yields
  `COULD-NOT-DISCHARGE`, never a silent pass — but the declaration itself is
  producer-attested.
- `compare`/`metric`/`metadata`/`provenance` kinds have no discharge rule yet and
  fail closed; extending them is new predicates over the same machinery (§ "Every
  ADR-0005 mode is now a kind").

### Resolved since drafting

- Witness evaluation is wired (`xorq catalog run … -c … -f csv`); `VERIFIED`,
  `DISCREPANCY`, and `COULD-NOT-VERIFY` are all exercised end-to-end against a
  sample catalog (`src/pi_xorq_verifier/tests/test_discharge_integration.py`).
- The extension (`extensions/xorq.ts`) loads in a plain pi session via the
  package's `pi.extensions`, so the tools are available with no `pi-subagents`.
  (The earlier `subagentOnlyExtensions` path — validated against pi-subagents — is
  no longer used.)

### Update 2026-07-03 (op-tree verification — closes the maximality hole)

An adversarial review found the structural checks were regex/substring over the
`compose` *string*, so §3's formalization was thinner than stated. Fixed by
importing `xorq` and deciding the checks over the **operation graph** instead of
source text (`witness.py`, the only module that imports xorq):

- **Maximality is now discharged, not asserted.** The old code hardcoded
  `("maximality", True)`; a witness that pre-filtered the population could
  discharge a false superlative. The checker now recomputes `max`/`min` over the
  full (or `predicate.scope`-restricted) population and refutes any witness whose
  extremum is not the true one. The producer declares `predicate.metric_col` (the
  ranked column); the ∀-quantifier of §3 is genuinely checked — *over the
  declared population.* A non-empty `predicate.scope` shrinks that population, and
  the checker cannot read the prose to know the scope was intended, so a scoped
  extremum genuinely maximizes its scoped set while possibly contradicting the
  unscoped claim ("ORD is busiest" over `origin != 'ATL'`). To keep that from
  reading as an unconditional superlative, a scoped extremum discharges under the
  check name `maximality_within_scope` (never bare `maximality`), and the
  disclosed `scope` field carries the population it was judged over. Reconciling
  scope against the prose remains the correctness question the checker does not
  answer (see the soundness boundary in §5).
- **Witnesses are synthesized from the predicate** for
  `scalar`/`argmax`/`argmin`/`count`/`membership` via the ibis API — correct by
  construction (right direction, `limit(1)`, no smuggled filter). `witness.compose`
  is demoted to an escape hatch for the kinds the checker cannot synthesize, and
  is evaluated through xorq's AST-whitelisted `safe_eval`.
- **The §3 lints are now `walk_nodes` predicates over the op-tree**, so the
  surface-syntax bypasses are gone: circularity walks `ops.Equals`/`ops.InValues`
  literals (catching `.isin([lit])` and `lit == col`, which the regex missed);
  "selection-only" walks `ops.NumericBinary` (arithmetic, not comparisons);
  shape walks `ops.SortKey.descending`/`ops.Limit`/count reductions. The check
  named `witness_on_declared_alias` is now real — the witness's data leaves
  (`walk_nodes` over `Read`/`InMemoryTable`/…) must be a subset of the alias's,
  so a compose that fabricates or reads a foreign table is rejected.
- **Execution is in-process** (`Catalog.from_repo_path(...).load(alias)` →
  compose → `expr.execute()`); no subprocess, no CSV round-trip, so the expression
  inspected is the expression run.
- **A mis-declared type is unconfirmable, not a contradiction.** A surface that
  will not parse under its declared numeric `value_type` (a text code like `ATL`
  declared `int`) now yields `COULD-NOT-DISCHARGE` with a message pointing at
  `value_type`, instead of the old `REFUTED` — which was a *false* "the data
  contradicts you" for a true claim. `REFUTED` is now reserved for a value that
  parses and genuinely differs from its witness cell.

Still open (tracked separately, not addressed here): the coverage audit remains
self-attested (`reply_values` and obligation surfaces are both producer-authored;
no independent tokenizer in the trust root); `value_type.tolerance` is
producer-declared and unbounded — now *disclosed* on the certificate (`value_type`,
`scope`, `witness_hash`, `catalog_state`) so a widened acceptance is at least
visible and re-checkable, but not yet *capped*; and lineage/provenance is still
capped by the "a memtable reads as `InMemoryTable`, not its CSV" caveat above.

### Update 2026-07-03 (equal-bound circularity + deterministic grounding)

A soundness review found two verdict bugs in the decision procedure; both are
closed:

- **Equality spelled as bounds is now circular.** `_is_circular` walked only
  `Equals`/`InValues`, so `between(v, v)` — or opposing inequalities meeting at
  the claimed literal on the same column (`>= v` then `<= v`) — pinned a column
  to the claim without tripping the check. Through the ungrounded-scalar escape
  hatch that discharged any value that merely *exists* in a column. Bound
  comparisons (`Greater`/`GreaterEqual`/`Less`/`LessEqual`/`Between`) are now
  walked and a claimed literal bounding the same column from both sides is
  circular; one-sided bounds remain legitimate analysis parameters. Circularity
  targets now also include the claimed *entity* for `argmax`/`argmin`/
  `membership` — `filter(origin == 'ATL')` makes "ATL is top/present" its own
  witness by vacating the quantifier — while a grounded `scalar`, whose
  canonical witness must filter to its entity, stays exempt.
- **Scalar grounding is order-independent and grain-checked.** The
  `scalar`/`count`/`metric` predicate judged `run.rows[0]`, so a multi-row
  witness made the verdict depend on physical row order (a true claim could
  land `COULD-NOT` or `DISCHARGED` by luck of the sort). The entity's rows are
  now located wherever they sit, and a witness whose rows carry *distinct*
  values fails closed (`value_unambiguous: false` — the claim's grain does not
  match the witness grain) instead of comparing whichever cell came first.
- **The synthesis-beats-compose ordering is pinned as load-bearing.** A
  self-join compose can fabricate a row pairing the claimed entity with the
  true extremum value, which row-grounding cannot see through; it is blocked
  only because `_synthesize` wins whenever the metric resolves (and when it
  does not, `recompute_extremum` fails closed). That ordering is now documented
  in `build_witness` and locked by a regression test
  (`test_argmax_synthesis_wins_over_a_fabricating_compose`).
- **`scope` is now first-class in the producer tooling.** A live analyst run
  (answering "highest organic share among states with ≥25 markets") exposed the
  honest-side dual of the gerrymander: the analyst put the `≥25` filter in
  `witness.compose`, but a synthesized argmax ignores compose, so the superlative
  was recomputed against the *global* max (Puerto Rico, 100%, 2 markets) and
  refused — after which the analyst downgraded every ranking claim to a bare
  `scalar`, shipping the ranking unverified. Two fixes: the `assert_fact` tool
  now exposes a `scope` parameter (plumbed to `predicate.scope`), and a witness
  whose `compose` was discarded by synthesis now carries a `detail` note pointing
  at `predicate.scope` — so a dropped population filter is never silent. The
  analyst prompt states the rule: a scoped superlative's population restriction
  goes in `predicate.scope`, never `compose`.
- **Tabular answers verify as a `table`, and coverage credits the grid.** The
  same run printed a 10-row × 4-column table but checked only three cells with
  per-fact `assert_fact` and declared it "verified." Two fixes: the `table` kind
  (merged here) verifies the whole grid + ordering in one obligation, and the
  coverage audit now credits *every claimed cell* of a `table` obligation (not
  just its surface label) — previously a fully-discharged table folded to
  `COULD-NOT-VERIFY` because its cells looked uncovered. The analyst prompt now
  requires a `table` obligation for any ranking/grid and every rendered number in
  `reply_values`.
- **Selecting from a derived/cataloged metric is selection, not computation.**
  When the run finally cataloged its metric (`top-organic-share-by-state`, a
  `filter(...).mutate(organic_share=ovm*100/fm)...` alias), every `assert_fact`
  against it failed `selection_only` → `COULD-NOT-DISCHARGE`, because
  `_has_arithmetic` walked the alias's *own* definition and found the computed
  column's `NumericBinary`. That made every derived metric unverifiable — the
  opposite of the point of cataloging one. Fixed: the selection-only check now
  excludes arithmetic that is part of the declared alias's subtree and flags only
  what the *witness adds on top*. The "no arithmetic" rule constrains the
  checker/witness, never the upstream data pipeline (all real metrics compute).
  `xorq_verify` exposes `catalog_witnesses` and the prompt tells the analyst to
  persist a derived metric, so cataloging-then-verifying now works end-to-end.
- **Silent degradation is now loud (three runs, one theme).** When the happy
  path had friction the analyst quietly dropped to a weaker/no-verification path
  and shipped a confident answer: (a) a malformed `table` obligation
  (`rows: 10` instead of the row list, `scope: "x >= 25"` instead of
  `source.filter(...)`) dead-ended at a bare "ill-formed witness", so the analyst
  fell back to per-fact `assert_fact`; (b) with **no catalog at all**, the analyst
  hand-edited a stray root `catalog.yaml` (not a supported format — it errors),
  then computed the whole answer in raw `csv`/Python with zero verification and
  even a different filter. Fixes: `witness.build_error` names the specific
  malformation (bad scope, missing table rows, unknown column) in the
  `COULD-NOT-DISCHARGE` detail so an obligation self-corrects; the xorq-catalog
  skill gains a working *ingest* recipe (build → `xorq catalog add`) so "no alias
  yet" has an answer other than raw pandas; the root `catalog.yaml` is repurposed
  as a signpost to `.xorq/catalog`; and the analyst prompt's iron rule now forbids
  a raw-Python fallback and requires an unverifiable answer be labeled
  `UNVERIFIED`, never presented as fact. These are workflow/tooling fixes, not
  changes to the decision procedure — the checker only ever runs when invoked, so
  the remaining guarantee that an answer is *not* silently unverified is prompt
  discipline, not a deterministic gate.
- **A bare `count` discharges; a fabricated certificate is called out.** Once the
  analyst role was actually loaded (AGENTS.md — the dogfood repo had never run
  `pi-xorq-check init`, so earlier runs were a generic agent), a peptides count
  ("79 research-only") hit a real bug: a `count` witness names its cell `n` by
  synthesis, but discharge read `predicate.select` verbatim — omitted, so it
  looked up the empty column, got `None`, and returned a *misleading* "surface
  '79' is not interpretable as int". The analyst chased that dead-end and finally
  **hand-wrote a fake `{"verdict":"VERIFIED","confidence":"HIGH"}` certificate** —
  the worst failure, a claim of verification the checker never issued. Fixes:
  discharge now falls back to the sole result column when `select` is omitted (so
  a bare `count` verifies), reports an empty/absent cell as such instead of
  blaming the surface type, and the analyst prompt now forbids authoring/editing a
  certificate in the strongest terms (the `certificate` field must be the tool's
  verbatim JSON; never write `VERIFIED` yourself; an unverified answer says so).
  The deterministic gap stands: nothing stops a model from *printing* a fabricated
  certificate — only `pi-xorq-check gate` in CI, or a harness hook rejecting an
  answer whose certificate isn't a real tool result, can enforce it.

### Update 2026-07-03 (the population lives in the witness — `scope` removed)

`predicate.scope` is gone. A superlative/count's population — the universe it
ranks or counts over — now lives in `witness.compose` as a *restriction* of the
alias (a filter; empty ⇒ the whole alias). The checker builds the canonical
ranking/aggregation **on top of** that population, and the maximality recompute
(`recompute_extremum`) ranges over the **same** population via the shared
`_population` helper — so the judgment and its cross-check both derive from the
one witness, and can never diverge (the old split `scope`, re-evaluated
separately for the recompute, could). This is what "the judgment always comes
from the witness" means, made literal.

Soundness is preserved by constraining the population to a *clean restriction*:
`_clean_restriction` rejects a compose that adds a join (a self-join could pair
the claimed entity with a foreign extremum), a set op (could inject rows), a
limit (could pre-narrow the set the recompute maxes over), or witness-added
arithmetic — each judged as what the compose *adds on top of* the alias, so a
derived/cataloged alias's own joins/limits/computed columns don't count. A
population that is not clean fails synthesis → the obligation fails closed. This
is strictly stronger than the prior "synthesis ignores compose" rule: a
fabricating compose is now *refused*, not merely bypassed.

Verified end-to-end against the live catalog: a restricted superlative discharges
over its population (`maximality_within_scope`, the population visible in
`witness_code`); a false one refutes; a fabricating (cross_join) population fails
closed with a clear message. `scope` is removed from the request/certificate
schemas and `assert_fact`; a legacy `predicate.scope` is migrated into
`witness.compose` (same `source.filter(...)` form) for backward compatibility.
The `maximality_within_scope` check name is retained — "scope" now names the
situation (a restricted population), not a field.

### Update 2026-07-05 (the superlative smuggle — prose gating in the extension)

A live dogfood run demonstrated a smuggle the numeric answer gate cannot see: the
analyst verified both figures of a callout (scalar obligations, ✅ banner), then
appended "—the highest concentration among all U.S. states" — a superlative with
no number attached and no `argmax` obligation behind it. The claim was false
(Rhode Island leads at 7.6× California's rate), but every *figure* was discharged,
so the banner stamped ✅. The analyst prompt already required superlatives to be
declared as `argmax`; the run shows prompt discipline alone does not hold.

Fix, in the extension's answer gate (not the checker — the checker never sees
prose): terminal-answer prose is scanned for superlative/ranking wording
("highest", "busiest", "leads all", "second-largest", "no other …", "than any
other"), and the ✅ banner is refused unless some certificate this turn
**discharged an extremal obligation** — read off the checker's own check names
(`maximality`, `maximality_within_scope`, or a ranking `table`'s `ordered`), never
the request. The refusal names the matched wording and the fix (declare
`argmax`/`argmin`/`table`, or rephrase). Superlatives gate the answer even when it
states no number at all ("California leads the nation in organic access"). The
gate's pure logic moved to `extensions/lib/gate.ts` (dependency-free, excluded
from pi's extension discovery) with `node --test` coverage including a regression
built from the live smuggle (`tests/extension/gate.test.mjs`).

The honest boundary, same shape as §5: the detector is a lexeme heuristic demoted
to a coverage auditor — a match can only *refuse* the banner (fail closed), never
grant it — and the backing check is existence-level. Whether the discharged
extremum's *population* matches the prose's claimed universe ("highest among all
states" backed by a `maximality_within_scope` over a restricted set) remains the
correctness question the checker does not answer; the analyst prompt now requires
the scope be stated in the prose, and the certificate's `witness_code` carries the
population it was actually judged over.

### Update 2026-07-05 (contract trim — remove decorative and contradictory surface)

A review of which constraints are load-bearing (faithfulness or coverage) versus
decorative trimmed four things. None changes the decision procedure:

- **`answer.schema.json` is removed.** The `{answer, certificate}` envelope asked
  the model to re-emit a certificate — the one artifact a model can fabricate, and
  the exact failure documented above (the hand-written `VERIFIED`). The analyst
  prompt already forbids pasting a certificate; the durable record is the checker's
  certificate card, the persisted `verify-<id>` witnesses, and the gate's banner.
  A schema institutionalizing the model-emitted copy contradicted all three.
- **`predicate.maximality` is removed** from the request schema, `Predicate`, and
  the samples. The checker never read it (the quantifier is always discharged by
  recomputation over `metric_col`); a flag that is neither trusted nor required is
  contract noise that invites producers to believe setting it does something.
- **The legacy `predicate.scope` migration shim is removed** (`obligation_from_dict`
  no longer folds `scope` into `compose`). It was compatibility for a field that
  existed briefly pre-release; compat shims in the trust root must earn their place.
- **The unimplemented kinds (`compare`/`metric`/`metadata`/`provenance`) leave the
  request schema's enum** (contract only — `ClaimKind` keeps them and the checker
  still fails closed on them). Advertising kinds that can only dead-end reproduces
  the documented silent-degradation failure mode; each returns to the enum when its
  discharge rule lands.

Deliberately NOT trimmed, with reasons pinned: the maximality recompute (redundant
by construction under mandatory synthesis, but it is what makes `maximality` a
*discharged* check rather than an assumption about our own synthesis code, fails
closed if that invariant regresses, and catches engine tie/null-ordering quirks);
`NO-OP` as a gate-passing verdict (needed for answers asserting nothing checkable;
its abuse path is closed by the coverage downgrade); and the equal-bounds
circularity, `<in-memory>` leaf markers, and magic-constants scans (each closes a
demonstrated exploit).

### Update 2026-08-13 (witness site rename — the declared thing is not the witness)

The word "witness" was doing three jobs — the obligation field the *producer*
declares, the expression the checker *synthesizes* (`witness_code`), and the
persisted `verify-<id>` entry (`witness_ref`) — and the misnamed one was the
security-critical one: calling the producer-declared field `witness` suggested
the untrusted side writes the check, when the load-bearing property is exactly
that it does not. The contract is renamed to say what each part is; the
decision procedure is unchanged:

- **Request:** the nested `witness: {on, compose}` flattens into the obligation
  as `on` (the declared alias — the *site's base*) and `population` (the clean
  restriction the witness ranges over). `compose`'s second, overloaded role —
  the full expression for an *ungrounded scalar*, the one kind the checker
  cannot synthesize — splits out as `expression`, so a full expression can
  never pose as a population (or vice versa; the synthesized kinds fail closed
  rather than evaluate producer code as the witness, exactly as before).
- **Certificate:** `witness_alias` — a name that needed a "NOT the persisted
  witness" disclaimer in its own description — becomes `base_alias`. The parts
  that genuinely name the witness (`witness_code`, `witness_hash`,
  `witness_ref`) keep their names, as do the persisted `verify-<id>` entries.
- The checker's ungrounded-scalar `build_error` now self-explains ("needs
  `expression`") instead of returning nothing, closing the diagnostic gap the
  split exposed.

## References

- xorq-desktop: `docs/adr/0005-verification-engine-over-the-catalog.md`,
  `docs/architecture/verification-model.md`, `desktop/agents/verifier.md`.
- This repo: `src/pi_xorq_verifier/checker.py` (pure decision procedure) +
  `src/pi_xorq_verifier/witness.py` (xorq op-tree layer),
  `src/pi_xorq_verifier/prompts/analyst.md` (the single role prompt, shipped as
  package data; `pi-xorq-check init` writes it into a consumer's `AGENTS.md`),
  `extensions/xorq.ts` (tools), `schemas/` (request/certificate contracts).
- nicobailon/pi-subagents — the initial (since-removed) subagent/chain seam.
- Kurt Mehlhorn, *Certifying Algorithms* (with McConnell/Kratsch/Spinrad, SODA
  2003; survey with Näher et al.) — the frame §5 states soundness in: an
  untrusted solver must return `(y, w)` such that a simple, independently
  trusted checker decides `W(x, y, w)`, with an elementary proof of
  `W(x, y, w) ⟹ y = f(x)`. Here the untrusted solver is the LLM, `f` is
  expression evaluation over the pinned catalog, and the gate loop is the
  Las Vegas construction for a solver that resists being made certifying.
