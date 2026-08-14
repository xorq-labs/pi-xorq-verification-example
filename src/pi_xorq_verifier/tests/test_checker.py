import json

import pytest
from click.testing import CliRunner

from pi_xorq_verifier import witness as witness_mod
from pi_xorq_verifier.checker import (
    DEFAULT_GATE_ALLOW,
    Certificate,
    ClaimKind,
    Obligation,
    ObligationResult,
    ObligationStatus,
    Predicate,
    ValueType,
    Verdict,
    WitnessRun,
    _catalog_state,
    _covered_surfaces,
    _witness_hash,
    certificate_for_request,
    certificate_to_dict,
    check_obligations,
    discharge,
    fold_verdict,
    gate_passes,
    is_remote_source,
    obligation_from_dict,
    provenance_ok,
    sources_trusted,
    values_match,
)
from pi_xorq_verifier.cli import cli


# --------------------------------------------------------------------------- #
# Pure semantics                                                              #
# --------------------------------------------------------------------------- #


def _result(status: ObligationStatus) -> ObligationResult:
    return ObligationResult(id="x", status=status)


@pytest.mark.core
@pytest.mark.parametrize(
    "statuses,expected",
    (
        ((), Verdict.NO_OP),
        ((ObligationStatus.DISCHARGED,), Verdict.VERIFIED),
        ((ObligationStatus.DISCHARGED, ObligationStatus.DISCHARGED), Verdict.VERIFIED),
        ((ObligationStatus.DISCHARGED, ObligationStatus.REFUTED), Verdict.DISCREPANCY),
        (
            (ObligationStatus.DISCHARGED, ObligationStatus.COULD_NOT_DISCHARGE),
            Verdict.COULD_NOT_VERIFY,
        ),
        # REFUTED dominates COULD-NOT-DISCHARGE.
        (
            (ObligationStatus.COULD_NOT_DISCHARGE, ObligationStatus.REFUTED),
            Verdict.DISCREPANCY,
        ),
    ),
)
def test_fold_verdict_is_monotone(statuses, expected):
    assert fold_verdict(tuple(_result(s) for s in statuses)) is expected


@pytest.mark.core
@pytest.mark.parametrize(
    "surface,cell,kind,tol,expected",
    (
        ("17,875", "17875", "int", "0", True),
        ("6,300", "6300", "int", "0", True),
        ("$1,200.50", "1200.50", "currency", "0", True),
        ("2.3%", "0.023", "percent", "0", True),
        ("0.91", "0.912", "decimal", "0.01", True),
        ("0.91", "0.95", "decimal", "0.01", False),
        ("Gentoo", "gentoo", "categorical", "0", True),
        ("100", "101", "int", "0", False),
    ),
)
def test_values_match_typed(surface, cell, kind, tol, expected):
    assert values_match(surface, cell, ValueType(kind=kind, tolerance=tol)) is expected


@pytest.mark.core
@pytest.mark.parametrize(
    "surface,cell,kind,expected",
    (
        # A producer-invented value_type (not in the schema enum) must fail closed to
        # string equality, never crash the request with a Decimal parse error.
        ("Ads", "Ads", "string", True),
        ("Ads", "Cloud", "string", False),
        ("Ads", "ads", "text", True),  # unknown kind still casefolds like categorical
        # A numeric kind whose surface is non-numeric returns False, not a traceback.
        ("N/A", "17875", "int", False),
        # A malformed tolerance fails the numeric compare closed rather than raising.
        ("100", "100", "int", True),
    ),
)
def test_values_match_unknown_kind_fails_closed(surface, cell, kind, expected):
    assert values_match(surface, cell, ValueType(kind=kind, tolerance="0")) is expected


@pytest.mark.core
def test_values_match_bad_tolerance_does_not_crash():
    assert values_match("100", "100", ValueType(kind="int", tolerance="oops")) is False


@pytest.mark.core
@pytest.mark.parametrize(
    "requires,lineage,expected",
    (
        ((), (("flights-by-origin", ("flights.csv",)),), True),  # no requirement
        (("flights.csv",), (("flights-by-origin", ("flights.csv",)),), True),
        (("flights.csv",), (("flights-by-origin", ("other.csv",)),), False),
        (("flights.csv",), (), False),  # unknown lineage cannot confirm
    ),
)
def test_provenance_ok(requires, lineage, expected):
    ob = Obligation(
        id="c1",
        kind=ClaimKind.ARGMAX,
        surface="17,875",
        on="flights-by-origin",
        predicate=Predicate(select="n"),
        requires_sources=requires,
    )
    assert provenance_ok(ob, lineage) is expected


@pytest.mark.core
@pytest.mark.parametrize(
    "source,remote",
    (
        ("https://host/data.csv", True),
        ("http://host/data.csv", True),
        ("s3://bucket/data.csv", True),
        ("gs://bucket/data.parquet", True),
        ("hdfs://ns/data", True),   # in checker's list, was missing from lineage's
        ("r2://bucket/x", True),    # in lineage's list, was missing from checker's
        ("hf://datasets/x", True),  # unified scheme set covers both
        ("/tmp/state_population.csv", False),
        ("/private/tmp/x.csv", False),
        ("data/farmers_markets.csv", False),  # relative local path
        ("file:///tmp/x.csv", False),  # explicit local scheme is not re-fetchable
        ("<in-memory>", False),     # a leaf marker has no scheme
        ("", False),
    ),
)
def test_is_remote_source(source, remote):
    assert is_remote_source(source) is remote


@pytest.mark.core
@pytest.mark.parametrize(
    "sources,no_local,trusted",
    (
        (("https://h/d.csv",), True, True),          # remote → trusted
        (("https://h/d.csv", "s3://b/x"), True, True),
        (("/tmp/x.csv",), True, False),              # local → rejected
        (("https://h/d.csv", "/tmp/x.csv"), True, False),  # any local → rejected
        (("https://h/d.csv", "<in-memory>"), True, False),  # in-memory leaf → rejected
        (("<in-memory>",), True, False),             # hand-built table → rejected
        ((), True, False),                            # no source at all → fail closed
        (("/tmp/x.csv",), False, True),              # policy off → anything passes
        ((), False, True),
    ),
)
def test_sources_trusted(sources, no_local, trusted):
    assert sources_trusted(sources, no_local) is trusted


@pytest.mark.core
def test_empty_obligations_is_noop():
    cert = check_obligations((), catalog_path=".xorq/catalog")
    assert cert.verdict is Verdict.NO_OP


@pytest.mark.core
def test_reply_values_uncovered_with_zero_obligations_downgrades_noop():
    # reply_values but no obligations must NOT pass as NO-OP: the stated figures are
    # unbacked, so the coverage audit downgrades to COULD-NOT-VERIFY (else a producer
    # clears the gate by declaring numbers and no obligations).
    cert = check_obligations(
        (), catalog_path=".xorq/catalog", reply_values=("42", "99.9")
    )
    assert cert.verdict is Verdict.COULD_NOT_VERIFY
    assert cert.uncovered == ("42", "99.9")


@pytest.mark.core
def test_obligation_from_dict_coerces_numeric_entity_val():
    # A JSON-number entity_val (a year, a FIPS code) must become a str, or the
    # string-only op-tree checks (e.g. _is_circular's .strip()) crash the request.
    ob = obligation_from_dict({
        "id": "c", "kind": "argmax", "surface": "10",
        "on": "a",
        "predicate": {"select": "n", "entity_col": "year", "entity_val": 2023,
                      "metric_col": "n"},
    })
    assert ob.predicate.entity_val == "2023"
    assert isinstance(ob.predicate.entity_val, str)


