"""xorq-backed witness layer for the ADR-0001 checker.

This is the **only** module that imports xorq. :mod:`checker` holds the pure
decision procedure; this module turns a declared obligation into a live xorq
expression, inspects its *operation graph* with ``walk_nodes`` (so structural
checks are decided over the semantic op-tree, not a regex over source text), and
runs it in-process against the catalog.

Two things follow from working over the op-tree instead of the ``compose``
string:

- The surface-syntax bypasses the old regex missed all vanish — ``.isin([lit])``
  and ``lit == col`` both lower to the same ``Equals``/``InValues`` nodes, and
  arithmetic is an ``ops.NumericBinary`` node however it is spelled.
- The expression the checker *inspects* is the expression it *runs* (same object,
  executed via ``expr.execute()``), closing the string↔execution gap.

Every entry point fails closed: it returns ``None`` (or an all-False check set)
when xorq is unavailable, an alias will not load, or an expression will not build
or run — so an unbuildable or unrunnable witness can never become a ``DISCHARGED``.
"""

from __future__ import annotations

import csv
import io
import re
import shutil
import subprocess
from functools import cache
from pathlib import Path


try:  # xorq is a hard dependency, but a failed import must fail closed, not crash.
    import xorq.expr.relations as _rel
    import xorq.vendor.ibis.expr.operations as _ops
    from xorq.catalog.bind import _eval_code
    from xorq.catalog.catalog import Catalog
    from xorq.common.utils.graph_utils import walk_nodes

    _AVAILABLE = True
    # Data-leaf node types for the provenance / alias-rooting check. ``walk_nodes``
    # descends opaque nodes (RemoteTable/CachedNode/Read), so these reach the real
    # physical inputs beneath the catalog's caching layers.
    _LEAF_TYPES: tuple[type, ...] = (
        _ops.InMemoryTable,
        _ops.DatabaseTable,
        _ops.UnboundTable,
        _rel.Read,
        _rel.RemoteTable,
    )
    # Count-shaped reductions a `count` witness may carry (a bare aggregate, not
    # arithmetic). Guarded because the exact set varies across xorq versions.
    _COUNT_OPS: tuple[type, ...] = tuple(
        t for t in (getattr(_ops, n, None) for n in ("CountStar", "CountDistinct", "Count"))
        if t is not None
    )
    # Population-fabricating / narrowing ops a witness may NOT add on top of the
    # alias: a join (incl. self-join) can pair the claimed entity with a foreign
    # extremum; a set op can inject rows; a Limit pre-narrows the population the
    # maximality recompute ranges over. Guarded (names vary across xorq versions).
    _FABRICATING_OPS: tuple[type, ...] = tuple(
        t for t in (
            getattr(_ops, n, None)
            for n in ("JoinChain", "JoinLink", "JoinReference", "Set", "Union", "Limit")
        )
        if isinstance(t, type)
    )
    # Bound comparisons for the equal-bounds circularity check: a claimed literal
    # that bounds the same column from both sides is `==` spelled differently.
    _LOWER_OPS: tuple[type, ...] = tuple(
        t for t in (getattr(_ops, n, None) for n in ("Greater", "GreaterEqual"))
        if t is not None
    )
    _UPPER_OPS: tuple[type, ...] = tuple(
        t for t in (getattr(_ops, n, None) for n in ("Less", "LessEqual"))
        if t is not None
    )
    _BETWEEN_OPS: tuple[type, ...] = tuple(
        t for t in (getattr(_ops, n, None) for n in ("Between",)) if t is not None
    )
except Exception:  # pragma: no cover - xorq absent → every entry point fails closed
    _AVAILABLE = False
    _LEAF_TYPES = ()
    _COUNT_OPS = ()
    _FABRICATING_OPS = ()
    _LOWER_OPS = ()
    _UPPER_OPS = ()
    _BETWEEN_OPS = ()


def available() -> bool:
    """Whether the xorq engine imported; when False every witness fails closed."""
    return _AVAILABLE


# --------------------------------------------------------------------------- #
# Loading + executing (the two boundaries that touch the live catalog)         #
# --------------------------------------------------------------------------- #

# Witnesses may be written with ibis's deferred ``_.col`` idiom; ``_eval_code``
# binds ``source`` (not ``_``), so rewrite a standalone ``_.`` to ``source.``
# while leaving identifiers like ``total_count`` untouched.
_DEFERRED = re.compile(r"(?<![\w.])_(?=\.)")


def normalize_compose(compose: str) -> str:
    """Rewrite the deferred ``_.col`` idiom to the ``source.col`` ``_eval_code`` binds."""
    return _DEFERRED.sub("source", compose)


@cache
def _catalog(catalog_path: str):
    return Catalog.from_repo_path(catalog_path)


