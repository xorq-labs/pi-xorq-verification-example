"""Tests for the deterministic source-lineage checker (pure-logic + one bundle).

Mirrors xorq-desktop's approach: every structural case calls ``check()`` with
synthetic expr/profiles/meta dicts — no zip, no engine, no network.
"""

import json
import zipfile
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

import pi_xorq_verifier.lineage as lineage_mod
from pi_xorq_verifier.cli import cli
from pi_xorq_verifier.lineage import (
    COULD_NOT_VERIFY,
    DISCREPANCY,
    VERIFIED,
    check,
    check_alias,
)


# A minimal traversable DAG: root HashingTag (0) -> Read (1); and a broken one.
# Pair-shaped edges are the v1 format (xorq <= 0.3.32).
_OK = {"lineage": {"nodes": [{"id": "0", "type": "HashingTag"}, {"id": "1", "type": "Read"}],
                   "edges": [["0", "1"]], "root": "0"}}
_BROKEN = {"lineage": {"nodes": [{"id": "0", "type": "HashingTag"}, {"id": "1", "type": "Read"}],
                       "edges": [], "root": "0"}}

# The v2 lineage shape (xorq >= 0.3.38), sampled from a real emitted bundle:
# ``version`` marker, ``@``-prefixed ids, dict edges with from/to/scope, overlays.
_OK_V2 = {"lineage": {
    "version": 2,
    "root": "@project_1",
    "nodes": [
        {"id": "@project_1", "type": "Project", "is_boundary": False},
        {"id": "@read_2", "type": "Read", "is_boundary": True},
    ],
    "edges": [{"from": "@project_1", "to": "@read_2", "scope": "root"}],
    "overlays": {},
}}


def _expr(path: str, profile: str = "p0", op: str = "Read") -> dict:
    return {"definitions": {"nodes": {"@r": {
        "op": op, "method_name": "read_csv", "profile": profile,
        "read_kwargs": [["hash_path", path], ["table_name", "t"]],
    }}}}


def _profiles(profile: str = "p0", con: str = "pandas") -> dict:
    return {profile: {"con_name": con}}


@pytest.mark.core
@pytest.mark.parametrize("uri", ("s3://bucket/data.parquet", "https://h/d.csv", "gs://b/x"))
def test_remote_source_legit(uri):
    r = check(_expr(uri), _profiles(), _OK, None, "a")
    assert r["legit"] and r["verdict"] == VERIFIED


@pytest.mark.core
def test_missing_profile_flagged():
    r = check(_expr("s3://b/d"), {}, _OK, None, "a")
    assert not r["legit"] and r["verdict"] == DISCREPANCY
    assert any("absent from profiles" in i for i in r["issues"])


@pytest.mark.core
def test_local_missing_path_flagged(tmp_path):
    r = check(_expr(str(tmp_path / "nope.csv")), _profiles(), _OK, None, "a")
    assert not r["legit"]
    assert any("does not exist" in i for i in r["issues"])


@pytest.mark.core
def test_local_existing_path_ok_by_default(tmp_path):
    p = tmp_path / "real.csv"
    p.write_text("a\n1\n")
    r = check(_expr(str(p)), _profiles(), _OK, None, "a")
    assert r["legit"]  # a local source that exists is fine under the default policy


@pytest.mark.core
def test_no_local_rejects_existing_local(tmp_path):
    p = tmp_path / "real.csv"
    p.write_text("a\n1\n")
    r = check(_expr(str(p)), _profiles(), _OK, None, "a", no_local=True)
    assert not r["legit"]
    assert any("local source not allowed" in i for i in r["issues"])


@pytest.mark.core
def test_no_local_allows_remote():
    r = check(_expr("https://h/d.csv"), _profiles(), _OK, None, "a", no_local=True)
    assert r["legit"]


@pytest.mark.core
def test_untraversable_lineage_flagged():
    r = check(_expr("s3://b/d"), _profiles(), _BROKEN, None, "a")
    assert not r["legit"]
    assert any("does not reach a source" in i for i in r["issues"])


@pytest.mark.core
def test_v2_dict_edges_traversable():
    # Regression: xorq 0.3.38 switched edges from pairs to from/to/scope dicts;
    # unpacking them as pairs crashed the checker (ValueError) instead of verifying.
    r = check(_expr("https://h/d.csv"), _profiles(), _OK_V2, None, "a")
    assert r["traversable"] and r["legit"] and r["verdict"] == VERIFIED


@pytest.mark.core
def test_v2_broken_dag_still_flagged():
    broken = {"lineage": {**_OK_V2["lineage"], "edges": []}}
    r = check(_expr("https://h/d.csv"), _profiles(), broken, None, "a")
    assert not r["legit"]
    assert any("does not reach a source" in i for i in r["issues"])


