"""End-to-end discharge against a live xorq catalog (ADR-0001 §2).

These exercise the real ``xorq catalog run`` path — the checker composes each
witness on the ``flights-by-origin`` alias, selects the cell, and folds a real
verdict. They skip when xorq / the sample catalog are unavailable (see conftest).
"""

import pytest

from pi_xorq_verifier.api import (
    ClaimKind,
    Obligation,
    Predicate,
    ValueType,
    Verdict,
    check_obligations,
)


pytestmark = pytest.mark.integration


def _argmax(surface="17,875", entity="ATL", sources=()) -> Obligation:
    # no population ⇒ it is the whole alias; the checker synthesizes
    # the ranking and recomputes the extremum over that same population.
    return Obligation(
        id="c1",
        kind=ClaimKind.ARGMAX,
        surface=surface,
        on="flights-by-origin",
        predicate=Predicate(
            select="n", entity_col="origin", entity_val=entity,
            metric_col="n",
        ),
        value_type=ValueType(kind="int"),
        requires_sources=tuple(sources),
    )


def test_argmax_verified(sample_catalog):
    cert = check_obligations((_argmax(),), sample_catalog)
    assert cert.verdict is Verdict.VERIFIED
    assert cert.results[0].selected_cell == "17875"


def test_wrong_value_is_discrepancy(sample_catalog):
    cert = check_obligations((_argmax(surface="99,999"),), sample_catalog)
    assert cert.verdict is Verdict.DISCREPANCY


def test_wrong_entity_is_discrepancy(sample_catalog):
    cert = check_obligations((_argmax(entity="ORD"),), sample_catalog)
    assert cert.verdict is Verdict.DISCREPANCY


def test_count_aggregate_verified(sample_catalog):
    ob = Obligation(
        id="c1",
        kind=ClaimKind.COUNT,
        surface="4",
        on="flights-by-origin",  # no population = the whole alias
        predicate=Predicate(select="n"),
        value_type=ValueType(kind="int"),
    )
    assert check_obligations((ob,), sample_catalog).verdict is Verdict.VERIFIED


def test_ungrounded_scalar_synthesized_from_select(sample_catalog):
    # No `expression`: predicate.select names a real column, so the checker
    # synthesizes the selection itself (the shape every producer tries first —
    # this used to dead-end in an "needs `expression`" retry loop). The
    # population narrows the alias to one row so the cell is unambiguous.
    ob = Obligation(
        id="c1",
        kind=ClaimKind.SCALAR,
        surface="17,875",
        on="flights-by-origin",
        population="source.filter(source.origin == 'ATL')",
        predicate=Predicate(select="n"),
        value_type=ValueType(kind="int"),
    )
    cert = check_obligations((ob,), sample_catalog)
    assert cert.verdict is Verdict.VERIFIED
    assert cert.results[0].selected_cell == "17875"
    assert cert.results[0].witness_code.endswith(".select('n')")


def test_ungrounded_scalar_ambiguous_select_fails_closed(sample_catalog):
    # The synthesized selection over a multi-row population holds distinct
    # cells — the claim's grain does not match, and it must NOT discharge
    # against whichever row comes first.
    ob = Obligation(
        id="c1",
        kind=ClaimKind.SCALAR,
        surface="17,875",
        on="flights-by-origin",
        predicate=Predicate(select="n"),
        value_type=ValueType(kind="int"),
    )
    assert check_obligations((ob,), sample_catalog).verdict is Verdict.COULD_NOT_VERIFY


def test_missing_alias_could_not_verify(sample_catalog):
    ob = Obligation(
        id="c1",
        kind=ClaimKind.SCALAR,
        surface="1",
        on="no-such-alias", expression="source.select('n')",
        predicate=Predicate(select="n"),
        value_type=ValueType(kind="int"),
    )
    assert check_obligations((ob,), sample_catalog).verdict is Verdict.COULD_NOT_VERIFY


def test_provenance_confirmed_verified(sample_catalog):
    cert = check_obligations(
        (_argmax(sources=["flights.csv"]),),
        sample_catalog,
        expressions=(("flights-by-origin", ("flights.csv",)),),
    )
    assert cert.verdict is Verdict.VERIFIED


def test_provenance_unconfirmed_could_not_verify(sample_catalog):
    cert = check_obligations(
        (_argmax(sources=["flights.csv"]),),
        sample_catalog,
        expressions=(("flights-by-origin", ("other.csv",)),),
    )
    assert cert.verdict is Verdict.COULD_NOT_VERIFY


def test_uncovered_reply_value_downgrades(sample_catalog):
    cert = check_obligations(
        (_argmax(),), sample_catalog, reply_values=("17,875", "6,300")
    )
    assert cert.verdict is Verdict.COULD_NOT_VERIFY
    assert "6,300" in cert.uncovered