def load_alias_expr(catalog_path: str, alias: str):
    """Resolve a declared alias to its catalog expression, or ``None`` (fail closed)."""
    if not _AVAILABLE or not alias:
        return None
    try:
        return _catalog(catalog_path).load(alias)
    except Exception:
        return None


def run_expr(expr):
    """Execute a witness in-process and return the ``WitnessRun`` grid, or ``None``.

    Cells are stringified so the pure checker compares them under its own typed
    equality; this module never interprets a value numerically.
    """
    if not _AVAILABLE or expr is None:
        return None
    from pi_xorq_verifier.checker import WitnessRun  # noqa: PLC0415

    try:
        df = expr.execute()
    except Exception:
        return None
    columns = tuple(str(c) for c in df.columns)
    rows = tuple(
        tuple((str(c), _cell(v)) for c, v in record.items())
        for record in df.to_dict("records")
    )
    return WitnessRun(columns, rows)


def _cell(v) -> str:
    if v is None or (isinstance(v, float) and v != v):  # None or NaN
        return ""
    return str(v)


# --------------------------------------------------------------------------- #
# The peek path (`pi-xorq-check select`) — compute, not verification.          #
# --------------------------------------------------------------------------- #

_HAS_LIMIT = re.compile(r"\.limit\s*\(")


def cached_select(
    catalog_path: str,
    alias: str,
    compose: str,
    limit: int = 50,
    cache_dir: str | None = None,
    use_cache: bool = True,
) -> str:
    """Compose ``compose`` on a snapshot-cached ``alias`` and return CSV text.

    The snapshot cache wraps the *alias* (the source), not the composed
    expression, so every peek on an alias shares one fetch of its sources — a
    second compose, however different, short-circuits to the local parquet.
    The verification path (:func:`run_expr`) never reads this cache: a witness
    always re-executes from the declared sources, so a snapshot can *propose*
    a number but can never *certify* one.

    ``cache_dir`` defaults to a ``select-cache`` directory beside the catalog,
    so the snapshots live and die with the catalog directory itself.

    Raises ``ValueError`` when the engine or alias is unavailable; compose and
    execution errors propagate for the caller to render.
    """
    if not _AVAILABLE:
        raise ValueError("xorq engine unavailable")
    alias_expr = load_alias_expr(catalog_path, alias)
    if alias_expr is None:
        raise ValueError(
            f"alias {alias!r} did not load from catalog {catalog_path!r} — "
            "declare it first (see xorq_catalog_list_aliases)"
        )
    source = _snapshot(alias_expr, catalog_path, cache_dir) if use_cache else alias_expr
    expr = _eval_code(normalize_compose(compose), source)
    if limit > 0 and not _HAS_LIMIT.search(compose):
        expr = expr.limit(limit)
    buf = io.StringIO()
    expr.execute().to_csv(buf, index=False, quoting=csv.QUOTE_NONNUMERIC)
    return buf.getvalue()


def _snapshot(alias_expr, catalog_path: str, cache_dir: str | None):
    from xorq.caching import ParquetSnapshotCache  # noqa: PLC0415

    base = (
        Path(cache_dir)
        if cache_dir
        else Path(catalog_path).resolve().parent / "select-cache"
    )
    return alias_expr.cache(cache=ParquetSnapshotCache.from_kwargs(base_path=str(base)))


# --------------------------------------------------------------------------- #
# Building the witness — synthesize the canonical query over the population.   #
#                                                                              #
# The obligation declares the witness SITE, never the witness: `on` (a         #
# declared alias) and `population` (a *restriction* of it — a filter — or the  #
# whole alias when empty). The checker adds the canonical ranking/aggregation  #
# on top, so a producer cannot smuggle a wrong sort or a pre-narrowed limit    #
# through. The SAME population feeds the maximality recompute                  #
# (`recompute_extremum`), so the cross-check is provably over exactly what the #
# witness ranked — the judgment and its check both come from the witness, with #
# nothing declared on the side. Only an ungrounded scalar (and the             #
# non-synthesized kinds) supplies a full expression, via `expression`.         #
# --------------------------------------------------------------------------- #


def build_witness(alias_expr, ob):
    """Build the witness for ``ob`` composed on ``alias_expr``.

    Synthesize the canonical expression over the population (``population``
    restriction, else the alias). ``None`` if it will not build. For the
    synthesizable kinds a failed synthesis fails closed — it never falls back to
    evaluating producer code as a whole witness, so a producer cannot bypass the
    canonical ranking. Only the non-synthesized kinds (an ungrounded scalar,
    ``compare``/``metric``) evaluate ``expression`` as the full witness.
    """
    from pi_xorq_verifier.checker import ClaimKind  # noqa: PLC0415

    if not _AVAILABLE or alias_expr is None:
        return None
    try:
        synthesized = _synthesize(alias_expr, ob)
        if synthesized is not None:
            return synthesized
        if ob.kind in (
            ClaimKind.ARGMAX,
            ClaimKind.ARGMIN,
            ClaimKind.COUNT,
            ClaimKind.MEMBERSHIP,
            ClaimKind.TABLE,
        ):
            return None  # fail closed — no raw-expression bypass of the canonical shape
        if not ob.expression:
            return None
        return _eval_code(normalize_compose(ob.expression), alias_expr)
    except Exception:
        return None


