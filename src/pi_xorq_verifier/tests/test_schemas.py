"""The JSON contracts (schemas/*.json) must accept what the checker/extension
actually emit and consume. These tests are the guard that the schemas and the code
never silently drift — e.g. a request field the extension injects that the schema
forbids under ``additionalProperties: false``.
"""

import json
from pathlib import Path

import pytest

from pi_xorq_verifier.checker import (
    ClaimKind,
    Obligation,
    Predicate,
    ValueType,
    certificate_to_dict,
    check_obligations,
    obligation_from_dict,
)


jsonschema = pytest.importorskip("jsonschema")

REPO = Path(__file__).resolve().parents[3]
SCHEMAS = REPO / "schemas"


def _schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text())


@pytest.mark.core
def test_sample_request_matches_request_schema():
    request = json.loads((REPO / "sample" / "answer_request.json").read_text())
    jsonschema.validate(request, _schema("request.schema.json"))


@pytest.mark.core
def test_catalog_witnesses_is_allowed_by_request_schema():
    # The extension injects catalog_witnesses into every request; the schema is
    # additionalProperties:false, so it must list the field or reject every request.
    request = {
        "catalog_path": ".xorq/catalog",
        "catalog_witnesses": True,
        "obligations": [{
            "id": "c", "kind": "argmax", "surface": "1",
            "on": "a",
            "predicate": {"select": "n", "metric_col": "n"},
        }],
    }
    jsonschema.validate(request, _schema("request.schema.json"))


@pytest.mark.core
def test_certificate_output_matches_certificate_schema(monkeypatch):
    # A certificate produced by the checker (via a stubbed witness so no engine is
    # needed) must validate against certificate.schema.json — including the `sources`
    # field and the exact check/status/verdict spellings.
    from pi_xorq_verifier import witness as witness_mod
    from pi_xorq_verifier.checker import WitnessRun

    run = WitnessRun(("origin", "n"), ((("origin", "ATL"), ("n", "17875")),))
    monkeypatch.setattr(witness_mod, "load_alias_expr", lambda *a, **k: object())
    monkeypatch.setattr(witness_mod, "build_witness", lambda *a, **k: object())
    monkeypatch.setattr(
        witness_mod, "validate_witness",
        lambda *a, **k: (("witness_on_declared_alias", True),),
    )
    monkeypatch.setattr(witness_mod, "run_expr", lambda *a, **k: run)
    monkeypatch.setattr(witness_mod, "recompute_extremum", lambda *a, **k: "17875")
    monkeypatch.setattr(witness_mod, "alias_sources", lambda *a, **k: ("https://h/d.csv",))
    monkeypatch.setattr(witness_mod, "magic_constants", lambda *a, **k: ())
    monkeypatch.setattr(witness_mod, "witness_code", lambda *a, **k: "source.select('origin', 'n')")

    ob = Obligation(
        id="c1", kind=ClaimKind.ARGMAX, surface="17,875",
        on="flights-by-origin",
        predicate=Predicate(select="n", entity_col="origin", entity_val="ATL", metric_col="n"),
        value_type=ValueType(kind="int", tolerance="0"),
    )
    cert = check_obligations((ob,), "unused", reply_values=("17,875",))
    payload = certificate_to_dict(cert)
    jsonschema.validate(payload, _schema("certificate.schema.json"))


@pytest.mark.core
def test_obligation_roundtrips_through_request_schema():
    # obligation_from_dict must accept every obligation the request schema permits.
    ob_dict = {
        "id": "c", "kind": "table", "surface": "",
        "on": "a", "population": "source.filter(source.n >= 25)",
        "predicate": {"columns": ["origin", "n"], "ordered": True, "metric_col": "n",
                      "rows": [{"origin": "ATL", "n": "17875"}]},
    }
    jsonschema.validate(
        {"catalog_path": "x", "obligations": [ob_dict]},
        _schema("request.schema.json"),
    )
    ob = obligation_from_dict(ob_dict)
    assert ob.kind is ClaimKind.TABLE
