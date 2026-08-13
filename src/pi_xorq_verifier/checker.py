"""Deterministic verification checker — the ADR-0001 decision procedure.

A claim is a *proof obligation*: a judgment ``⟦witness⟧_catalog ⊨ φ`` that pairs a
verbatim surface value from an answer with an expression composed over declared
catalog aliases and a decidable predicate its result must satisfy. This module
implements the pure, deterministic core of discharging obligations into a
re-checkable certificate.

:func:`discharge` runs the full decision procedure of ADR-0001 §2: it evaluates
the witness against the live catalog (``xorq catalog run``), selects the claimed
cell, checks the kind's predicate φ (row-grounding for extrema, membership), the
typed-equality faithfulness relation ``≡_ε``, and the provenance constraint
against declared lineage. Every step fails closed to ``COULD-NOT-DISCHARGE`` — a
witness that will not evaluate, a cell that is not selectable, or a provenance
that cannot be confirmed never becomes a ``DISCHARGED``, so the aggregate verdict
can never be a false pass. The checker is *selection-only*: it shells to the xorq
engine to run the query and reads cells from the result; it never computes them.
"""

from __future__ import annotations

import hashlib
import os
from collections import Counter
from decimal import Decimal, InvalidOperation
from enum import Enum

from attr import evolve, field, frozen


class Verdict(str, Enum):
    """Turn-level verdict. ``COULD-NOT-VERIFY`` is the conservative top."""

    VERIFIED = "VERIFIED"
    DISCREPANCY = "DISCREPANCY"
    COULD_NOT_VERIFY = "COULD-NOT-VERIFY"
    NO_OP = "NO-OP"


class ObligationStatus(str, Enum):
    """Per-obligation status on the lattice DISCHARGED ⊏ COULD-NOT ⊏ REFUTED."""

    DISCHARGED = "DISCHARGED"
    COULD_NOT_DISCHARGE = "COULD-NOT-DISCHARGE"
    REFUTED = "REFUTED"


class ClaimKind(str, Enum):
    """The predicate shape an obligation carries (ADR-0001 §1)."""

    SCALAR = "scalar"
    ARGMAX = "argmax"
    ARGMIN = "argmin"
    COMPARE = "compare"
    COUNT = "count"
    MEMBERSHIP = "membership"
    TABLE = "table"  # a claimed grid (ranking / row / set) vs the witness result
    METRIC = "metric"
    METADATA = "metadata"
    PROVENANCE = "provenance"


@frozen(auto_attribs=True)
class ValueType:
    """Typed value model. ``tolerance`` is a Decimal string ε for numeric kinds."""

    kind: str = "int"  # int | decimal | percent | currency | date | categorical
    tolerance: str = "0"


@frozen(auto_attribs=True)
class Predicate:
    """The relation the witness result must satisfy."""

    select: str = ""  # column selected from the witness result
    entity_col: str | None = None
    entity_val: str | None = None
    # There is no `maximality` flag: the extremum quantifier is never declared,
    # always discharged by recomputing max/min over `metric_col` (a flag the
    # checker neither trusted nor required was pure contract noise).
    metric_col: str | None = None  # column an extremum ranges over (argmax/argmin/table)
    # The population a superlative/count ranges over is NOT a separate field — it
    # lives in the obligation's `population` (a restriction of the alias), so the
    # judgment and its cross-check both derive from the one witness.
    # table claims: the claimed grid compared against the witness result.
    columns: tuple[str, ...] = ()  # columns compared (and, for a ranking, display order)
    rows: tuple[tuple[tuple[str, str], ...], ...] = ()  # claimed rows as (col, value) pairs
    ordered: bool = True  # table: ranking (positional) vs set (order-insensitive)
    value_types: tuple[tuple[str, ValueType], ...] = ()  # per-column types for the grid


@frozen(auto_attribs=True)
class Obligation:
    """A declared claim paired with the site the checker witnesses it on.

    The producer declares the *witness site* — ``on`` (a declared alias),
    ``population`` (a clean restriction of it), and the predicate. The witness
    itself is synthesized by the checker from that declaration; only an
    ungrounded scalar supplies the full expression via ``expression``.
    """

    id: str
    kind: ClaimKind
    surface: str
    predicate: Predicate
    on: str = ""  # declared alias the witness is composed on (the site)
    population: str = ""  # restriction of the alias the witness ranges over
    expression: str = ""  # ungrounded scalar only: the full expression to evaluate
    value_type: ValueType = field(factory=ValueType)
    requires_sources: tuple[str, ...] = ()


@frozen(auto_attribs=True)
class ObligationResult:
    id: str
    status: ObligationStatus
    checks: tuple[tuple[str, bool], ...] = ()
    selected_cell: str | None = None
    base_alias: str | None = None  # the declared alias the witness composed on
    detail: str = ""
    witness_code: str = ""  # the compose code that discharges this (re-runnable)
    witness_ref: str = ""  # cataloged alias of the verified witness, if persisted
    # Transparency: the producer-declared knobs that shaped acceptance, echoed
    # onto the result so the certificate is self-contained and re-checkable —
    # a consumer can see *what* was checked, *over which population*, and *how
    # loosely* without holding the original request (ADR-0001 §4).
    surface: str = ""  # the claimed value verbatim (what typed-equality compared)
    tolerance: str = ""  # the ε the typed-equality (≡_ε) admitted
    value_kind: str = ""  # declared value type (tolerance semantics depend on it)
    witness_hash: str = ""  # content handle for witness_code (integrity / dedup)
    # For a `table` obligation the verified content is the grid, not a single
    # cell — echo it so the certificate records *what* was confirmed (the rows),
    # rather than a bogus scalar. Empty for the value kinds.
    table_columns: tuple[str, ...] = ()
    table_rows: tuple[tuple[tuple[str, str], ...], ...] = ()
    # The alias's ACTUAL sources (URL/path per Read leaf), recovered from the
    # catalog op-tree — the grounded lineage the source-trust policy judges.
    sources: tuple[str, ...] = ()