@pytest.mark.core
def test_covered_surfaces_credits_table_grid_cells():
    # A table obligation covers every claimed cell, not just its surface label —
    # so reply_values listing each rendered number stays covered by the one table
    # obligation (regression: a fully-verified table read COULD-NOT-VERIFY).
    scalar = Obligation(
        id="s", kind=ClaimKind.SCALAR, surface="17,875",
        predicate=Predicate(),
    )
    assert _covered_surfaces(scalar) == frozenset({"17,875"})
    table = Obligation(
        id="t", kind=ClaimKind.TABLE, surface="top-2",
                predicate=Predicate(
            columns=("state", "pct"),
            rows=((("state", "Rhode Island"), ("pct", "78.1")),
                  (("state", "Nebraska"), ("pct", "45.2"))),
        ),
    )
    assert _covered_surfaces(table) == frozenset(
        {"top-2", "Rhode Island", "78.1", "Nebraska", "45.2"}
    )


# --------------------------------------------------------------------------- #
# discharge() decision procedure — the xorq-backed witness layer is stubbed at #
# its module boundary, so these stay hermetic (no engine, no catalog).         #
# --------------------------------------------------------------------------- #

_ALIAS = object()  # opaque sentinels: discharge only passes them back to witness
_EXPR = object()


def _run(columns, *rows) -> WitnessRun:
    return WitnessRun(
        columns=tuple(columns),
        rows=tuple(tuple(row.items()) for row in rows),
    )


def _stub_witness(
    monkeypatch, run, *, extremum=None, valid=True, expr=_EXPR, alias=_ALIAS
):
    """Stub the whole witness seam discharge() calls. ``run`` is the grid the
    (fake) execution returns; ``extremum`` is the recomputed max/min for argmax."""
    checks = (
        (("witness_on_declared_alias", True), ("noncircular", True), ("selection_only", True))
        if valid
        else (("witness_on_declared_alias", True), ("noncircular", False))
    )
    monkeypatch.setattr(witness_mod, "load_alias_expr", lambda *a, **k: alias)
    monkeypatch.setattr(witness_mod, "build_witness", lambda *a, **k: expr)
    monkeypatch.setattr(witness_mod, "validate_witness", lambda *a, **k: checks)
    monkeypatch.setattr(witness_mod, "run_expr", lambda *a, **k: run)
    monkeypatch.setattr(witness_mod, "recompute_extremum", lambda *a, **k: extremum)
    monkeypatch.setattr(witness_mod, "witness_code", lambda *a, **k: "source.select('n')")
    monkeypatch.setattr(witness_mod, "catalog_witness", lambda *a, **k: None)
    monkeypatch.setattr(witness_mod, "alias_sources", lambda *a, **k: ())
    monkeypatch.setattr(witness_mod, "magic_constants", lambda *a, **k: ())


def _argmax(surface="17,875", entity="ATL", sources=(), population="") -> Obligation:
    # compose = the population (empty ⇒ the whole alias, an unrestricted superlative)
    return Obligation(
        id="c1",
        kind=ClaimKind.ARGMAX,
        surface=surface,
        on="flights-by-origin",
        population=population,
        predicate=Predicate(
            select="n",
            entity_col="origin",
            entity_val=entity,
            metric_col="n",
        ),
        value_type=ValueType(kind="int"),
        requires_sources=tuple(sources),
    )


@pytest.mark.core
def test_discharge_argmax_discharged(monkeypatch):
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    r = discharge(_argmax(), "unused")
    assert r.status is ObligationStatus.DISCHARGED
    assert r.selected_cell == "17875"
    assert dict(r.checks)["row_grounding"] is True
    assert dict(r.checks)["maximality"] is True


@pytest.mark.core
def test_discharge_records_witness_code_and_ref_when_cataloging(monkeypatch):
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    monkeypatch.setattr(witness_mod, "witness_code", lambda *a, **k: "source.order_by(...)")
    monkeypatch.setattr(witness_mod, "catalog_witness", lambda *a, **k: "verify-c1")
    r = discharge(_argmax(), "unused", catalog_witnesses=True)
    assert r.status is ObligationStatus.DISCHARGED
    assert r.witness_code == "source.order_by(...)"
    assert r.witness_ref == "verify-c1"


@pytest.mark.core
def test_discharge_does_not_catalog_by_default(monkeypatch):
    # Default is read-only: witness_code is recorded, but nothing is persisted.
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    called: list[int] = []
    monkeypatch.setattr(witness_mod, "catalog_witness", lambda *a, **k: called.append(1))
    r = discharge(_argmax(), "unused")
    assert r.witness_ref == "" and not called


@pytest.mark.core
def test_discharge_no_local_sources_rejects_local_alias(monkeypatch):
    # Grounded lineage gate: with no_local_sources, an alias whose REAL source is a
    # local/scratch path (data typed straight into the catalog) fails closed — even
    # though the value would otherwise discharge. This is the check the run that
    # hand-added `state-population` from /tmp should have failed.
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    monkeypatch.setattr(
        witness_mod, "alias_sources", lambda *a, **k: ("/tmp/state_population.csv",)
    )
    r = discharge(_argmax(), "unused", no_local_sources=True)
    assert r.status is ObligationStatus.COULD_NOT_DISCHARGE
    assert dict(r.checks)["remote_sources"] is False
    assert "local source not allowed" in r.detail
    assert r.sources == ("/tmp/state_population.csv",)


@pytest.mark.core
def test_discharge_no_local_sources_accepts_remote_alias(monkeypatch):
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    monkeypatch.setattr(witness_mod, "alias_sources", lambda *a, **k: ("https://h/data.csv",))
    r = discharge(_argmax(), "unused", no_local_sources=True)
    assert r.status is ObligationStatus.DISCHARGED
    assert dict(r.checks)["remote_sources"] is True


@pytest.mark.core
def test_discharge_no_local_sources_rejects_in_memory_alias(monkeypatch):
    # An alias with NO discoverable source (in-memory / hand-typed df) is untrusted.
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    monkeypatch.setattr(witness_mod, "alias_sources", lambda *a, **k: ())
    r = discharge(_argmax(), "unused", no_local_sources=True)
    assert r.status is ObligationStatus.COULD_NOT_DISCHARGE
    assert "in-memory/hand-added table" in r.detail


@pytest.mark.core
def test_discharge_no_local_sources_rejects_inmemory_joined_to_url(monkeypatch):
    # THE gap from the last run: a real URL source JOINed with a hand-built
    # in-memory population table. The in-memory leaf surfaces as `<in-memory>`, so
    # the alias is NOT all-remote and fails — it no longer hides behind the URL.
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    monkeypatch.setattr(
        witness_mod, "alias_sources", lambda *a, **k: ("https://h/data.csv", "<in-memory>")
    )
    r = discharge(_argmax(), "unused", no_local_sources=True)
    assert r.status is ObligationStatus.COULD_NOT_DISCHARGE
    assert dict(r.checks)["remote_sources"] is False
    assert "<in-memory>" in r.detail


@pytest.mark.core
def test_discharge_no_magic_constants_rejects_hardcoded_literal(monkeypatch):
    # The other laundering path: a fabricated constant typed into the metric's
    # arithmetic (a hardcoded population). Sources are clean (remote), but the
    # embedded literal fails the no_magic_constants check.
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    monkeypatch.setattr(witness_mod, "alias_sources", lambda *a, **k: ("https://h/data.csv",))
    monkeypatch.setattr(witness_mod, "magic_constants", lambda *a, **k: ("37453038",))
    r = discharge(_argmax(), "unused", no_local_sources=True)
    assert r.status is ObligationStatus.COULD_NOT_DISCHARGE
    assert dict(r.checks)["remote_sources"] is True
    assert dict(r.checks)["no_magic_constants"] is False
    assert "37453038" in r.detail


@pytest.mark.core
def test_discharge_remote_no_constants_discharges(monkeypatch):
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    monkeypatch.setattr(witness_mod, "alias_sources", lambda *a, **k: ("https://h/data.csv",))
    monkeypatch.setattr(witness_mod, "magic_constants", lambda *a, **k: ())
    r = discharge(_argmax(), "unused", no_local_sources=True)
    assert r.status is ObligationStatus.DISCHARGED
    assert dict(r.checks) == {**dict(r.checks), "remote_sources": True, "no_magic_constants": True}


