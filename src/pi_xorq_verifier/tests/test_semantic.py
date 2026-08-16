"""The BSL-measure exemption in ``selection_only`` (witness._has_arithmetic).

A semantic model's measure arithmetic lives in the alias's own tag metadata
and expands only when queried by name — invoking a reviewed measure is
selection, not computation. Arithmetic matching no declared measure must keep
failing, and aliases without a BSL tag must be untouched by the exemption.
"""

import xorq.api as xo
from boring_semantic_layer import to_semantic_table, to_tagged

from pi_xorq_verifier.witness import _has_arithmetic


def _csv_table(tmp_path):
    # A deferred file read, like every real cataloged alias — NOT a memtable:
    # memtable relations mint a fresh uuid name on every builder rehydration,
    # so their expansions are never node-equal across accesses.
    path = tmp_path / "t.csv"
    path.write_text("a,b\n1,10\n2,20\n")
    return xo.deferred_read_csv(str(path), xo.pandas.connect(), table_name="t")


def _model_alias(tmp_path):
    model = to_semantic_table(_csv_table(tmp_path), name="m").with_measures(
        ratio=lambda t: t.a.sum() / t.b.sum() * 100,
    )
    return to_tagged(model)


def test_measure_by_name_is_selection(tmp_path):
    alias = _model_alias(tmp_path)
    witness = to_tagged(alias.ls.builder.query(measures=["ratio"]))
    assert not _has_arithmetic(witness, alias)


def test_foreign_arithmetic_still_flagged(tmp_path):
    alias = _model_alias(tmp_path)
    witness = alias.aggregate(x=alias.a.sum() / alias.b.count())
    assert _has_arithmetic(witness, alias)


def test_untagged_alias_unaffected(tmp_path):
    t = _csv_table(tmp_path)
    witness = t.aggregate(x=t.a.sum() / t.a.count())
    assert _has_arithmetic(witness, t)


def test_check_hint_points_at_by_name_declaration(tmp_path):
    from pi_xorq_verifier.witness import check_hint

    hint = check_hint(_model_alias(tmp_path), None, (("selection_only", False),))
    assert "ratio" in hint
    assert '"measures"' in hint


def test_semantic_query_by_name(tmp_path):
    from pi_xorq_verifier.checker import Predicate
    from pi_xorq_verifier.witness import _has_arithmetic, _semantic_query

    alias = _model_alias(tmp_path)
    q = _semantic_query(alias, Predicate(select="ratio", measures=("ratio",)))
    assert q is not None
    assert not _has_arithmetic(q, alias)
    assert _semantic_query(alias, Predicate(measures=("nope",))) is None


def test_check_hint_silent_when_checks_pass(tmp_path):
    from pi_xorq_verifier.witness import check_hint

    assert check_hint(_model_alias(tmp_path), None, (("selection_only", True),)) == ""


def test_select_hint_names_measures(tmp_path):
    from pi_xorq_verifier.witness import _semantic_select_hint

    hint = _semantic_select_hint(_model_alias(tmp_path), "source.select('ratio')")
    assert "ratio" in hint and "ls.builder.query" in hint
    assert _semantic_select_hint(_csv_table(tmp_path), "source.select('x')") == ""