def build_error(alias_expr, ob) -> str:
    """A specific reason a witness would not build, for the ``COULD-NOT-DISCHARGE``
    detail — so a malformed obligation self-explains instead of dead-ending at a
    bare "ill-formed witness" (which made analysts abandon the structured path).
    Returns "" when no common malformation is found. Never raises."""
    from pi_xorq_verifier.checker import ClaimKind  # noqa: PLC0415

    if not _AVAILABLE:
        return "xorq engine unavailable"
    if alias_expr is None:
        return ""
    try:
        cols = tuple(alias_expr.columns)
    except Exception:
        cols = ()
    p = ob.predicate
    # An ungrounded scalar is the one kind whose witness is not synthesized: it
    # needs `expression` (the full expression). A population alone selects no
    # cell — the split exists so a full expression can never pose as a
    # population (or vice versa).
    if (
        ob.kind is ClaimKind.SCALAR
        and not ob.expression
        and not (p.entity_col and p.entity_val is not None)
    ):
        return (
            "an ungrounded scalar needs `expression` — the full expression whose "
            "cell is the claimed value, e.g. source.aggregate(total=source.n.sum()) "
            "— `population` only restricts the alias for the synthesized kinds; "
            "or ground the value with predicate.entity_col/entity_val"
        )
    # The declared `population` (a restriction of the alias) must compose on
    # `source` and stay a clean restriction (no join/set/limit/arith).
    if ob.population:
        try:
            pop = _population(alias_expr, ob.population)
        except Exception as exc:  # noqa: BLE001
            # The AST-whitelisted evaluator rejects `&`/`|` (bitwise), but ibis
            # filters use exactly those to combine conditions — so a compound
            # filter must be *chained*, not `&`-ed. Point at that specifically.
            if "&" in ob.population or "|" in ob.population:
                return (
                    "population combines conditions with `&`/`|`, which the "
                    "safe evaluator rejects — chain filters instead: "
                    "source.filter(a).filter(b) for AND (not "
                    "source.filter((a) & (b)))"
                )
            return (
                f"population did not compose on `source` — write it as a full "
                f"expression, e.g. source.filter(source.col >= 25), not "
                f"{ob.population!r} ({type(exc).__name__})"
            )
        if pop is not None and not _clean_restriction(pop, alias_expr):
            return (
                "population must be a restriction of the alias (a filter) — it "
                "may not add a join, set op, or limit, which would fabricate or "
                "pre-narrow the population the checker ranks/counts over"
            )
    if ob.kind is ClaimKind.TABLE:
        if not p.rows:
            return (
                "table predicate.rows must be a non-empty list of row objects "
                "{col: value}, one per row you display — not a row count and not "
                "omitted (this is the grid the checker compares against the witness)"
            )
        if p.columns:
            missing = tuple(c for c in p.columns if c not in cols)
            if missing:
                return f"predicate.columns {missing} not in alias columns {cols}"
        if p.ordered and not (p.metric_col and p.metric_col in cols):
            return (
                f"an ordered table (ranking) needs predicate.metric_col in the "
                f"alias to synthesize the top-k order; got {p.metric_col!r}, "
                f"alias columns {cols}"
            )
    if p.select and cols and p.select not in cols:
        return f"predicate.select {p.select!r} not in alias columns {cols}"
    return ""


def _population(alias_expr, restriction: str):
    """The population a witness ranges over: the alias restricted by the
    obligation's ``population`` (a filter), or the whole alias when empty.
    This is the single source of the population — used to *build* the witness and
    to *cross-check* it (the maximality recompute), so the two can never diverge."""
    if not restriction:
        return alias_expr
    return _eval_code(normalize_compose(restriction), alias_expr)


def _clean_restriction(pop_expr, alias_expr) -> bool:
    """A population must be a *restriction* of the alias: its data leaves are a
    subset of the alias's (rooted), and it adds no join/set/limit or arithmetic
    of its own. A join could pair the claimed entity with a foreign extremum, a
    set op inject rows, a limit pre-narrow the population the recompute maxes over
    — each defeats the maximality cross-check. The alias's *own* such ops (a
    derived/cataloged metric) are excluded; only what compose ADDS is judged."""
    if pop_expr is None:
        return False
    return (
        _rooted_on_alias(pop_expr, alias_expr)
        and not _adds(pop_expr, alias_expr, _FABRICATING_OPS)
        and not _has_arithmetic(pop_expr, alias_expr)
    )