@frozen(auto_attribs=True)
class Certificate:
    verdict: Verdict
    results: tuple[ObligationResult, ...]
    uncovered: tuple[str, ...] = ()
    soundness: str = "faithful-to-declared-expressions; not a correctness re-derivation"
    catalog_state: str = ""  # content handle for the catalog this was checked against


# --------------------------------------------------------------------------- #
# Typed value semantics (pure)                                                #
# --------------------------------------------------------------------------- #


def _to_decimal(s: str) -> Decimal:
    cleaned = s.strip().lstrip("$€£").replace(",", "").replace("_", "")
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"not a numeric surface: {s!r}") from exc


def normalize_value(surface: str, value_type: ValueType) -> Decimal | str:
    """Total surface→value normalization keyed on the declared type."""
    match value_type.kind:
        case "int" | "decimal" | "currency":
            return _to_decimal(surface)
        case "percent":
            body = surface.strip().rstrip("%")
            scale = Decimal(100) if "%" in surface else Decimal(1)
            return _to_decimal(body) / scale
        case _:
            return surface.strip().casefold()


def values_match(surface: str, cell: str, value_type: ValueType) -> bool:
    """Typed equality with declared tolerance (``≡_ε`` in ADR-0001 §2)."""
    match value_type.kind:
        case "int" | "decimal" | "currency" | "percent":
            # Numeric kinds: compare under tolerance. Any non-numeric surface,
            # cell, or malformed tolerance fails closed rather than crashing the
            # whole request (a producer-invented kind lands in `case _`).
            try:
                a = Decimal(normalize_value(surface, value_type))
                b = Decimal(normalize_value(cell, value_type))
                return abs(a - b) <= Decimal(value_type.tolerance)
            except (ValueError, InvalidOperation):
                return False
        case _:
            # categorical, date, and any unrecognized/producer-invented kind:
            # exact (casefolded) equality — never a numeric parse.
            return normalize_value(surface, value_type) == normalize_value(
                cell, value_type
            )


# --------------------------------------------------------------------------- #
# Witness evaluation against the live catalog (selection-only)                 #
# --------------------------------------------------------------------------- #
#
# Building, op-tree validation, and in-process execution live in
# :mod:`pi_xorq_verifier.witness` (the only module that imports xorq). The
# checker orchestrates them and stays pure and dependency-light; it is imported
# lazily inside :func:`discharge` so this module loads even when xorq does not.


@frozen(auto_attribs=True)
class WitnessRun:
    """The grid an evaluated witness returns: column names and selected rows."""

    columns: tuple[str, ...]
    rows: tuple[tuple[tuple[str, str], ...], ...]

    def value(self, row: tuple[tuple[str, str], ...], column: str) -> str | None:
        return dict(row).get(column)

    def column_values(self, column: str) -> tuple[str | None, ...]:
        return tuple(dict(row).get(column) for row in self.rows)


def _categorical_eq(a: object, b: object) -> bool:
    cat = ValueType(kind="categorical")
    return normalize_value(str(a), cat) == normalize_value(str(b), cat)


def _typed_faithfulness(
    surface: str, cell: str | None, value_type: ValueType
) -> tuple[bool, bool]:
    """Return ``(comparable, value_ok)`` for the faithfulness relation ``≡_ε``.

    A surface (or cell) that will not parse under a *numeric* ``value_type`` — a
    text code declared ``int``, say — is **not comparable**: the claim is
    unconfirmable (the type was mis-declared), which the caller maps to
    ``COULD-NOT-DISCHARGE``. It is *not* a contradiction, so it must never become
    ``REFUTED`` — that would falsely assert the data disagrees with a true claim.
    """
    if cell is None:
        return False, False
    if value_type.kind not in ("categorical", "date"):
        try:
            normalize_value(surface, value_type)
            normalize_value(cell, value_type)
        except ValueError:
            return False, False
    return True, values_match(surface, cell, value_type)


def _uninterpretable_detail(ob: Obligation) -> str:
    return (
        f"surface {ob.surface!r} is not interpretable as {ob.value_type.kind} — "
        "declare a value_type matching the cell (e.g. 'categorical' for a code/label)"
    )