@pytest.mark.core
def test_discharge_local_alias_ok_when_policy_off(monkeypatch):
    # Default (policy off): a local source is fine and neither remote_sources nor
    # no_magic_constants check rows are added — backward compatible.
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    monkeypatch.setattr(witness_mod, "alias_sources", lambda *a, **k: ("/tmp/x.csv",))
    monkeypatch.setattr(witness_mod, "magic_constants", lambda *a, **k: ("37453038",))
    r = discharge(_argmax(), "unused")
    assert r.status is ObligationStatus.DISCHARGED
    assert "remote_sources" not in dict(r.checks)
    assert "no_magic_constants" not in dict(r.checks)


@pytest.mark.core
def test_alias_sources_and_magic_constants_extraction():
    # Real op-tree extraction (no catalog/engine — just ibis expr construction):
    # an in-memory table exposes a `<in-memory>` leaf, and a hardcoded literal in a
    # mutate is found while a power-of-ten unit scale is not.
    if not witness_mod._AVAILABLE:
        pytest.skip("xorq not available")
    import xorq.api as xo

    t = xo.memtable({"state_name": ["California"], "organic_vendor_markets": [116]})
    alias = t.mutate(
        per_100k=t.organic_vendor_markets / 37453038 * 100000
    ).select("state_name", "per_100k")
    assert witness_mod.alias_sources(alias) == ("<in-memory>",)
    constants = witness_mod.magic_constants(alias)
    assert "37453038" in constants
    assert "100000" not in constants  # unit scale allowed


def test_magic_constants_catches_bare_projected_literal():
    # A fabricated value laundered out of arithmetic into a bare projected column
    # (mutate(pop=N) then n/pop) is still caught — the NumericBinary walk alone
    # would miss it (the operand is a Field), the projected-Literal walk finds it.
    if not witness_mod._AVAILABLE:
        pytest.skip("xorq not available")
    import xorq.api as xo

    t = xo.memtable({"n": [1, 2, 3]})
    laundered = t.mutate(pop=37453038).mutate(
        per=lambda x: x.n / x.pop * 100000
    ).select("per")
    assert "37453038" in witness_mod.magic_constants(laundered.op())
    # An ungrounded-scalar fabrication (a literal projected as the answer) too.
    fabricated = t.mutate(x=424242).select("x")
    assert "424242" in witness_mod.magic_constants(fabricated.op())
    # A legitimate filter threshold is not a fabricated data value.
    assert witness_mod.magic_constants(t.filter(t.n >= 25).op()) == ()


def test_is_circular_catches_numeric_pin_under_categorical_value_type():
    # A `table` obligation whose value_type is categorical must still detect a pin on
    # a NUMERIC column: value targets are built per-column (Decimal), so the token
    # "12055" must be normalized under the column type too, or the pin escapes.
    if not witness_mod._AVAILABLE:
        pytest.skip("xorq not available")
    import xorq.api as xo

    t = xo.memtable({"origin": ["ATL", "ORD"], "n": [17875, 12055]})
    pinned = t.filter(t.n.isin([12055]))  # single-element numeric pin
    ob = Obligation(
        id="c", kind=ClaimKind.TABLE, surface="",
        on="a",
        predicate=Predicate(
            columns=("origin", "n"),
            rows=((("origin", "ORD"), ("n", "12055")),),
            metric_col="n",
            value_types=(("n", ValueType(kind="int")),),
        ),
        value_type=ValueType(kind="categorical"),  # obligation-level type is categorical
    )
    assert witness_mod._is_circular(pinned.op(), ob) is True


def _local_argmax_request() -> dict:
    return {
        "catalog_path": "unused",
        "obligations": [{
            "id": "c1", "kind": "argmax", "surface": "17,875",
            "on": "flights-by-origin",
            "predicate": {"select": "n", "entity_col": "origin", "entity_val": "ATL",
                          "metric_col": "n"},
            "value_type": {"kind": "int", "tolerance": "0"},
        }],
    }


@pytest.mark.core
def test_request_no_local_sources_flag_threads_through(monkeypatch):
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    monkeypatch.setattr(witness_mod, "alias_sources", lambda *a, **k: ("/tmp/x.csv",))
    req = {**_local_argmax_request(), "no_local_sources": True}
    cert = certificate_for_request(req)
    assert cert.results[0].status is ObligationStatus.COULD_NOT_DISCHARGE
    assert cert.verdict is Verdict.COULD_NOT_VERIFY


@pytest.mark.core
def test_env_forces_no_local_sources_over_request(monkeypatch):
    # The operator's env var enables the policy even when the request omits it —
    # the producer cannot relax the gate.
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    monkeypatch.setattr(witness_mod, "alias_sources", lambda *a, **k: ("/tmp/x.csv",))
    monkeypatch.setenv("PI_XORQ_NO_LOCAL_SOURCES", "1")
    cert = certificate_for_request(_local_argmax_request())  # request does not set the flag
    assert cert.results[0].status is ObligationStatus.COULD_NOT_DISCHARGE


@pytest.mark.core
def test_discharge_scoped_extremum_is_within_scope_never_unconditional(monkeypatch):
    # A superlative over a witness-restricted population (a non-empty compose)
    # maximizes only that population. The checker cannot see the prose to know the
    # restriction was intended, so it must never emit an unconditional
    # `maximality: true` — the check name carries the caveat; witness_code carries
    # the population.
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ORD", "n": "12055"}), extremum="12055"
    )
    ob = _argmax(
        surface="12,055", entity="ORD", population="source.filter(source.origin != 'ATL')"
    )
    r = discharge(ob, "unused")
    checks = dict(r.checks)
    assert r.status is ObligationStatus.DISCHARGED
    assert checks["maximality_within_scope"] is True
    assert "maximality" not in checks  # no bare, unconditional maximality signal


@pytest.mark.core
def test_discharge_unscoped_extremum_keeps_plain_maximality(monkeypatch):
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    checks = dict(discharge(_argmax(), "unused").checks)
    assert checks["maximality"] is True
    assert "maximality_within_scope" not in checks


@pytest.mark.core
def test_discharge_argmax_refuted_on_bad_value(monkeypatch):
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    r = discharge(_argmax(surface="99,999"), "unused")
    assert r.status is ObligationStatus.REFUTED
    assert dict(r.checks)["typed_eq"] is False


@pytest.mark.core
def test_discharge_argmax_refuted_on_bad_entity(monkeypatch):
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    r = discharge(_argmax(entity="ORD"), "unused")
    assert r.status is ObligationStatus.REFUTED
    assert dict(r.checks)["row_grounding"] is False


@pytest.mark.core
def test_discharge_argmax_maximality_refuted_when_extremum_disagrees(monkeypatch):
    # THE regression for the closed hole: the witness row's value matches the
    # claim and grounds the entity, but the recomputed extremum over the full
    # population is larger — the witness narrowed/mis-ranked the population, so
    # the superlative is REFUTED rather than a false DISCHARGE.
    _stub_witness(
        monkeypatch,
        _run(("origin", "n"), {"origin": "ORD", "n": "12055"}),
        extremum="17875",
    )
    r = discharge(_argmax(surface="12,055", entity="ORD"), "unused")
    assert r.status is ObligationStatus.REFUTED
    assert dict(r.checks)["maximality"] is False
    assert "not the true extremum" in r.detail


@pytest.mark.core
def test_discharge_argmax_missing_select_names_the_cell_not_the_type(monkeypatch):
    # An argmax whose predicate.select is unset gets a null cell — the message
    # must point at select, NOT falsely blame the value_type ("'Rhode Island' is
    # not interpretable as categorical", which a categorical surface can't hit).
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    ob = Obligation(
        id="c1", kind=ClaimKind.ARGMAX, surface="ATL",
        on="a",
        predicate=Predicate(select="", metric_col="n"),  # select unset
        value_type=ValueType(kind="categorical"),
    )
    r = discharge(ob, "unused")
    assert r.status is ObligationStatus.COULD_NOT_DISCHARGE
    assert "names no cell" in r.detail and "not interpretable" not in r.detail