def _is_numeric(expr, column: str) -> bool:
    try:
        return column in expr.columns and expr[column].type().is_numeric()
    except Exception:
        return False


def _metric_col(expr, ob) -> str | None:
    """The column an extremum ranges over: the declared ``metric_col``, else the
    selected column when it is itself numeric (the value-claim case)."""
    from pi_xorq_verifier.checker import ClaimKind  # noqa: PLC0415

    p = ob.predicate
    if p.metric_col:
        return p.metric_col
    if ob.kind in (ClaimKind.ARGMAX, ClaimKind.ARGMIN) and _is_numeric(expr, p.select):
        return p.select
    return None


def _synthesize(alias_expr, ob):
    """Canonical witness over the population, or ``None`` to fail closed / fall back.

    The population is ``population`` (a clean restriction of the alias) or
    the whole alias. Superlatives/counts require a *clean* restriction (no
    fabricating/narrowing op) so the checker's recompute is trustworthy; a
    population that is not clean returns ``None`` (fail closed, never a guess).
    """
    from pi_xorq_verifier.checker import ClaimKind  # noqa: PLC0415

    p = ob.predicate
    try:
        pop = _population(alias_expr, ob.population)
    except Exception:
        return None
    if pop is None or not _clean_restriction(pop, alias_expr):
        return None
    cols = pop.columns

    match ob.kind:
        case ClaimKind.ARGMAX | ClaimKind.ARGMIN:
            metric = _metric_col(pop, ob)
            if metric is None or metric not in cols:
                return None
            key = pop[metric]
            ordered = pop.order_by(
                key.desc() if ob.kind is ClaimKind.ARGMAX else key.asc()
            ).limit(1)
            select = tuple(
                c for c in dict.fromkeys((p.entity_col, p.select, metric)) if c and c in cols
            )
            return ordered.select(*select) if select else ordered
        case ClaimKind.COUNT:
            name = p.select or "n"
            return pop.aggregate(**{name: pop.count()})
        case ClaimKind.SCALAR:
            if p.entity_col and p.entity_val is not None and p.entity_col in cols:
                filtered = pop.filter(pop[p.entity_col] == p.entity_val)
                select = tuple(
                    c for c in dict.fromkeys((p.entity_col, p.select)) if c and c in cols
                )
                return filtered.select(*select) if select else filtered
            return None  # ungrounded scalar → escape hatch
        case ClaimKind.MEMBERSHIP:
            column = p.select or p.entity_col
            if not column or column not in cols:
                return None
            return pop.select(column)
        case ClaimKind.TABLE:
            select = tuple(c for c in p.columns if c in cols) or tuple(cols)
            if p.ordered:
                metric = p.metric_col
                if not metric or metric not in cols:
                    return None  # a ranking needs an order key
                return (
                    pop.order_by(pop[metric].desc())
                    .limit(len(p.rows))
                    .select(*select)
                )
            return pop.select(*select)  # set: population defines membership
        case _:
            return None  # compare/metric/metadata/provenance → escape hatch / fail closed


# --------------------------------------------------------------------------- #
# Op-tree validation (walk_nodes) — replaces the regex is_circular / _shape_ok  #
# --------------------------------------------------------------------------- #


def validate_witness(expr, alias_expr, ob) -> tuple[tuple[str, bool], ...]:
    """Structural well-formedness over the witness op-tree.

    Defense-in-depth on synthesized witnesses and the real gate on the compose
    escape hatch. Maximality is *not* here — it is discharged by re-running the
    extremum in :func:`recompute_extremum`, not by inspecting shape.
    """
    if not _AVAILABLE or expr is None:
        return (("witness_built", False),)
    return (
        ("witness_on_declared_alias", _rooted_on_alias(expr, alias_expr)),
        ("noncircular", not _is_circular(expr, ob)),
        ("selection_only", not _has_arithmetic(expr, alias_expr)),
        (f"shape:{ob.kind.value}", _shape_ok(expr, ob)),
    )


def _leaves(expr) -> frozenset:
    return frozenset(walk_nodes(_LEAF_TYPES, expr))