def _discharge_table(
    ob: Obligation, run: WitnessRun
) -> tuple[ObligationStatus, tuple[tuple[str, bool], ...], str | None, str]:
    """Verify a claimed grid against the witness result (ADR-0001 table kind).

    ``ordered`` → compare row-by-row by position (a ranking / a single row);
    otherwise compare as an order-insensitive multiset. Each cell is checked under
    its column's ``value_type`` (``predicate.value_types``, else the obligation's).
    A claimed cell that cannot be read under its type is unconfirmable
    (``COULD-NOT``, never a false ``REFUTED``).
    """
    p = ob.predicate
    columns = p.columns or run.columns
    types = dict(p.value_types)

    def vt(col: str) -> ValueType:
        return types.get(col, ob.value_type)

    missing = tuple(c for c in columns if c not in run.columns)
    if missing:
        return (
            ObligationStatus.COULD_NOT_DISCHARGE,
            (("columns_present", False),),
            None,
            f"claimed columns {missing} not in witness result {run.columns}",
        )
    claimed = [dict(r) for r in p.rows]
    result = [dict(r) for r in run.rows]

    if p.ordered:
        if len(claimed) != len(result):
            return (
                ObligationStatus.REFUTED,
                (("row_count", False),),
                None,  # a table has no single "selected cell"; the grid is the content
                f"claimed {len(claimed)} rows; witness returned {len(result)}",
            )
        for i, (cl, rs) in enumerate(zip(claimed, result)):
            for c in columns:
                comparable, ok = _typed_faithfulness(str(cl.get(c, "")), rs.get(c), vt(c))
                if not comparable:
                    return (
                        ObligationStatus.COULD_NOT_DISCHARGE,
                        (("cells_comparable", False),),
                        None,
                        f"row {i} column {c!r} not interpretable as {vt(c).kind}",
                    )
                if not ok:
                    return (
                        ObligationStatus.REFUTED,
                        (("cells_match", False),),
                        None,
                        f"row {i} column {c!r}: claimed {cl.get(c)!r} vs {rs.get(c)!r}",
                    )
        return (
            ObligationStatus.DISCHARGED,
            (("row_count", True), ("ordered", True), ("cells_match", True)),
            None,  # table content is the grid (emitted separately), not a cell
            "",
        )

    # order-insensitive set: canonicalize each row to a typed key, compare multisets
    def key(row: dict) -> tuple | None:
        parts: list[str] = []
        for c in columns:
            v = row.get(c)
            if v is None:
                return None
            try:
                parts.append(str(normalize_value(str(v), vt(c))))
            except ValueError:
                return None
        return tuple(parts)

    claimed_keys = [key(r) for r in claimed]
    result_keys = [key(r) for r in result]
    if None in claimed_keys or None in result_keys:
        return (
            ObligationStatus.COULD_NOT_DISCHARGE,
            (("cells_comparable", False),),
            None,
            "a claimed cell is not present or not interpretable under its type",
        )
    if Counter(claimed_keys) == Counter(result_keys):
        return (
            ObligationStatus.DISCHARGED,
            (("set_equal", True), ("row_count", True)),
            None,  # table content is the grid (emitted separately), not a cell
            "",
        )
    return (
        ObligationStatus.REFUTED,
        (("set_equal", False),),
        None,
        f"claimed set of {len(claimed)} does not equal the witness set of {len(result)}",
    )