@pytest.mark.core
def test_discharge_argmax_maximality_unrecomputable_is_could_not(monkeypatch):
    # Extremum cannot be recomputed (e.g. metric_col undeclared) → fail closed,
    # never a false DISCHARGE and never a false REFUTED.
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum=None
    )
    r = discharge(_argmax(), "unused")
    assert r.status is ObligationStatus.COULD_NOT_DISCHARGE
    assert dict(r.checks)["maximality"] is False


@pytest.mark.core
def test_discharge_argmax_missing_metric_column_is_could_not(monkeypatch):
    # Witness result lacks the metric column, so maximality cannot be grounded.
    _stub_witness(monkeypatch, _run(("origin",), {"origin": "ATL"}), extremum="17875")
    r = discharge(_argmax(surface="ATL"), "unused")
    assert r.status is ObligationStatus.COULD_NOT_DISCHARGE
    assert dict(r.checks)["maximality"] is False


@pytest.mark.core
def test_discharge_argmax_uninterpretable_surface_is_could_not_not_refuted(monkeypatch):
    # A categorical entity ("ATL") declared with a numeric value_type cannot be
    # parsed as a number. That is a mis-declared type, NOT the data contradicting
    # the claim — it must fail closed to COULD-NOT-DISCHARGE, never REFUTED.
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    ob = Obligation(
        id="c1",
        kind=ClaimKind.ARGMAX,
        surface="ATL",
        on="flights-by-origin",
        predicate=Predicate(select="origin", metric_col="n"),
        value_type=ValueType(kind="int"),  # wrong: should be categorical
    )
    r = discharge(ob, "unused")
    assert r.status is ObligationStatus.COULD_NOT_DISCHARGE
    assert dict(r.checks)["typed_eq"] is False
    assert "not interpretable" in r.detail


@pytest.mark.core
def test_discharge_scalar_uninterpretable_surface_is_could_not(monkeypatch):
    _stub_witness(monkeypatch, _run(("origin", "revenue"), {"origin": "ATL", "revenue": "3000000"}))
    ob = _scalar_attr("ATL", "origin", "ATL")  # surface 'ATL' but value_type int
    r = discharge(ob, "unused")
    assert r.status is ObligationStatus.COULD_NOT_DISCHARGE
    assert "not interpretable" in r.detail


@pytest.mark.core
def test_discharge_count_without_select_reads_the_sole_column(monkeypatch):
    # A `count` witness names its cell 'n' by synthesis; a producer that omits
    # predicate.select must still discharge (read the single result column) —
    # not dead-end at a misleading "surface not interpretable" (the peptides bug).
    _stub_witness(monkeypatch, _run(("n",), {"n": "79"}))
    ob = Obligation(
        id="c", kind=ClaimKind.COUNT, surface="79",
        on="peptides",
        predicate=Predicate(select=""),  # omitted
        value_type=ValueType(kind="int"),
    )
    r = discharge(ob, "unused")
    assert r.status is ObligationStatus.DISCHARGED
    assert r.selected_cell == "79"


@pytest.mark.core
def test_discharge_count_without_select_refutes_wrong_value(monkeypatch):
    _stub_witness(monkeypatch, _run(("n",), {"n": "156"}))
    ob = Obligation(
        id="c", kind=ClaimKind.COUNT, surface="79",
        on="peptides",
        predicate=Predicate(select=""),
        value_type=ValueType(kind="int"),
    )
    r = discharge(ob, "unused")
    assert r.status is ObligationStatus.REFUTED  # not COULD-NOT — a real contradiction


@pytest.mark.core
def test_discharge_empty_cell_reports_null_not_uninterpretable(monkeypatch):
    # A genuinely empty/absent cell must say so, not blame the surface's type.
    _stub_witness(monkeypatch, _run(("n",), {"n": ""}))
    ob = Obligation(
        id="c", kind=ClaimKind.COUNT, surface="79",
        on="peptides",
        predicate=Predicate(select="n"), value_type=ValueType(kind="int"),
    )
    r = discharge(ob, "unused")
    assert r.status is ObligationStatus.COULD_NOT_DISCHARGE
    assert "empty/null" in r.detail and "not interpretable" not in r.detail


@pytest.mark.core
def test_discharge_count_discharged(monkeypatch):
    _stub_witness(monkeypatch, _run(("n",), {"n": "4"}))
    ob = Obligation(
        id="c1",
        kind=ClaimKind.COUNT,
        surface="4",
        on="flights-by-origin",
        predicate=Predicate(select="n"),
        value_type=ValueType(kind="int"),
    )
    assert discharge(ob, "unused").status is ObligationStatus.DISCHARGED


def _scalar_attr(surface, entity_col=None, entity_val=None) -> Obligation:
    return Obligation(
        id="attr",
        kind=ClaimKind.SCALAR,
        surface=surface,
        on="origin-metrics",
        expression="source.select('origin','revenue')",
        predicate=Predicate(select="revenue", entity_col=entity_col, entity_val=entity_val),
        value_type=ValueType(kind="int"),
    )


@pytest.mark.core
def test_discharge_scalar_grounded_matches(monkeypatch):
    _stub_witness(monkeypatch, _run(("origin", "revenue"), {"origin": "ATL", "revenue": "3000000"}))
    r = discharge(_scalar_attr("3000000", "origin", "ATL"), "unused")
    assert r.status is ObligationStatus.DISCHARGED
    assert dict(r.checks)["row_grounding"] is True


@pytest.mark.core
def test_discharge_scalar_grounded_wrong_value_refuted(monkeypatch):
    _stub_witness(monkeypatch, _run(("origin", "revenue"), {"origin": "ATL", "revenue": "3000000"}))
    r = discharge(_scalar_attr("5000000", "origin", "ATL"), "unused")
    assert r.status is ObligationStatus.REFUTED


@pytest.mark.core
def test_discharge_scalar_wrong_entity_row_is_could_not(monkeypatch):
    # Witness returned some other entity's row; must NOT falsely discharge for ATL.
    _stub_witness(monkeypatch, _run(("origin", "revenue"), {"origin": "ORD", "revenue": "5000000"}))
    r = discharge(_scalar_attr("5000000", "origin", "ATL"), "unused")
    assert r.status is ObligationStatus.COULD_NOT_DISCHARGE
    assert dict(r.checks)["row_grounding"] is False


@pytest.mark.core
def test_discharge_scalar_ungrounded_unchanged(monkeypatch):
    _stub_witness(monkeypatch, _run(("revenue",), {"revenue": "5000000"}))
    r = discharge(_scalar_attr("5000000"), "unused")
    assert r.status is ObligationStatus.DISCHARGED
    assert "row_grounding" not in dict(r.checks)


@pytest.mark.core
def test_discharge_scalar_grounds_entity_beyond_first_row(monkeypatch):
    # Grounding must find the entity's row wherever it sits — judging rows[0]
    # made the verdict depend on physical row order.
    _stub_witness(
        monkeypatch,
        _run(
            ("origin", "revenue"),
            {"origin": "ORD", "revenue": "5000000"},
            {"origin": "ATL", "revenue": "3000000"},
        ),
    )
    r = discharge(_scalar_attr("3000000", "origin", "ATL"), "unused")
    assert r.status is ObligationStatus.DISCHARGED
    assert r.selected_cell == "3000000"


@pytest.mark.core
def test_discharge_scalar_ambiguous_entity_rows_fail_closed(monkeypatch):
    # Two rows for the claimed entity with distinct values: which cell "the"
    # value is would depend on row order — fail closed, never roll that die.
    _stub_witness(
        monkeypatch,
        _run(
            ("origin", "revenue"),
            {"origin": "ATL", "revenue": "3000000"},
            {"origin": "ATL", "revenue": "9000000"},
        ),
    )
    r = discharge(_scalar_attr("3000000", "origin", "ATL"), "unused")
    assert r.status is ObligationStatus.COULD_NOT_DISCHARGE
    assert dict(r.checks)["value_unambiguous"] is False


@pytest.mark.core
def test_discharge_scalar_duplicate_rows_are_unambiguous(monkeypatch):
    _stub_witness(
        monkeypatch,
        _run(
            ("origin", "revenue"),
            {"origin": "ATL", "revenue": "3000000"},
            {"origin": "ATL", "revenue": "3000000"},
        ),
    )
    r = discharge(_scalar_attr("3000000", "origin", "ATL"), "unused")
    assert r.status is ObligationStatus.DISCHARGED