def _read_source(node) -> tuple[str, ...]:
    """The path(s)/URL a single Read leaf points at, from its ``read_kwargs``
    (``[[hash_path, "https://…"], [table_name, …]]`` in the serialized build).
    Probes the shared PATH_KEYS so it and the lineage checker never disagree on
    where a source is (a key one probed and the other did not was invisible)."""
    from pi_xorq_verifier.checker import PATH_KEYS  # noqa: PLC0415

    rk = getattr(node, "read_kwargs", None)
    try:
        kw = dict(rk) if rk is not None else {}
    except (TypeError, ValueError):
        kw = {}

    def _strs(v) -> tuple[str, ...]:
        if isinstance(v, str):
            return (v,)
        if isinstance(v, (tuple, list)):
            return tuple(str(x) for x in v if isinstance(x, str))
        return ()

    return tuple(s for key in PATH_KEYS for s in _strs(kw.get(key)))


def _leaf_marker(node) -> str:
    """A stable label for a leaf that exposes no source path — an in-memory table
    (hand-built data) or an opaque backend table. Surfaced so the lineage policy
    SEES it (an ``InMemoryTable`` is the strongest tell of fabricated data) instead
    of silently ignoring a sourceless leaf that is joined to a real source."""
    name = type(node).__name__
    if name == "InMemoryTable":
        return "<in-memory>"
    tname = getattr(node, "name", None)
    return f"<{name}:{tname}>" if tname else f"<{name}>"


def alias_sources(alias_expr) -> tuple[str, ...]:
    """The alias's ACTUAL lineage — one entry per op-tree leaf: the URL/path for a
    ``Read``, or a ``<marker>`` for a leaf with no source (an in-memory/hand-built
    table). Recovered from the catalog (not producer-declared), so a source-trust
    policy is grounded in what the catalog really re-runs — and no leaf is
    invisible: a hand-built table joined to a real source no longer hides behind
    it (the gap that let a hardcoded-population df pass)."""
    if not _AVAILABLE or alias_expr is None:
        return ()
    try:
        out = tuple(
            src
            for node in walk_nodes(_LEAF_TYPES, alias_expr)
            for src in (_read_source(node) or (_leaf_marker(node),))
        )
    except Exception:
        return ()
    return tuple(dict.fromkeys(out))  # dedup, preserve order