def _discharge_predicate(
    ob: Obligation, run: WitnessRun
) -> tuple[ObligationStatus, tuple[tuple[str, bool], ...], str | None, str]:
    """Apply the predicate φ + faithfulness for the obligation's kind."""
    if ob.kind is ClaimKind.TABLE:
        return _discharge_table(ob, run)
    sel = ob.predicate.select
    if not run.rows:
        return (
            ObligationStatus.COULD_NOT_DISCHARGE,
            (("nonempty_result", False),),
            None,
            "witness returned no rows",
        )
    if sel and sel not in run.columns:
        return (
            ObligationStatus.COULD_NOT_DISCHARGE,
            (("select_in_result", False),),
            None,
            f"select column {sel!r} not in witness result {run.columns}",
        )
    match ob.kind:
        case ClaimKind.SCALAR | ClaimKind.COUNT | ClaimKind.METRIC:
            grounds_entity = (
                ob.predicate.entity_col is not None
                and ob.predicate.entity_val is not None
            )
            # If the claim grounds the value to an entity, judge the entity's
            # row(s) wherever they sit in the result — never rows[0], whose
            # identity depends on physical row order. A witness with no row for
            # the entity is unconfirmable, not a contradiction, so fail closed
            # rather than DISCHARGE or REFUTE.
            if grounds_entity and ob.predicate.entity_col not in run.columns:
                return (
                    ObligationStatus.COULD_NOT_DISCHARGE,
                    (("row_grounding", False), ("typed_eq", False)),
                    None,
                    f"entity column {ob.predicate.entity_col!r} not co-selected in "
                    "the witness result — cannot ground the value to the entity",
                )
            rows = (
                tuple(
                    row
                    for row in run.rows
                    if _categorical_eq(
                        run.value(row, ob.predicate.entity_col),
                        ob.predicate.entity_val,
                    )
                )
                if grounds_entity
                else run.rows
            )
            if not rows:
                return (
                    ObligationStatus.COULD_NOT_DISCHARGE,
                    (("row_grounding", False), ("typed_eq", False)),
                    None,
                    "no witness row is the claimed entity's — cannot ground the "
                    "value to the entity",
                )
            grounding = (("row_grounding", True),) if grounds_entity else ()
            # Resolve the value column. A `count` witness names its cell ``n`` by
            # synthesis, but a producer need not know that — so when ``select`` is
            # omitted and the (ungrounded) result has a single column, read it.
            # This is why a bare `count` returned a misleading "not interpretable"
            # before: discharge looked up the empty column and found nothing.
            col = sel or (run.columns[0] if len(run.columns) == 1 else "")
            if not col or col not in run.columns:
                return (
                    ObligationStatus.COULD_NOT_DISCHARGE,
                    grounding + (("select_in_result", False),),
                    None,
                    "declare predicate.select naming the value column; witness "
                    f"result columns are {run.columns}",
                )
            # "The value" must be one value: rows carrying distinct cells make
            # the claim's grain ambiguous, and which cell gets compared would
            # depend on physical row order — fail closed, never roll that die.
            cells = tuple(dict.fromkeys(run.value(row, col) for row in rows))
            if len(cells) > 1:
                return (
                    ObligationStatus.COULD_NOT_DISCHARGE,
                    grounding + (("value_unambiguous", False),),
                    None,
                    f"witness rows hold {len(cells)} distinct {col!r} values — the "
                    "claim's grain does not match the witness grain (aggregate or "
                    "narrow the witness to one value)",
                )
            cell = cells[0]
            if cell is None or cell == "":
                return (
                    ObligationStatus.COULD_NOT_DISCHARGE,
                    grounding + (("typed_eq", False),),
                    cell,
                    f"witness cell for column {col!r} is empty/null — nothing to "
                    "compare against the claimed value",
                )
            comparable, value_ok = _typed_faithfulness(ob.surface, cell, ob.value_type)
            # A surface that cannot be read under its declared type is unconfirmable,
            # not a contradiction — fail closed rather than REFUTE a true claim.
            if not comparable:
                return (
                    ObligationStatus.COULD_NOT_DISCHARGE,
                    grounding + (("typed_eq", False),),
                    cell,
                    _uninterpretable_detail(ob),
                )
            checks = grounding + (("typed_eq", value_ok),)
            status = (
                ObligationStatus.DISCHARGED if value_ok else ObligationStatus.REFUTED
            )
            return (
                status,
                checks,
                cell,
                "" if value_ok else "selected cell contradicts the claimed value",
            )
        case ClaimKind.ARGMAX | ClaimKind.ARGMIN:
            row = run.rows[0]  # order_by(...).limit(1) ⇒ the extremum row
            cell = run.value(row, sel)
            comparable, value_ok = _typed_faithfulness(ob.surface, cell, ob.value_type)
            grounds_entity = (
                ob.predicate.entity_col is not None
                and ob.predicate.entity_val is not None
            )
            if grounds_entity and ob.predicate.entity_col not in run.columns:
                # The witness did not co-select the entity column, so grounding
                # cannot be evaluated. That is unconfirmable, not a contradiction:
                # fail closed rather than REFUTE (which would be a false
                # DISCREPANCY on an otherwise-true claim).
                return (
                    ObligationStatus.COULD_NOT_DISCHARGE,
                    (("row_grounding", False), ("typed_eq", value_ok)),
                    cell,
                    f"entity column {ob.predicate.entity_col!r} not co-selected in "
                    "the witness result — cannot ground the value to the entity",
                )
            grounded = not grounds_entity or _categorical_eq(
                run.value(row, ob.predicate.entity_col), ob.predicate.entity_val
            )
            # A missing cell (predicate.select unset or not co-selected) is a
            # different failure from an unparseable surface — say so, rather than
            # blaming the value_type (the misleading "X is not interpretable as
            # categorical" that a categorical surface can never actually hit).
            if cell is None:
                return (
                    ObligationStatus.COULD_NOT_DISCHARGE,
                    (("row_grounding", grounded), ("select_in_result", False)),
                    None,
                    f"predicate.select {sel!r} names no cell in the witness result "
                    f"{run.columns} — set select to the column whose cell is the "
                    "claimed value (e.g. the entity column for a state name)",
                )
            # A surface that cannot be read under its declared type is unconfirmable
            # (typically an entity code declared numeric) — fail closed, do not
            # REFUTE a claim the data does not actually contradict.
            if not comparable:
                return (
                    ObligationStatus.COULD_NOT_DISCHARGE,
                    (("row_grounding", grounded), ("typed_eq", False)),
                    cell,
                    _uninterpretable_detail(ob),
                )
            # Maximality is NOT asserted here — discharge() confirms it by
            # recomputing the extremum over the full declared population.
            checks = (
                ("row_grounding", grounded),
                ("typed_eq", value_ok),
            )
            if grounded and value_ok:
                return (ObligationStatus.DISCHARGED, checks, cell, "")
            return (
                ObligationStatus.REFUTED,
                checks,
                cell,
                "extremum row contradicts the claimed entity/value",
            )
        case ClaimKind.MEMBERSHIP:
            column = sel or ob.predicate.entity_col or (
                run.columns[0] if run.columns else ""
            )
            target = (
                ob.predicate.entity_val
                if ob.predicate.entity_val is not None
                else ob.surface
            )
            present = any(_categorical_eq(v, target) for v in run.column_values(column))
            status = (
                ObligationStatus.DISCHARGED if present else ObligationStatus.REFUTED
            )
            return (
                status,
                (("membership", present),),
                target if present else None,
                "" if present else f"{target!r} not present in witness result",
            )
        case _:
            return (
                ObligationStatus.COULD_NOT_DISCHARGE,
                ((f"kind:{ob.kind.value}", False),),
                None,
                f"kind {ob.kind.value!r} has no discharge rule yet",
            )


def provenance_ok(
    ob: Obligation, lineage_by_alias: tuple[tuple[str, tuple[str, ...]], ...]
) -> bool:
    """``requires_sources`` must be covered by the witness alias's declared
    lineage. Empty requirement holds trivially; unknown lineage cannot confirm."""
    if not ob.requires_sources:
        return True
    lineage = dict(lineage_by_alias).get(ob.on)
    if lineage is None:
        return False
    return frozenset(ob.requires_sources) <= frozenset(lineage)