@pytest.mark.core
def test_discharge_scalar_ungrounded_multivalue_fails_closed(monkeypatch):
    # An ungrounded scalar over a multi-value result has no "the" cell to select.
    _stub_witness(
        monkeypatch, _run(("revenue",), {"revenue": "5000000"}, {"revenue": "3000000"})
    )
    r = discharge(_scalar_attr("5000000"), "unused")
    assert r.status is ObligationStatus.COULD_NOT_DISCHARGE
    assert dict(r.checks)["value_unambiguous"] is False


@pytest.mark.core
def test_discharge_membership(monkeypatch):
    _stub_witness(monkeypatch, _run(("origin",), {"origin": "ATL"}, {"origin": "ORD"}))
    ob = Obligation(
        id="c1",
        kind=ClaimKind.MEMBERSHIP,
        surface="ORD",
        on="flights-by-origin",
        predicate=Predicate(select="origin", entity_val="ORD"),
        value_type=ValueType(kind="categorical"),
    )
    assert discharge(ob, "unused").status is ObligationStatus.DISCHARGED


def _table_ob(rows, *, columns=("origin", "n"), ordered=True, metric="n") -> Obligation:
    vt = {"origin": ValueType(kind="categorical"), "n": ValueType(kind="int"),
          "name": ValueType(kind="categorical")}
    return Obligation(
        id="t",
        kind=ClaimKind.TABLE,
        surface="a table",
        on="flights-by-origin",
        predicate=Predicate(
            columns=columns,
            ordered=ordered,
            metric_col=metric,
            rows=tuple(tuple(r.items()) for r in rows),
            value_types=tuple((c, vt[c]) for c in columns),
        ),
        value_type=ValueType(kind="categorical"),
    )


@pytest.mark.core
def test_discharge_table_ranking_discharged(monkeypatch):
    _stub_witness(monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"},
                                    {"origin": "ORD", "n": "12055"}))
    r = discharge(_table_ob([{"origin": "ATL", "n": "17,875"},
                             {"origin": "ORD", "n": "12,055"}]), "unused")
    assert r.status is ObligationStatus.DISCHARGED


@pytest.mark.core
def test_discharge_table_ranking_refuted_on_order(monkeypatch):
    _stub_witness(monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"},
                                    {"origin": "ORD", "n": "12055"}))
    r = discharge(_table_ob([{"origin": "ORD", "n": "12,055"},
                             {"origin": "ATL", "n": "17,875"}]), "unused")
    assert r.status is ObligationStatus.REFUTED
    assert dict(r.checks)["cells_match"] is False


@pytest.mark.core
def test_discharge_table_ranking_refuted_on_row_count(monkeypatch):
    _stub_witness(monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"},
                                    {"origin": "ORD", "n": "12055"}))
    r = discharge(_table_ob([{"origin": "ATL", "n": "17,875"}]), "unused")
    assert r.status is ObligationStatus.REFUTED
    assert dict(r.checks)["row_count"] is False


@pytest.mark.core
def test_discharge_table_set_is_order_insensitive(monkeypatch):
    _stub_witness(monkeypatch, _run(("name",), {"name": "A"}, {"name": "B"}))
    r = discharge(_table_ob([{"name": "B"}, {"name": "A"}], columns=("name",),
                            ordered=False, metric=None), "unused")
    assert r.status is ObligationStatus.DISCHARGED


@pytest.mark.core
def test_discharge_table_set_refuted_when_missing(monkeypatch):
    _stub_witness(monkeypatch, _run(("name",), {"name": "A"}, {"name": "B"}))
    r = discharge(_table_ob([{"name": "A"}], columns=("name",), ordered=False,
                            metric=None), "unused")
    assert r.status is ObligationStatus.REFUTED
    assert dict(r.checks)["set_equal"] is False


@pytest.mark.core
def test_table_cert_has_null_cell_and_emits_the_grid(monkeypatch):
    # A table has no single cell: selected_cell is null, and the certificate
    # instead records the verified grid (columns + rows) — that's the content.
    _stub_witness(monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"},
                                    {"origin": "ORD", "n": "12055"}))
    rows = [{"origin": "ATL", "n": "17,875"}, {"origin": "ORD", "n": "12,055"}]
    cert = check_obligations((_table_ob(rows),), "unused")
    ob = certificate_to_dict(cert)["obligations"][0]
    assert ob["selected_cell"] is None
    assert ob["grid"]["columns"] == ["origin", "n"]
    assert ob["grid"]["rows"] == rows


@pytest.mark.core
def test_value_kind_obligation_has_no_grid_in_cert(monkeypatch):
    # Non-table obligations must not carry a `grid` key.
    _stub_witness(monkeypatch, _run(("n",), {"n": "4"}))
    ob = Obligation(
        id="c", kind=ClaimKind.COUNT, surface="4",
        on="a", predicate=Predicate(),
        value_type=ValueType(kind="int"),
    )
    d = certificate_to_dict(check_obligations((ob,), "unused"))["obligations"][0]
    assert "grid" not in d and d["selected_cell"] == "4"


@pytest.mark.core
def test_discharge_fails_closed_when_alias_does_not_load(monkeypatch):
    _stub_witness(monkeypatch, None, alias=None)
    r = discharge(_argmax(), "unused")
    assert r.status is ObligationStatus.COULD_NOT_DISCHARGE
    assert dict(r.checks)["witness_on_declared_alias"] is False


@pytest.mark.core
def test_discharge_fails_closed_when_witness_does_not_evaluate(monkeypatch):
    _stub_witness(monkeypatch, None, extremum="17875")
    r = discharge(_argmax(), "unused")
    assert r.status is ObligationStatus.COULD_NOT_DISCHARGE


@pytest.mark.core
def test_discharge_ill_formed_witness_fails_closed_without_running(monkeypatch):
    # validate_witness reports a failing check ⇒ COULD-NOT before predicate/eval.
    _stub_witness(monkeypatch, _run(("origin",), {"origin": "ATL"}), valid=False)
    r = discharge(_argmax(), "unused")
    assert r.status is ObligationStatus.COULD_NOT_DISCHARGE
    assert dict(r.checks)["noncircular"] is False


@pytest.mark.core
def test_discharge_provenance_downgrades_a_discharged_obligation(monkeypatch):
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    r = discharge(
        _argmax(sources=["flights.csv"]),
        "unused",
        lineage_by_alias=(("flights-by-origin", ("other.csv",)),),
    )
    assert r.status is ObligationStatus.COULD_NOT_DISCHARGE
    assert dict(r.checks)["provenance"] is False


@pytest.mark.core
def test_discharge_unsupported_kind_fails_closed(monkeypatch):
    _stub_witness(monkeypatch, _run(("a", "b"), {"a": "1", "b": "2"}))
    ob = Obligation(
        id="c1",
        kind=ClaimKind.COMPARE,
        surface="1",
        on="flights-by-origin",
        expression="source.select('a','b')",
        predicate=Predicate(select="a"),
    )
    assert discharge(ob, "unused").status is ObligationStatus.COULD_NOT_DISCHARGE


# --------------------------------------------------------------------------- #
# Op-tree validators — build real xorq expressions and check walk_nodes logic. #
# Skips when xorq is unavailable (the module fails closed there anyway).        #
# --------------------------------------------------------------------------- #

_needs_xorq = pytest.mark.skipif(not witness_mod.available(), reason="xorq not importable")


@pytest.fixture
def flights():
    import xorq.api as xo

    return xo.memtable(
        [
            {"origin": "ATL", "n": 17875},
            {"origin": "ORD", "n": 12055},
            {"origin": "DEN", "n": 9812},
        ],
        name="flights_by_origin",
    )


def _ob(kind, surface, *, select="n", value_type="int", metric_col=None):
    return Obligation(
        id="c",
        kind=kind,
        surface=surface,
        on="a",
        predicate=Predicate(select=select, metric_col=metric_col),
        value_type=ValueType(kind=value_type),
    )