def _is_unit_scale(value) -> bool:
    """Whether a numeric literal is a legitimate constant in metric arithmetic — a
    small integer or a power of ten (unit scales: 100 for %, 100000 for per-100k,
    10 for rounding). A data value (a population, a price) is neither, so a literal
    that fails this is a suspected *fabricated* constant."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return True  # a non-numeric literal (a string label) is not a magic number
    if f != f or f == 0:  # NaN or 0
        return True
    a = abs(f)
    if a == int(a) and int(a) <= 1000:  # small integers
        return True
    import math  # noqa: PLC0415

    log = math.log10(a)
    return abs(log - round(log)) < 1e-9  # a power of ten (100, 1000, 0.01, ...)


def magic_constants(expr) -> tuple[str, ...]:
    """Non-unit numeric literals embedded as *data* in ``expr`` — the tell of a
    fabricated constant typed straight in (a hardcoded population/price), which a
    column reference (a ``Field``) would not be. Two carriers are inspected:

    * ``NumericBinary`` operands — a literal in arithmetic (``n / 37453038``);
    * a bare ``Literal`` *projected as a column value* (``mutate(pop=37453038)``,
      or an ungrounded scalar witness ``mutate(x=424242).select('x')``) — the same
      fabrication one projection removed from the arithmetic, which the operand
      walk alone misses.

    Filter thresholds (literals inside comparisons) and unit scales (100, 100000)
    are not flagged. Runs over the alias AND the witness expression, so a value
    fabricated in ``population`` cannot slip past the alias-only scan."""
    if not _AVAILABLE or expr is None:
        return ()
    try:
        out: list[str] = []
        for nb in walk_nodes(_ops.NumericBinary, expr):
            for operand in (getattr(nb, "left", None), getattr(nb, "right", None)):
                if isinstance(operand, _ops.Literal) and not _is_unit_scale(
                    getattr(operand, "value", None)
                ):
                    out.append(str(operand.value))
        project = getattr(_ops, "Project", None)
        if project is not None:
            for node in walk_nodes(project, expr):
                for value in (getattr(node, "values", {}) or {}).values():
                    if isinstance(value, _ops.Literal) and not _is_unit_scale(
                        getattr(value, "value", None)
                    ):
                        out.append(str(value.value))
    except Exception:
        return ()
    return tuple(dict.fromkeys(out))


def _rooted_on_alias(expr, alias_expr) -> bool:
    """The witness must read only the declared alias's physical inputs — its data
    leaves are a non-empty subset of the alias's (fabricated tables, or a compose
    that ignores ``source``, introduce a foreign leaf and fail this)."""
    witness_leaves = _leaves(expr)
    return bool(witness_leaves) and witness_leaves <= _leaves(alias_expr)


def _adds(expr, alias_expr, types) -> bool:
    """Whether ``expr`` contains a node of ``types`` that the alias does not — i.e.
    an op the *witness* added on top of the declared alias. The alias's own nodes
    (a derived/cataloged metric's computed columns, its upstream filters/limits)
    are excluded, so using such an alias as a base is not penalized; only what the
    witness adds is judged."""
    base = (
        frozenset(walk_nodes(types, alias_expr)) if alias_expr is not None else frozenset()
    )
    return any(n not in base for n in walk_nodes(types, expr))


def _has_arithmetic(expr, alias_expr=None) -> bool:
    """Whether the *witness* computes rather than selects the cell.

    Uses ``NumericBinary`` — comparisons (``Equals``/``Greater``) also subclass
    ``Binary`` but are legitimate in filters, so they are not caught. Arithmetic
    that is part of ``alias_expr``'s *own* definition (a derived/cataloged metric
    with a computed column) is **excluded**: selecting a cell from a computed
    alias is still selection — the alias is a declared, provenance-tracked
    expression, and re-flagging its upstream math made every derived metric
    unverifiable. Only arithmetic the witness adds *on top of* the alias counts."""
    return _adds(expr, alias_expr, _ops.NumericBinary)


def _is_circular(expr, ob) -> bool:
    """A witness that constrains a column to a *claimed* literal proves the value
    *exists*, not the *claim*. Caught: equality/membership constants, a
    ``between`` whose bound meets the claim, and a sandwich of opposing
    inequalities pinning the same column at the claimed literal (``==`` spelled
    as two filters). One-sided bounds stay legitimate analysis parameters.

    Two kinds of target:
    - the claimed **value** — the scalar ``surface`` and, for a ``table``, every
      grid cell. Pinning a column to a claimed value (``==``/``isin``/equal
      bounds) is always circular — it proves the value exists, not the claim.
    - the claimed **entity** (``argmax``/``argmin``/``membership``). For
      membership, *any* inclusion of the entity (``== ATL`` or ``isin([ATL,…])``)
      makes "ATL is present" trivially true. For an extremum, only a pin to the
      entity **alone** (``== ATL`` or single-element ``isin([ATL])``) is circular;
      a multi-value ``isin([ATL, ORD, …])`` is a legitimate comparison scope (ATL
      still had to win it), honestly reported as ``maximality_within_scope``. A
      grounded ``scalar``, whose canonical witness filters to its entity, is exempt.
    """
    from pi_xorq_verifier.checker import ClaimKind, normalize_value  # noqa: PLC0415

    def _norm(value: str, vt) -> object:
        try:
            return normalize_value(value, vt)
        except ValueError:
            return value.strip().casefold()

    # Collect the value targets AND every value-type they were normalized under.
    # A table pins a numeric column with an int/decimal literal even when the
    # obligation's own value_type is categorical, so the token must be tried under
    # each column's type — not just ob.value_type — or a Decimal target (12055)
    # never equals a categorical-normalized token ("12055") and the pin escapes.
    target_types = {ob.value_type}
    value_targets = {_norm(ob.surface, ob.value_type)}
    if ob.kind is ClaimKind.TABLE:
        vts = dict(ob.predicate.value_types)
        for row in ob.predicate.rows:
            for col, val in row:
                vt = vts.get(col, ob.value_type)
                target_types.add(vt)
                value_targets.add(_norm(val, vt))
    entity = (
        str(ob.predicate.entity_val).strip().casefold()
        if ob.kind in (ClaimKind.ARGMAX, ClaimKind.ARGMIN, ClaimKind.MEMBERSHIP)
        and ob.predicate.entity_val is not None
        else None
    )

    def matches_value(token: str) -> bool:
        candidates = {token.strip().casefold()} | {
            _norm(token, vt) for vt in target_types
        }
        return bool(candidates & value_targets)

    def is_entity(token: str) -> bool:
        return entity is not None and token.strip().casefold() == entity

    for node in walk_nodes((_ops.Equals, _ops.InValues), expr):
        lits = list(walk_nodes(_ops.Literal, node))
        if any(matches_value(str(lit.value)) for lit in lits):
            return True
        if entity is not None and any(is_entity(str(lit.value)) for lit in lits):
            if isinstance(node, _ops.Equals):
                return True  # col == entity → the population is pinned to it
            # isin(...): membership inclusion is circular; an extremum is only
            # pinned by a single-element isin([entity]) — multi = comparison scope.
            if ob.kind is ClaimKind.MEMBERSHIP or len(getattr(node, "options", ()) or ()) <= 1:
                return True
    # Equal-bounds detection (value targets only — entity pins are categorical):
    # a column bounded from both sides at the claimed value is `==` in disguise.
    lower_cols: set[str | None] = set()
    upper_cols: set[str | None] = set()
    for node in walk_nodes(_LOWER_OPS + _UPPER_OPS, expr):
        for lit, col, flipped in (
            (node.right, node.left, False),
            (node.left, node.right, True),
        ):
            if isinstance(lit, _ops.Literal) and matches_value(str(lit.value)):
                is_lower = isinstance(node, _LOWER_OPS) != flipped
                (lower_cols if is_lower else upper_cols).add(
                    getattr(col, "name", None)
                )
    for node in walk_nodes(_BETWEEN_OPS, expr):
        col = getattr(getattr(node, "arg", None), "name", None)
        for bound, bucket in (
            (getattr(node, "lower_bound", getattr(node, "lower", None)), lower_cols),
            (getattr(node, "upper_bound", getattr(node, "upper", None)), upper_cols),
        ):
            if isinstance(bound, _ops.Literal) and matches_value(str(bound.value)):
                bucket.add(col)
    return bool(lower_cols & upper_cols)


def _shape_ok(expr, ob) -> bool:
    from pi_xorq_verifier.checker import ClaimKind  # noqa: PLC0415

    match ob.kind:
        case ClaimKind.ARGMAX:
            return bool(walk_nodes(_ops.Limit, expr)) and any(
                sk.descending for sk in walk_nodes(_ops.SortKey, expr)
            )
        case ClaimKind.ARGMIN:
            return bool(walk_nodes(_ops.Limit, expr)) and any(
                not sk.descending for sk in walk_nodes(_ops.SortKey, expr)
            )
        case ClaimKind.COUNT:
            return bool(_COUNT_OPS) and bool(walk_nodes(_COUNT_OPS, expr))
        case ClaimKind.TABLE:
            # a ranking must be ordered + limited; a set is just a projection
            if ob.predicate.ordered:
                return bool(walk_nodes(_ops.Limit, expr)) and bool(
                    walk_nodes(_ops.SortKey, expr)
                )
            return True
        case _:
            return True


# --------------------------------------------------------------------------- #
# Maximality — discharged by recomputing the extremum, not asserted             #
# --------------------------------------------------------------------------- #


def recompute_extremum(alias_expr, ob) -> str | None:
    """The true extremum of the metric over the witness's population, as text.

    The population is the *same* one the witness ranks — ``population`` (a
    clean restriction of the alias) or the whole alias — so this cross-check is
    provably over exactly what the witness ranked, with nothing declared on the
    side. An argmax claim holds only if the witness's extremum cell equals the
    ``max`` (or ``min``) recomputed here; a witness that pre-filtered below its
    population lands a smaller extremum and is refuted. ``None`` (fail closed) if
    the population is not a clean restriction or will not run.
    """
    if not _AVAILABLE or alias_expr is None:
        return None
    from pi_xorq_verifier.checker import ClaimKind  # noqa: PLC0415

    metric = _metric_col(alias_expr, ob)
    if metric is None:
        return None
    try:
        pop = _population(alias_expr, ob.population)
        if pop is None or not _clean_restriction(pop, alias_expr):
            return None
        if metric not in pop.columns:
            return None
        col = pop[metric]
        agg = pop.aggregate(m=col.max() if ob.kind is ClaimKind.ARGMAX else col.min())
        df = agg.execute()
    except Exception:
        return None
    if df.empty:
        return None
    return _cell(df["m"].iloc[0])


# --------------------------------------------------------------------------- #
# Cataloging a verified witness (ADR-0001 §4)                                  #
# --------------------------------------------------------------------------- #
#
# A DISCHARGED witness can be persisted as a first-class *composed* catalog entry
# so the verified value is re-derivable by anyone (`xorq catalog run verify-<id>`).
# `witness_code` renders the witness as `xorq catalog compose -c` code — the
# canonical query built over the population (``population`` or the alias).
# Reductions use ``xo._`` (the deferred current relation), because a restricted
# ``source.count()`` binds to the wrong relation and errors; ``xo._.count()``
# counts the population's rows.


def _select_code(cols: tuple[str, ...]) -> str:
    return f".select({', '.join(repr(c) for c in cols)})" if cols else ""


def witness_code(alias_expr, ob) -> str | None:
    """Compose-code for the witness that discharges ``ob``, or ``None`` if it
    cannot be expressed as ``compose`` code (compare/metric with no compose)."""
    if not _AVAILABLE or alias_expr is None:
        return None
    from pi_xorq_verifier.checker import ClaimKind  # noqa: PLC0415

    p = ob.predicate
    try:
        cols = tuple(alias_expr.columns)
    except Exception:
        return None
    # The population base: the declared restriction, or the whole alias. The
    # escape hatch (ungrounded scalar / non-synthesized kinds) is the producer's
    # full `expression` — the one place producer code IS the witness.
    base = normalize_compose(ob.population) if ob.population else "source"
    escape = normalize_compose(ob.expression) if ob.expression else None
    match ob.kind:
        case ClaimKind.ARGMAX | ClaimKind.ARGMIN:
            metric = _metric_col(alias_expr, ob)
            if metric is None or metric not in cols:
                return None
            direction = "desc" if ob.kind is ClaimKind.ARGMAX else "asc"
            sel = tuple(
                c for c in dict.fromkeys((p.entity_col, p.select, metric)) if c and c in cols
            )
            # column ref in order_by resolves against the population relation; a
            # reduction would not, hence xo._ is reserved for count() below.
            return (
                f"{base}.order_by(source.{metric}.{direction}()).limit(1)"
                + _select_code(sel)
            )
        case ClaimKind.COUNT:
            name = p.select or "n"
            return f"{base}.aggregate({name}=xo._.count())"
        case ClaimKind.SCALAR:
            if p.entity_col and p.entity_val is not None and p.entity_col in cols:
                sel = tuple(
                    c for c in dict.fromkeys((p.entity_col, p.select)) if c and c in cols
                )
                return (
                    f"{base}.filter(source.{p.entity_col} == {p.entity_val!r})"
                    + _select_code(sel)
                )
            return escape
        case ClaimKind.MEMBERSHIP:
            column = p.select or p.entity_col
            return f"{base}.select({column!r})" if column and column in cols else None
        case ClaimKind.TABLE:
            # Emit the full query the checker synthesized and ran — the ranking,
            # not just the population base — so witness_code round-trips (a
            # reviewer can re-run it and get the same grid).
            sel = tuple(c for c in p.columns if c in cols) or tuple(cols)
            if p.ordered:
                if not p.metric_col or p.metric_col not in cols:
                    return None
                return (
                    f"{base}.order_by(source.{p.metric_col}.desc())"
                    f".limit({len(p.rows)})" + _select_code(sel)
                )
            return f"{base}{_select_code(sel)}"
        case _:
            return escape


def _remove_alias(xorq: str, catalog_path: str, alias: str) -> None:
    """Best-effort removal of an alias we created but could not confirm — so a
    failed round-trip never leaves an orphan ``verify-<id>`` in the catalog."""
    try:
        subprocess.run(
            (xorq, "catalog", "-p", catalog_path, "remove-alias", alias),
            capture_output=True, text=True, timeout=WITNESS_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def catalog_witness(
    catalog_path: str, on: str, code: str, alias: str, expected_cell, select: str,
    expected_rows: int | None = None,
) -> str | None:
    """Persist the witness as a composed entry and confirm it reproduces the result.

    Runs ``xorq catalog compose <on> -c <code> -a <alias>``, then re-runs the
    persisted entry and checks it reproduces the verified result — the selected
    cell for a value kind, or ``expected_rows`` for a ``table`` (whose content is
    the grid, not a cell). Returns the alias only when the entry both catalogs AND
    reproduces; **if it does not, the alias we just created is removed** so the
    catalog never gains a ``verify-<id>`` the certificate disclaims. Fail-soft:
    any error → ``None`` (cataloging is opt-in and must never affect the verdict).
    """
    xorq = shutil.which("xorq")
    if not (_AVAILABLE and xorq and on and code):
        return None
    try:
        add = subprocess.run(
            (xorq, "catalog", "-p", catalog_path, "compose", on, "-c", code,
             "-a", alias, "--no-sync"),
            capture_output=True, text=True, timeout=WITNESS_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    blob = add.stdout + add.stderr
    if add.returncode != 0 and "already exists" not in blob:
        return None
    # Only clean up an alias *we* created (never one that pre-existed).
    created = add.returncode == 0 and "already exists" not in blob

    def _fail() -> None:
        if created:
            _remove_alias(xorq, catalog_path, alias)
        return None

    # round-trip: the persisted witness must re-derive the verified result
    try:
        runp = subprocess.run(
            (xorq, "catalog", "-p", catalog_path, "run", alias,
             "-o", "-", "-f", "csv", "--use-this-venv"),
            capture_output=True, text=True, timeout=WITNESS_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return _fail()
    if runp.returncode != 0:
        return _fail()
    rows = list(csv.DictReader(io.StringIO(runp.stdout)))
    if not rows:
        return _fail()
    if expected_rows is not None:  # table: the grid, checked by row count
        ok = len(rows) == expected_rows
    else:  # value kind: the selected cell must reproduce
        cell = rows[0].get(select) if select else next(iter(rows[0].values()), None)
        ok = cell is not None and str(cell).strip() == str(expected_cell).strip()
    if not ok:
        return _fail()
    return alias


WITNESS_TIMEOUT_S = 180