# Source URI schemes we can re-fetch from anywhere. A source with none of these
# schemes is a local/scratch path (or an in-memory table with no source at all) —
# not independently reproducible, and the tell of hand-fabricated data added
# straight to the catalog. This is the ONE definition of "remote"; lineage.py
# imports it, so the value checker and the lineage checker never disagree on the
# same source under the no-local policy.
REMOTE_SCHEMES: frozenset[str] = frozenset(
    {"http", "https", "s3", "s3a", "gs", "gcs", "az", "abfs", "abfss",
     "hdfs", "ftp", "ftps", "r2", "hf"}
)
# read-kwarg keys that carry a source location in a serialized Read node, most
# specific first. Shared so witness.alias_sources and lineage.check probe the same
# keys (a source under a key one of them omitted would be invisible to that one).
PATH_KEYS: tuple[str, ...] = (
    "hash_path", "path", "paths", "source", "uri", "url", "location",
)


def _scheme(source: str) -> str:
    """The URI scheme of a source string ("" for a bare path or a ``<marker>``)."""
    return source.split("://", 1)[0].strip().lower() if "://" in source else ""


def is_remote_source(source: str) -> bool:
    """Whether a source URL/path is re-fetchable (has a remote scheme)."""
    return isinstance(source, str) and _scheme(source) in REMOTE_SCHEMES


def sources_trusted(sources: tuple[str, ...], no_local_sources: bool) -> bool:
    """The alias's lineage-trust decision under the policy.

    With ``no_local_sources`` off, any source is accepted. With it on, EVERY
    source the alias reads must be remote (re-fetchable) — and there must be at
    least one: an alias with no discoverable source (an in-memory/hand-added
    table) is untrusted (fail closed), which is what catches data typed straight
    into the catalog."""
    if not no_local_sources:
        return True
    return bool(sources) and all(is_remote_source(s) for s in sources)


# --------------------------------------------------------------------------- #
# Discharge + verdict fold                                                     #
# --------------------------------------------------------------------------- #


def discharge(
    ob: Obligation,
    catalog_path: str,
    lineage_by_alias: tuple[tuple[str, tuple[str, ...]], ...] = (),
    catalog_witnesses: bool = False,
    no_local_sources: bool = False,
) -> ObligationResult:
    """Discharge one obligation against the catalog (ADR-0001 §2).

    Loads the declared alias, builds the witness (synthesized from the predicate
    when possible, else the producer's compose), validates its op-tree, runs it
    in-process, applies the kind's predicate + typed-equality faithfulness,
    recomputes maximality for extrema, and confirms provenance. Any step that
    cannot be confirmed fails closed to ``COULD-NOT-DISCHARGE``; a value (or an
    extremum) contradicted by the data is ``REFUTED``.

    The result carries ``witness_code`` — the compose code that discharges it, so
    the certificate is re-runnable. When ``catalog_witnesses`` and the obligation
    ``DISCHARGED``, the witness is also persisted as a composed catalog entry
    (``verify-<id>``) and its alias recorded in ``witness_ref`` (ADR-0001 §4).
    """
    from pi_xorq_verifier import witness  # noqa: PLC0415

    alias_expr = witness.load_alias_expr(catalog_path, ob.on)
    if alias_expr is None:
        return ObligationResult(
            ob.id,
            ObligationStatus.COULD_NOT_DISCHARGE,
            (("witness_on_declared_alias", False),),
            base_alias=ob.on,
            detail="alias did not load from the catalog (or xorq unavailable)",
        )
    expr = witness.build_witness(alias_expr, ob)
    checks = witness.validate_witness(expr, alias_expr, ob)
    if not all(passed for _, passed in checks):
        # Name *why* it is ill-formed when we can, so a malformed obligation
        # self-corrects instead of dead-ending (the reason a run abandoned the
        # table kind and fell back to weaker per-fact checks).
        reason = witness.build_error(alias_expr, ob) if expr is None else ""
        return ObligationResult(
            ob.id,
            ObligationStatus.COULD_NOT_DISCHARGE,
            checks,
            base_alias=ob.on,
            detail=reason or "ill-formed witness",
        )
    run = witness.run_expr(expr)
    if run is None:
        return ObligationResult(
            ob.id,
            ObligationStatus.COULD_NOT_DISCHARGE,
            checks,
            base_alias=ob.on,
            detail="witness did not evaluate against the catalog",
        )
    status, predicate_checks, cell, detail = _discharge_predicate(ob, run)
    checks = checks + predicate_checks
    if ob.kind in (ClaimKind.ARGMAX, ClaimKind.ARGMIN):
        status, maximality, detail = _confirm_maximality(
            ob, run, alias_expr, status, detail, witness
        )
        checks = checks + (maximality,)
    provenance = provenance_ok(ob, lineage_by_alias)
    checks = checks + (("provenance", provenance),)
    if status is ObligationStatus.DISCHARGED and not provenance:
        status = ObligationStatus.COULD_NOT_DISCHARGE
        detail = "provenance not confirmed: requires_sources exceed declared lineage"
    # Grounded lineage policy: judge the alias's ACTUAL sources (from its op-tree),
    # not a producer-declared list. Under `no_local_sources`, an alias backed by a
    # local/scratch path — or by an in-memory table with no source at all (data
    # typed straight into the catalog) — is rejected; only a re-fetchable (remote)
    # source is trusted.
    sources = witness.alias_sources(alias_expr)
    if no_local_sources:
        trusted = sources_trusted(sources, True)
        # Scan the alias AND the witness expression: an ungrounded scalar/metric
        # fabricates its value in `population`, which the alias-only scan
        # never sees (the compose-escape-hatch hole).
        constants = tuple(
            dict.fromkeys(
                witness.magic_constants(alias_expr) + witness.magic_constants(expr)
            )
        )
        checks = checks + (
            ("remote_sources", trusted),
            ("no_magic_constants", not constants),
        )
        if status is ObligationStatus.DISCHARGED and not trusted:
            status = ObligationStatus.COULD_NOT_DISCHARGE
            where = ", ".join(sources) if sources else "an in-memory/hand-added table"
            detail = (
                f"local source not allowed: alias '{ob.on}' reads from {where} — "
                "ingest from a re-fetchable source (a URL) so the lineage is reproducible"
            )
        elif status is ObligationStatus.DISCHARGED and constants:
            status = ObligationStatus.COULD_NOT_DISCHARGE
            detail = (
                f"fabricated constant not allowed: alias '{ob.on}' hardcodes "
                f"{', '.join(constants)} in its arithmetic — source it as a column "
                "(join a sourced alias), not a literal typed from memory"
            )
    code = witness.witness_code(alias_expr, ob) or ""
    witness_ref = ""
    if status is ObligationStatus.DISCHARGED and catalog_witnesses and code:
        # A table has no single cell — confirm the persisted witness reproduces
        # the grid by row count; value kinds confirm the selected cell.
        expected_rows = len(ob.predicate.rows) if ob.kind is ClaimKind.TABLE else None
        witness_ref = (
            witness.catalog_witness(
                catalog_path, ob.on, code, f"verify-{ob.id}", cell,
                ob.predicate.select, expected_rows=expected_rows,
            )
            or ""
        )
    return ObligationResult(
        ob.id,
        status,
        checks,
        selected_cell=cell,
        base_alias=ob.on,
        detail=detail,
        witness_code=code,
        witness_ref=witness_ref,
        sources=sources,
    )