@pytest.mark.core
@_needs_xorq
def test_witness_code_table_emits_the_full_ranking_not_just_population(flights):
    # For a table, witness_code must round-trip the ranking the checker ran —
    # order_by(metric).limit(k).select(...) — not merely the population filter.
    ob = Obligation(
        id="t", kind=ClaimKind.TABLE, surface="top 2",
        on="a",
        population="source.filter(source.n >= 1000)",
        predicate=Predicate(
            columns=("origin", "n"), ordered=True, metric_col="n",
            rows=((("origin", "ATL"), ("n", "17875")),
                  (("origin", "ORD"), ("n", "12055"))),
        ),
        value_type=ValueType(kind="categorical"),
    )
    code = witness_mod.witness_code(flights, ob)
    assert "order_by(source.n.desc())" in code
    assert ".limit(2)" in code
    assert "source.filter(source.n >= 1000)" in code  # population preserved


@pytest.mark.core
@_needs_xorq
@pytest.mark.parametrize(
    "compose,circular",
    (
        ("source.filter(source.n == 17875).select('n')", True),  # RHS literal
        ("source.filter(17875 == source.n).select('n')", True),  # LHS literal (regex missed this)
        ("source.filter(source.n.isin([17875])).select('n')", True),  # .isin (regex missed this)
        ("source.order_by(source.n.desc()).limit(1).select('origin','n')", False),
        ("source.filter(source.n > 1000).select('n')", False),  # inequality is legitimate
    ),
)
def test_is_circular_over_op_tree(flights, compose, circular):
    expr = witness_mod._eval_code(compose, flights)
    assert witness_mod._is_circular(expr, _ob(ClaimKind.SCALAR, "17,875")) is circular


@pytest.mark.core
@_needs_xorq
@pytest.mark.parametrize(
    "compose,circular",
    (
        # equality spelled as bounds pins the column at the claim just like `==`
        ("source.filter(source.n.between(17875, 17875)).select('n')", True),
        ("source.filter(source.n >= 17875).filter(source.n <= 17875).select('n')", True),
        ("source.filter(source.n <= 17875).filter(17875 <= source.n).select('n')", True),
        # one-sided bounds remain legitimate analysis parameters
        ("source.filter(source.n.between(0, 17875)).select('n')", False),
        ("source.filter(source.n >= 17875).select('n')", False),
    ),
)
def test_is_circular_catches_equal_bound_sandwiches(flights, compose, circular):
    expr = witness_mod._eval_code(compose, flights)
    assert witness_mod._is_circular(expr, _ob(ClaimKind.SCALAR, "17,875")) is circular


@pytest.mark.core
@_needs_xorq
def test_is_circular_catches_conjunction_sandwich(flights):
    # xorq's safe_eval currently rejects `&` in compose code, so this spelling
    # cannot arrive via the escape hatch — but the op-tree detection must not
    # depend on the eval whitelist, so build it directly.
    expr = flights.filter((flights.n >= 17875) & (flights.n <= 17875)).select("n")
    assert witness_mod._is_circular(expr, _ob(ClaimKind.SCALAR, "17,875")) is True


@pytest.mark.core
@_needs_xorq
def test_is_circular_sandwich_requires_same_column():
    # Opposing bounds at the claimed literal on DIFFERENT columns are two
    # ordinary analysis parameters, not a pin.
    import xorq.api as xo

    t = xo.memtable([{"a": 1, "b": 2}], name="two_cols")
    expr = t.filter(t.a >= 17875).filter(t.b <= 17875).select("a")
    assert witness_mod._is_circular(expr, _ob(ClaimKind.SCALAR, "17,875")) is False


@pytest.mark.core
@_needs_xorq
def test_is_circular_targets_claimed_entity_for_membership_and_extrema(flights):
    # Pinning the population to the claimed entity vacates the quantifier:
    # "ORD is present"/"ORD is top" witnessed by filter(origin == 'ORD') is
    # its own witness. A grounded scalar legitimately filters to its entity.
    pinned = witness_mod._eval_code(
        "source.filter(source.origin == 'ORD').select('origin', 'n')", flights
    )
    member = Obligation(
        id="c",
        kind=ClaimKind.MEMBERSHIP,
        surface="the top table includes ORD",
        on="a",
        predicate=Predicate(select="origin", entity_val="ORD"),
        value_type=ValueType(kind="categorical"),
    )
    assert witness_mod._is_circular(pinned, member) is True
    assert witness_mod._is_circular(pinned, _argmax(entity="ORD")) is True
    assert witness_mod._is_circular(pinned, _scalar_attr("12055", "origin", "ORD")) is False


@pytest.mark.core
@_needs_xorq
def test_argmax_fabricating_population_is_rejected(flights):
    # A self-join population can pair the claimed entity with the true extremum,
    # which row-grounding cannot see through. Now that the population lives in
    # compose, such a population is REJECTED (not a clean restriction) — synthesis
    # returns None → build_witness None → the obligation fails closed, strictly
    # stronger than the old "ignore the compose" behavior. Load-bearing for soundness.
    fabricating = (
        "source.filter(source.origin == 'ORD')"
        ".cross_join(source.order_by(source.n.desc()).limit(1).select(top='n'))"
    )
    ob = Obligation(
        id="c1",
        kind=ClaimKind.ARGMAX,
        surface="17,875",
        on="a",
        population=fabricating,
        predicate=Predicate(
            select="n", entity_col="origin", entity_val="ORD", metric_col="n"
        ),
        value_type=ValueType(kind="int"),
    )
    assert witness_mod.build_witness(flights, ob) is None  # fabricating population refused


@pytest.mark.core
@_needs_xorq
@pytest.mark.parametrize(
    "compose,has_arith",
    (
        ("source.select(x=source.n * 2)", True),
        ("source.aggregate(r=source.n.sum() / source.n.count())", True),
        ("source.order_by(source.n.desc()).limit(1).select('origin','n')", False),
        ("source.filter(source.n > 1000).select('n')", False),  # comparison is not arithmetic
    ),
)
def test_has_arithmetic_over_op_tree(flights, compose, has_arith):
    expr = witness_mod._eval_code(compose, flights)
    assert witness_mod._has_arithmetic(expr) is has_arith


@pytest.mark.core
@_needs_xorq
def test_has_arithmetic_excludes_the_aliases_own_computed_columns(flights):
    # A derived/cataloged alias with a computed column is a valid witness base:
    # selecting its cell is selection, not the witness computing. Only arithmetic
    # the witness ADDS on top of the alias counts. (Regression: derived aliases
    # like top-organic-share-by-state were unverifiable — selection_only:false.)
    derived = flights.mutate(share=(flights.n * 100).cast("float") / flights.n)
    selects = derived.order_by(derived.share.desc()).limit(1).select("origin", "share")
    assert witness_mod._has_arithmetic(selects, derived) is False  # alias math excluded
    assert witness_mod._has_arithmetic(selects) is True  # whole-tree (no base) still sees it
    adds = derived.select(origin="origin", doubled=derived.share * 2)
    assert witness_mod._has_arithmetic(adds, derived) is True  # witness added arithmetic