@pytest.mark.core
def test_unrecognized_edge_shape_fails_closed(tmp_path):
    # A future emitter shape must surface as COULD-NOT-VERIFY at the bundle
    # boundary — never a crash, never a silent pass or false DISCREPANCY.
    meta = {"lineage": {**_OK_V2["lineage"],
                        "edges": [{"src": "@project_1", "dst": "@read_2"}]}}
    (tmp_path / "aliases").mkdir()
    (tmp_path / "entries").mkdir()
    with zipfile.ZipFile(tmp_path / "entries" / "abc123.zip", "w") as zf:
        zf.writestr("abc123/expr.yaml", yaml.safe_dump(_expr("https://h/d.csv")))
        zf.writestr("abc123/profiles.yaml", yaml.safe_dump(_profiles()))
        zf.writestr("abc123/expr_metadata.json", json.dumps(meta))
    r = check_alias(str(tmp_path), "abc123")
    assert r["verdict"] == COULD_NOT_VERIFY
    assert not r["legit"]
    assert any("unrecognized" in i for i in r["issues"])


@pytest.mark.core
def test_no_source_node_flagged():
    r = check({"definitions": {"nodes": {}}}, {}, _OK, None, "a")
    assert not r["legit"]
    assert any("no Read" in i for i in r["issues"])


@pytest.mark.core
def test_composed_from_fabricated_flagged(tmp_path):
    (tmp_path / "entries").mkdir()
    (tmp_path / "aliases").mkdir()
    (tmp_path / "entries" / "realhash.zip").write_text("")  # a present catalog entry
    meta = {**_OK, "composed_from": [{"entry_name": "ghosthash", "alias": "ghost"}]}
    r = check(_expr("s3://b/d"), _profiles(), meta, tmp_path, "a")
    assert not r["legit"]
    assert any("fabricated lineage" in i for i in r["issues"])


@pytest.mark.core
def test_check_alias_missing_is_could_not_verify(tmp_path):
    (tmp_path / "aliases").mkdir()
    (tmp_path / "entries").mkdir()
    r = check_alias(str(tmp_path), "nope")
    assert r["verdict"] == COULD_NOT_VERIFY
    assert not r["legit"]


@pytest.mark.core
def test_lineage_cli_missing_alias_exits_zero_with_verdict(tmp_path):
    # The lineage subcommand always exits 0; the verdict lives in the JSON.
    (tmp_path / "aliases").mkdir()
    (tmp_path / "entries").mkdir()
    res = CliRunner().invoke(
        cli, ["lineage", "--alias", "nope", "--catalog-path", str(tmp_path)]
    )
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert payload["verdict"] == COULD_NOT_VERIFY
    assert payload["legit"] is False


@pytest.mark.core
def test_lineage_cli_no_local_from_flag_and_env(tmp_path, monkeypatch):
    # --no-local and PI_XORQ_NO_LOCAL_SOURCES both force the strict policy; the env
    # override uses the SAME truthy parsing as verify/gate (checker.env_flag).
    captured: dict = {}

    def fake_check_alias(catalog_path, alias, no_local):
        captured["no_local"] = no_local
        return {"alias": alias, "verdict": COULD_NOT_VERIFY, "legit": False,
                "sources": [], "issues": [], "traversable": False,
                "upstream_entries": [], "detail": ""}

    monkeypatch.setattr(lineage_mod, "check_alias", fake_check_alias)
    args = ["lineage", "--alias", "a", "--catalog-path", str(tmp_path)]

    CliRunner().invoke(cli, args)
    assert captured["no_local"] is False
    CliRunner().invoke(cli, [*args, "--no-local"])
    assert captured["no_local"] is True
    monkeypatch.setenv("PI_XORQ_NO_LOCAL_SOURCES", "yes")
    CliRunner().invoke(cli, args)  # env forces it on without the flag
    assert captured["no_local"] is True


@pytest.mark.core
def test_lineage_cli_checker_error_is_could_not_verify(tmp_path, monkeypatch):
    # A checker bug must still honor the exit-0 JSON-verdict contract: the calling
    # gate needs a machine-readable refusal, not a traceback.
    def boom(catalog_path, alias, no_local):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(lineage_mod, "check_alias", boom)
    res = CliRunner().invoke(
        cli, ["lineage", "--alias", "a", "--catalog-path", str(tmp_path)]
    )
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert payload["verdict"] == COULD_NOT_VERIFY
    assert "kaboom" in payload["detail"]


@pytest.mark.core
def test_check_alias_reads_real_bundles():
    # End-to-end canary against every alias the local catalog holds: reads the
    # real entry bundles the CURRENT xorq emits (zip + yaml, no engine), so an
    # emitter format drift breaks this test on upgrade day, not mid-demo. Skips
    # only when no catalog alias is present at all. (Its predecessor hard-coded
    # one alias name and silently skipped for weeks once that alias was gone.)
    cat = Path(".xorq/catalog")
    aliases = sorted((cat / "aliases").glob("*.zip")) if (cat / "aliases").is_dir() else ()
    if not aliases:
        pytest.skip("no local catalog alias to read")
    for alias_zip in aliases:
        r = check_alias(str(cat), alias_zip.stem)
        assert r["verdict"] != COULD_NOT_VERIFY, r  # bundle readable, shape recognized
        assert r["sources"], r