# Metric cells (counts, revenues) are compared as exact decimals regardless of
# the surface's declared value_type (which may be categorical for an entity claim).
_NUMERIC = ValueType(kind="decimal", tolerance="0")


def _confirm_maximality(
    ob: Obligation,
    run: WitnessRun,
    alias_expr,
    status: ObligationStatus,
    detail: str,
    witness,
) -> tuple[ObligationStatus, tuple[str, bool], str]:
    """Discharge the ∀-quantifier of an extremum by recomputing it.

    The witness's extremum cell must equal ``max``/``min`` recomputed over the
    witness's *own* population (``recompute_extremum`` ranges over exactly what
    the witness ranked). A witness that narrowed the population below itself lands
    a non-extremal value and is refuted; an extremum that cannot be recomputed or
    co-selected fails closed rather than passing.

    The quantifier ranges over the witness's population, so a ``population``
    restriction shrinks it — "ORD is busiest" over ``filter(origin != 'ATL')``
    genuinely maximizes its restricted set while contradicting the unrestricted
    claim, and the checker cannot see the prose to know which was meant. So a
    restricted extremum is reported under ``maximality_within_scope`` (never plain
    ``maximality``): the signal can never read as an unconditional superlative,
    and ``witness_code`` carries the population it was judged over.
    """
    # A witness that restricts the population (a non-empty compose) maximizes only
    # its scoped set; report it as `maximality_within_scope` (never bare
    # `maximality`) so it can't read as an unconditional superlative.
    check = "maximality_within_scope" if ob.population else "maximality"
    metric = ob.predicate.metric_col or ob.predicate.select
    if not run.rows or metric not in run.columns:
        return (
            ObligationStatus.COULD_NOT_DISCHARGE,
            (check, False),
            f"witness did not co-select the metric column {metric!r}; cannot confirm maximality",
        )
    extremum = witness.recompute_extremum(alias_expr, ob)
    if extremum is None:
        return (
            ObligationStatus.COULD_NOT_DISCHARGE,
            (check, False),
            "could not recompute the extremum to confirm maximality "
            "(declare predicate.metric_col for the ranked column)",
        )
    witness_metric = run.value(run.rows[0], metric)
    maximal = witness_metric is not None and values_match(
        witness_metric, extremum, _NUMERIC
    )
    if not maximal:
        return (
            ObligationStatus.REFUTED,
            (check, False),
            f"witness extremum {witness_metric!r} is not the true extremum {extremum!r} "
            "— the population was narrowed or mis-ranked",
        )
    return status, (check, True), detail


def fold_verdict(results: tuple[ObligationResult, ...]) -> Verdict:
    """Monotone aggregation — the model can never upgrade a verdict."""
    if not results:
        return Verdict.NO_OP
    statuses = tuple(r.status for r in results)
    if ObligationStatus.REFUTED in statuses:
        return Verdict.DISCREPANCY
    if all(s is ObligationStatus.DISCHARGED for s in statuses):
        return Verdict.VERIFIED
    return Verdict.COULD_NOT_VERIFY


def _content_hash(data: bytes) -> str:
    """A short, stable, prefixed content handle (not a full digest — this is a
    transparency/dedup reference, not a cryptographic commitment)."""
    return "sha256:" + hashlib.sha256(data).hexdigest()[:16]