@pytest.mark.core
@_needs_xorq
def test_build_error_names_malformations(flights):
    # A malformed obligation must self-explain, not dead-end at "ill-formed
    # witness" — the reason a run abandoned the table kind for weaker checks.
    no_rows = Obligation(
        id="t", kind=ClaimKind.TABLE, surface="x",
        on="a",
        predicate=Predicate(columns=("origin", "n"), metric_col="n"),
    )
    assert "predicate.rows must be" in witness_mod.build_error(flights, no_rows)
    bad_compose = Obligation(
        id="s", kind=ClaimKind.COUNT, surface="1",
        on="a", population="n >= 1000",  # not a source.filter(...) expr
        predicate=Predicate(),
    )
    assert "did not compose" in witness_mod.build_error(flights, bad_compose)
    dirty_pop = Obligation(
        id="j", kind=ClaimKind.COUNT, surface="1",
        on="a", population="source.limit(3)",  # a limit pre-narrows
        predicate=Predicate(),
    )
    assert "restriction of the alias" in witness_mod.build_error(flights, dirty_pop)
    ampersand = Obligation(
        id="amp", kind=ClaimKind.COUNT, surface="1",
        on="a", population="source.filter((source.n >= 25) & (source.n < 100))",
        predicate=Predicate(),
    )
    msg = witness_mod.build_error(flights, ampersand)
    assert "chain filters" in msg and "&" in msg  # points at chaining, not "full expression"
    # An ungrounded scalar whose select names a REAL column is well-formed now —
    # the checker synthesizes the selection itself (no `expression` round-trip).
    bare_scalar = Obligation(
        id="bare", kind=ClaimKind.SCALAR, surface="1",
        on="a", predicate=Predicate(select="n"),
    )
    assert witness_mod.build_error(flights, bare_scalar) == ""
    # With no usable select (empty, or not an alias column), the hint names both
    # ways out: predicate.select on a real column, or a full `expression`.
    unnamed_scalar = Obligation(
        id="unnamed", kind=ClaimKind.SCALAR, surface="1",
        on="a", predicate=Predicate(select="not_a_column"),
    )
    msg = witness_mod.build_error(flights, unnamed_scalar)
    assert "predicate.select" in msg and "`expression`" in msg
    well_formed = Obligation(
        id="ok", kind=ClaimKind.SCALAR, surface="1",
        on="a", expression="source.select('n')", predicate=Predicate(select="n"),
    )
    assert witness_mod.build_error(flights, well_formed) == ""


@pytest.mark.core
@_needs_xorq
def test_rooted_on_alias_rejects_foreign_table(flights):
    import xorq.api as xo

    on_alias = flights.order_by(flights.n.desc()).limit(1).select("origin", "n")
    assert witness_mod._rooted_on_alias(on_alias, flights) is True
    foreign = xo.memtable([{"origin": "ZZZ", "n": 99999}], name="fake").select("origin", "n")
    assert witness_mod._rooted_on_alias(foreign, flights) is False


@pytest.mark.core
@_needs_xorq
def test_population_rejects_fabricating_or_narrowing_compose(flights):
    # The population must be a clean restriction of the alias: a self-join could
    # pair the claimed entity with a foreign extremum; a limit pre-narrows the set
    # the recompute maxes over. Both make synthesis fail closed.
    clean = witness_mod._population(flights, "source.filter(source.n > 1000)")
    assert witness_mod._clean_restriction(clean, flights) is True
    narrowed = witness_mod._population(flights, "source.limit(1)")
    assert witness_mod._clean_restriction(narrowed, flights) is False
    fabricated = witness_mod._population(
        flights, "source.cross_join(source.select(m='n'))"
    )
    assert witness_mod._clean_restriction(fabricated, flights) is False


@pytest.mark.core
@_needs_xorq
def test_shape_argmax_requires_descending_limit(flights):
    good = flights.order_by(flights.n.desc()).limit(1).select("origin", "n")
    ascending = flights.order_by(flights.n.asc()).limit(1).select("origin", "n")
    assert witness_mod._shape_ok(good, _ob(ClaimKind.ARGMAX, "17,875")) is True
    assert witness_mod._shape_ok(ascending, _ob(ClaimKind.ARGMAX, "17,875")) is False


def _table_grid_ob(rows, *, columns=("origin",), ordered=False, metric=None) -> Obligation:
    return Obligation(
        id="t", kind=ClaimKind.TABLE, surface="a table",
        on="a",
        predicate=Predicate(
            columns=columns, ordered=ordered, metric_col=metric,
            rows=tuple(tuple(r.items()) for r in rows),
            value_types=tuple((c, ValueType(kind="categorical")) for c in columns),
        ),
        value_type=ValueType(kind="categorical"),
    )


@pytest.mark.core
@_needs_xorq
def test_build_witness_blocks_escape_hatch_for_ordered_table(flights):
    # A ranking must be synthesized from metric_col; a producer's arbitrary ordered
    # compose must NOT be trusted (that reopens the maximality hole one kind over).
    ob = Obligation(
        id="t", kind=ClaimKind.TABLE, surface="top",
        on="a",
        expression="source.order_by(source.n.desc()).limit(1).select('origin','n')",
        predicate=Predicate(columns=("origin", "n"), ordered=True, metric_col=None,
                            rows=((("origin", "ATL"), ("n", "17875")),)),
        value_type=ValueType(kind="categorical"),
    )
    assert witness_mod.build_witness(flights, ob) is None


@pytest.mark.core
@_needs_xorq
def test_is_circular_targets_table_grid_cells(flights):
    # A ranking scoped to filter(entity.isin([the claimed rows])) is its own top-k.
    ob = _table_grid_ob([{"origin": "ATL"}], columns=("origin",), ordered=False)
    circular = witness_mod._eval_code("source.filter(source.origin.isin(['ATL']))", flights)
    honest = witness_mod._eval_code("source.filter(source.n > 1000)", flights)
    assert witness_mod._is_circular(circular, ob) is True
    assert witness_mod._is_circular(honest, ob) is False


# --------------------------------------------------------------------------- #
# Producer-declared request → certificate + CI gate                           #
# --------------------------------------------------------------------------- #


def _argmax_request(surface: str = "17,875") -> dict:
    return {
        "catalog_path": ".xorq/catalog",
        "obligations": [
            {
                "id": "c1",
                "kind": "argmax",
                "surface": surface,
                "value_type": {"kind": "int", "tolerance": "0"},
                "on": "flights-by-origin",
                "predicate": {
                    "select": "n",
                    "entity_col": "origin",
                    "entity_val": "ATL",
                    "metric_col": "n",
                },
            }
        ],
    }


def _write(tmp_path, obj) -> str:
    path = tmp_path / "req.json"
    path.write_text(json.dumps(obj))
    return str(path)


@pytest.mark.core
@pytest.mark.parametrize(
    "verdict,allowed,expected",
    (
        (Verdict.VERIFIED, DEFAULT_GATE_ALLOW, True),
        (Verdict.NO_OP, DEFAULT_GATE_ALLOW, True),
        (Verdict.DISCREPANCY, DEFAULT_GATE_ALLOW, False),
        (Verdict.COULD_NOT_VERIFY, DEFAULT_GATE_ALLOW, False),
        (Verdict.COULD_NOT_VERIFY, (Verdict.COULD_NOT_VERIFY,), True),
    ),
)
def test_gate_passes(verdict, allowed, expected):
    assert gate_passes(Certificate(verdict, ()), allowed) is expected


@pytest.mark.core
def test_certificate_for_request_empty_is_noop():
    cert = certificate_for_request({"catalog_path": ".xorq/catalog", "obligations": []})
    assert cert.verdict is Verdict.NO_OP


@pytest.mark.core
def test_certificate_discloses_declared_knobs(monkeypatch):
    # Transparency (ADR-0001 §4): the certificate must echo every producer-declared
    # dial that widened acceptance — the surface checked, its typing + tolerance,
    # and a content hash of the exact witness (which carries the population) — so a
    # consumer can re-run the proof without ever seeing the original request. There
    # is no separate `scope` field: the population lives in witness_code.
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    req = _argmax_request("17,875")
    req["obligations"][0]["value_type"]["tolerance"] = "5"
    ob = certificate_to_dict(certificate_for_request(req))["obligations"][0]
    assert ob["surface"] == "17,875"
    assert ob["value_type"] == {"kind": "int", "tolerance": "5"}
    assert "scope" not in ob  # removed — the population is in witness_code
    assert ob["witness_code"] == "source.select('n')"
    assert ob["witness_hash"] == _witness_hash("source.select('n')")
    assert ob["witness_hash"].startswith("sha256:")


@pytest.mark.core
def test_witness_hash_is_stable_and_empty_for_no_witness():
    assert _witness_hash("") == ""
    h = _witness_hash("source.select('n')")
    assert h.startswith("sha256:") and h == _witness_hash("source.select('n')")
    assert _witness_hash("source.select('m')") != h  # content-addressed