def _witness_hash(code: str) -> str:
    """Content handle for a witness's code; "" when there is no witness."""
    return _content_hash(code.encode("utf-8")) if code else ""


def _catalog_state(catalog_path: str) -> str:
    """Best-effort content handle for the catalog the certificate was checked
    against: the hash of its ``catalog.yaml`` manifest, which is content-addressed
    and so moves whenever entries or aliases change. Returns "" when it cannot be
    read — a missing manifest never fails a verdict, it just leaves the state
    unpinned (fail-open on transparency, never on soundness)."""
    from pathlib import Path

    try:
        return _content_hash((Path(catalog_path) / "catalog.yaml").read_bytes())
    except OSError:
        return ""


def check_obligations(
    obligations: tuple[Obligation, ...],
    catalog_path: str,
    reply_values: tuple[str, ...] = (),
    expressions: tuple[tuple[str, tuple[str, ...]], ...] = (),
    catalog_witnesses: bool = False,
    no_local_sources: bool = False,
) -> Certificate:
    """Discharge every obligation, fold the verdict, and audit coverage.

    ``expressions`` declares each alias's lineage as ``(alias, (source, ...))``;
    it grounds the provenance check (ADR-0001 §2). When ``catalog_witnesses``,
    each DISCHARGED witness is also persisted as a ``verify-<id>`` catalog entry.

    Each result is enriched with the producer-declared knobs it was judged under
    (surface, tolerance, value kind, and a witness content hash) so the
    certificate discloses every dial that widened acceptance and is re-checkable
    on its own — you never need the original request to re-run the proof. The
    population lives in ``witness_code``, not a separate field.
    """
    lineage_by_alias = tuple(
        (alias, tuple(lineage)) for alias, lineage in expressions
    )
    raw = tuple(
        discharge(ob, catalog_path, lineage_by_alias, catalog_witnesses, no_local_sources)
        for ob in obligations
    )
    results = tuple(
        evolve(
            r,
            surface=ob.surface,
            tolerance=ob.value_type.tolerance,
            value_kind=ob.value_type.kind,
            witness_hash=_witness_hash(r.witness_code),
            table_columns=(
                (ob.predicate.columns or ())
                if ob.kind is ClaimKind.TABLE else ()
            ),
            table_rows=(ob.predicate.rows if ob.kind is ClaimKind.TABLE else ()),
        )
        for ob, r in zip(obligations, raw)
    )
    covered = frozenset().union(
        *(_covered_surfaces(ob) for ob in obligations), frozenset()
    )
    uncovered = tuple(v for v in reply_values if v not in covered)
    verdict = fold_verdict(results)
    # An uncovered reply value is a stated figure no obligation accounts for; it
    # downgrades any *passing* verdict — VERIFIED and NO-OP alike. Without the
    # NO-OP case, a request that declares reply_values but zero obligations folds
    # to NO-OP and clears the gate with every figure unbacked.
    if uncovered and verdict in (Verdict.VERIFIED, Verdict.NO_OP):
        verdict = Verdict.COULD_NOT_VERIFY
    return Certificate(verdict, results, uncovered, catalog_state=_catalog_state(catalog_path))


def _covered_surfaces(ob: Obligation) -> frozenset[str]:
    """Every reply value an obligation accounts for in the coverage audit.

    A scalar/argmax/… obligation covers its single ``surface``. A ``table``
    obligation covers its whole grid — *every* claimed cell value — so listing
    each rendered number in ``reply_values`` stays covered by the one table
    obligation that verified them, rather than each number needing its own
    obligation (the bug that made a fully-verified table read COULD-NOT-VERIFY)."""
    surfaces = {ob.surface}
    if ob.kind is ClaimKind.TABLE:
        surfaces |= {val for row in ob.predicate.rows for _, val in row}
    return frozenset(surfaces)


# --------------------------------------------------------------------------- #
# (De)serialization                                                            #
# --------------------------------------------------------------------------- #


def obligation_from_dict(d: dict) -> Obligation:
    # A producer (or an LLM building the request) can hand us a malformed shape —
    # e.g. `predicate` as a string instead of an object. Coerce non-dict
    # sub-objects to empty rather than crashing with AttributeError; the obligation
    # then fails closed to COULD-NOT-DISCHARGE (an empty predicate selects nothing)
    # instead of taking the trust gate down. Missing id/kind/surface still raise a
    # clean KeyError/ValueError the CLI reports as a bad request.
    if not isinstance(d, dict):
        raise ValueError(f"each obligation must be a JSON object, got {type(d).__name__}")

    def _obj(key: str) -> dict:
        value = d.get(key)
        return value if isinstance(value, dict) else {}

    p, vt = _obj("predicate"), _obj("value_type")
    p_rows = p.get("rows", ())
    rows = tuple(
        tuple((str(k), str(v)) for k, v in row.items())
        for row in (p_rows if isinstance(p_rows, list) else ())
        if isinstance(row, dict)
    )
    p_vtypes = p.get("value_types", {})
    value_types = tuple(
        (col, ValueType(kind=spec.get("kind", "int"), tolerance=str(spec.get("tolerance", "0"))))
        for col, spec in (p_vtypes.items() if isinstance(p_vtypes, dict) else ())
        if isinstance(spec, dict)
    )
    kind = ClaimKind(d["kind"])
    # `surface` is the claimed value for the value kinds (required — a missing one
    # is a genuinely malformed obligation). A `table`'s content is its grid, so
    # `surface` there is only an optional label — don't reject a table for lacking
    # one (that KeyError killed the whole request, losing every obligation).
    surface = str(d["surface"]) if kind is not ClaimKind.TABLE else str(d.get("surface", ""))
    # The site is declared flat on the obligation: `on` (alias), `population`
    # (restriction), and — for an ungrounded scalar only — `expression`. A
    # non-string value coerces to "" and the obligation fails closed downstream.
    def _str(key: str) -> str:
        value = d.get(key)
        return value if isinstance(value, str) else ""

    return Obligation(
        id=d["id"],
        kind=kind,
        surface=surface,
        on=_str("on"),
        population=_str("population"),
        expression=_str("expression"),
        predicate=Predicate(
            select=p.get("select", ""),
            entity_col=p.get("entity_col"),
            # Coerce to str: a JSON-number entity_val (a year, a FIPS code) must not
            # reach the string-only op-tree checks (e.g. _is_circular's .strip()).
            entity_val=(None if p.get("entity_val") is None else str(p.get("entity_val"))),
            metric_col=p.get("metric_col"),
            columns=tuple(p.get("columns", ())),
            rows=rows,
            ordered=bool(p.get("ordered", True)),
            value_types=value_types,
        ),
        value_type=ValueType(
            kind=vt.get("kind", "int"), tolerance=str(vt.get("tolerance", "0"))
        ),
        requires_sources=tuple(d.get("requires_sources", ())),
    )