@pytest.mark.core
def test_catalog_state_pins_manifest_and_fails_open(tmp_path, monkeypatch):
    # A read manifest pins the catalog state onto the certificate; a missing one
    # leaves it unpinned ("") — transparency fails open, never a verdict.
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    (tmp_path / "catalog.yaml").write_text("entries: {}\n")
    cert = certificate_for_request(dict(_argmax_request(), catalog_path=str(tmp_path)))
    assert cert.catalog_state == _catalog_state(str(tmp_path))
    assert cert.catalog_state.startswith("sha256:")
    missing = certificate_for_request(
        dict(_argmax_request(), catalog_path=str(tmp_path / "nope"))
    )
    assert missing.catalog_state == ""


@pytest.mark.core
def test_obligation_from_dict_coerces_nondict_subobjects():
    # A producer/LLM may send predicate/value_type as a string instead of an
    # object, or on/population as non-strings; coerce to empty rather than raising
    # (fail closed later).
    ob = obligation_from_dict(
        {"id": "c", "kind": "scalar", "surface": "6", "on": 42,
         "population": ["x"], "predicate": "the max", "value_type": "int"}
    )
    assert ob.predicate.select == "" and ob.on == "" and ob.population == ""
    assert ob.value_type.kind == "int"


@pytest.mark.core
def test_obligation_from_dict_rejects_nondict_obligation():
    with pytest.raises(ValueError):
        obligation_from_dict("just a string")


@pytest.mark.core
def test_obligation_from_dict_rejects_bool_requires_sources():
    # LLM producers guessed `requires_sources: true` in nearly every run;
    # tuple(True) surfaced as the impenetrable "'bool' object is not iterable".
    # The error must now say what the field is and how to fix the request.
    base = {"id": "c", "kind": "scalar", "surface": "6", "on": "a", "predicate": {}}
    with pytest.raises(ValueError, match="list of source URLs"):
        obligation_from_dict(dict(base, requires_sources=True))
    # A single URL string is unambiguous — coerce, don't reject.
    ob = obligation_from_dict(dict(base, requires_sources="https://h/d.csv"))
    assert ob.requires_sources == ("https://h/d.csv",)
    # predicate.columns has the same trap: a bare string must not iterate chars.
    ob = obligation_from_dict(
        {"id": "t", "kind": "table",
         "predicate": {"columns": "state", "rows": [{"state": "x"}]}}
    )
    assert ob.predicate.columns == ("state",)


@pytest.mark.core
def test_table_obligation_needs_no_surface_but_value_kind_does():
    # A table's content is its grid, so surface is optional — a missing one must
    # NOT raise (that KeyError previously failed the whole request, losing every
    # obligation). A value kind still requires surface (the claimed value).
    ob = obligation_from_dict(
        {"id": "t", "kind": "table",
         "predicate": {"columns": ["s"], "rows": [{"s": "x"}]}}
    )
    assert ob.surface == "" and ob.kind is ClaimKind.TABLE
    with pytest.raises(KeyError):
        obligation_from_dict({"id": "c", "kind": "scalar", "predicate": {}})


@pytest.mark.core
def test_gate_cli_malformed_obligation_is_exit_2(tmp_path):
    # An obligation that is a bare string must not crash the gate — clean exit 2.
    req = {"catalog_path": ".x", "obligations": ["just a string"]}
    result = CliRunner().invoke(cli, ["gate", _write(tmp_path, req)])
    assert result.exit_code == 2


@pytest.mark.core
def test_verify_cli_malformed_obligation_is_exit_2(tmp_path):
    req = {"catalog_path": ".x", "obligations": [{"id": "c", "kind": "nope", "surface": "1"}]}
    result = CliRunner().invoke(cli, ["verify", _write(tmp_path, req)])
    assert result.exit_code == 2


@pytest.mark.core
def test_gate_cli_noop_passes(tmp_path):
    result = CliRunner().invoke(
        cli, ["gate", _write(tmp_path, {"catalog_path": ".x", "obligations": []})]
    )
    assert result.exit_code == 0


@pytest.mark.core
def test_gate_cli_verified_passes(tmp_path, monkeypatch):
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    result = CliRunner().invoke(cli, ["gate", _write(tmp_path, _argmax_request("17,875"))])
    assert result.exit_code == 0


@pytest.mark.core
def test_gate_cli_discrepancy_fails(tmp_path, monkeypatch):
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ATL", "n": "17875"}), extremum="17875"
    )
    result = CliRunner().invoke(cli, ["gate", _write(tmp_path, _argmax_request("99,999"))])
    assert result.exit_code == 1


@pytest.mark.core
def test_gate_cli_maximality_exploit_fails(tmp_path, monkeypatch):
    # A witness that lands ORD/12,055 while the true max is 17,875 must fail the gate.
    _stub_witness(
        monkeypatch, _run(("origin", "n"), {"origin": "ORD", "n": "12055"}), extremum="17875"
    )
    req = _argmax_request("12,055")
    req["obligations"][0]["predicate"]["entity_val"] = "ORD"
    result = CliRunner().invoke(cli, ["gate", _write(tmp_path, req)])
    assert result.exit_code == 1


@pytest.mark.core
def test_gate_cli_allow_override_passes(tmp_path, monkeypatch):
    # Missing metric column ⇒ COULD-NOT-VERIFY; fails by default, allowed explicitly.
    _stub_witness(monkeypatch, _run(("origin",), {"origin": "ATL"}), extremum="17875")
    result = CliRunner().invoke(
        cli,
        ["gate", "--allow", "COULD-NOT-VERIFY", _write(tmp_path, _argmax_request("17,875"))],
    )
    assert result.exit_code == 0


@pytest.mark.core
def test_gate_cli_malformed_request_is_exit_2(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{ not json")
    result = CliRunner().invoke(cli, ["gate", str(path)])
    assert result.exit_code == 2


# --------------------------------------------------------------------------- #
# Shipping: `prompt` prints the role; `init` sets up a consumer's AGENTS.md     #
# --------------------------------------------------------------------------- #


@pytest.mark.core
def test_prompt_cli_prints_role():
    out = CliRunner().invoke(cli, ["prompt"]).output
    assert "xorq analyst" in out
    assert "Iron rule" in out


@pytest.mark.core
def test_init_creates_agents_md(tmp_path):
    result = CliRunner().invoke(cli, ["init", "--path", str(tmp_path)])
    assert result.exit_code == 0
    agents = (tmp_path / "AGENTS.md").read_text()
    assert "pi-xorq-verifier:analyst BEGIN" in agents
    assert "pi-xorq-verifier:analyst END" in agents
    assert "Iron rule" in agents


@pytest.mark.core
def test_init_is_idempotent(tmp_path):
    runner = CliRunner()
    runner.invoke(cli, ["init", "--path", str(tmp_path)])
    first = (tmp_path / "AGENTS.md").read_text()
    result = runner.invoke(cli, ["init", "--path", str(tmp_path)])
    assert "already up to date" in result.output
    assert (tmp_path / "AGENTS.md").read_text() == first
    # exactly one managed block
    assert first.count("pi-xorq-verifier:analyst BEGIN") == 1


@pytest.mark.core
def test_init_preserves_surrounding_content(tmp_path):
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# My project\n\nhouse rules\n")
    CliRunner().invoke(cli, ["init", "--path", str(tmp_path)])
    text = agents.read_text()
    assert "# My project" in text and "house rules" in text
    assert "pi-xorq-verifier:analyst BEGIN" in text


@pytest.mark.core
def test_init_catalog_path_is_baked_in(tmp_path):
    CliRunner().invoke(
        cli, ["init", "--path", str(tmp_path), "--catalog-path", "data/cat"]
    )
    assert "Default catalog path: `data/cat`" in (tmp_path / "AGENTS.md").read_text()


@pytest.mark.core
def test_init_print_does_not_write(tmp_path):
    result = CliRunner().invoke(cli, ["init", "--path", str(tmp_path), "--print"])
    assert "pi-xorq-verifier:analyst BEGIN" in result.output
    assert not (tmp_path / "AGENTS.md").exists()