def _obligation_to_dict(r: ObligationResult) -> dict:
    d = {
        "id": r.id,
        "status": r.status.value,
        "checks": {name: passed for name, passed in r.checks},
        "surface": r.surface,
        "selected_cell": r.selected_cell,
        "value_type": {"kind": r.value_kind, "tolerance": r.tolerance},
        "base_alias": r.base_alias,
        "witness_code": r.witness_code,
        "witness_hash": r.witness_hash,
        "witness_ref": r.witness_ref,
        "sources": list(r.sources),
        "detail": r.detail,
    }
    # A table obligation has no single cell: emit the verified grid instead, so
    # the certificate records the rows it confirmed (selected_cell stays null).
    if r.table_rows:
        d["grid"] = {
            "columns": list(r.table_columns),
            "rows": [dict(row) for row in r.table_rows],
        }
    return d


def certificate_to_dict(c: Certificate) -> dict:
    return {
        "verdict": c.verdict.value,
        "obligations": [_obligation_to_dict(r) for r in c.results],
        "coverage": {"uncovered": list(c.uncovered)},
        "catalog_state": c.catalog_state,
        "soundness": c.soundness,
    }


# --------------------------------------------------------------------------- #
# Producer-declared request → certificate + CI gate                           #
# --------------------------------------------------------------------------- #

# Verdicts that clear the gate by default: a confirmed answer, or one that
# asserts nothing checkable. DISCREPANCY and COULD-NOT-VERIFY are held back —
# a number that is contradicted or unconfirmable should not ship silently.
DEFAULT_GATE_ALLOW: tuple[Verdict, ...] = (Verdict.VERIFIED, Verdict.NO_OP)


def certificate_for_request(
    request: dict,
    default_catalog_path: str = ".xorq/catalog",
    catalog_witnesses: bool = False,
    no_local_sources: bool = False,
) -> Certificate:
    """Discharge a full producer-declared request into a certificate.

    ``request`` is the ADR-0001 §6 shape (see ``schemas/request.schema.json``):
    ``{catalog_path?, expressions?, reply_values?, obligations}``. This is the
    single entry point a producer (an agent or a CI job) calls after declaring
    the obligations that back its answer. When ``catalog_witnesses`` (or the
    request sets ``"catalog_witnesses": true``), DISCHARGED witnesses are persisted
    to the catalog as ``verify-<id>`` entries.
    """
    obligations = tuple(
        obligation_from_dict(o) for o in request.get("obligations", ())
    )
    reply_values = tuple(str(v) for v in request.get("reply_values", ()))
    expressions = tuple(
        (e["alias"], tuple(str(s) for s in e.get("lineage", ())))
        for e in request.get("expressions", ())
        if isinstance(e, dict) and "alias" in e
    )
    # `no_local_sources` policy: rejects any alias not backed by a re-fetchable
    # (remote) source. Set per-request, or forced by the operator via the
    # PI_XORQ_NO_LOCAL_SOURCES env var (an env truthy value wins — the gate should
    # not be relaxable by the producer that declared the request).
    no_local_sources = (
        no_local_sources
        or bool(request.get("no_local_sources", False))
        or env_flag("PI_XORQ_NO_LOCAL_SOURCES")
    )
    return check_obligations(
        obligations,
        request.get("catalog_path", default_catalog_path),
        reply_values,
        expressions,
        catalog_witnesses or bool(request.get("catalog_witnesses", False)),
        no_local_sources,
    )


def env_flag(name: str) -> bool:
    """Whether an env var is set to a truthy value. The one definition of "truthy"
    for the operator policy overrides, so every surface (verify, gate, lineage)
    agrees on when e.g. PI_XORQ_NO_LOCAL_SOURCES is on."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def gate_passes(
    cert: Certificate, allowed: tuple[Verdict, ...] = DEFAULT_GATE_ALLOW
) -> bool:
    """Whether a certificate clears the gate (default: only VERIFIED or NO-OP)."""
    return cert.verdict in allowed
