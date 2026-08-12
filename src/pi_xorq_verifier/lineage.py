"""Deterministic source-lineage checker for a catalog entry.

A SEPARATE deterministic surface from the faithfulness checker (``checker.py``):
that one grounds *values*; this grounds the *source*. It reads the ACTUAL
data-source arguments xorq already serialized into an entry bundle and reports
structural legitimacy, so a gate — or the verifier calling it as a tool — can
catch a hallucinated, broken, or non-reproducible source WITHOUT trusting the
agent's prose. It never reaches the network and never computes; it only reads:

  - ``expr.yaml``          the source ops (Read / RemoteTable / DatabaseTable):
                           ``method_name``, ``read_kwargs`` (the real path/URI +
                           table), and the profile id;
  - ``profiles.yaml``      profile id -> connection backend (``con_name``);
  - ``expr_metadata.json`` ``lineage`` (nodes/edges/root, for traversability) and
                           ``composed_from`` (upstream catalog entry names).

Structural, fail-closed: a profile must resolve; a remote URI (http/s3/gs/...) is
trusted when well-formed; a local path must EXIST; every ``composed_from`` entry
must be a real catalog entry (no fabricated lineage); the lineage DAG must reach a
source leaf from its root. Under the optional ``no_local`` policy, a source must be
remote (re-fetchable) — a local/in-memory path is rejected even if it exists.
Prose↔args attribution ("does the answer describe where the data came from") is a
separate, LLM-side job, not this module's.

This mirrors the pattern in xorq-desktop's ``check_source_lineage.py``.
"""

from __future__ import annotations

import json
import zipfile
from functools import cache
from pathlib import Path
from urllib.parse import urlparse

import yaml

from pi_xorq_verifier.checker import PATH_KEYS, REMOTE_SCHEMES, Verdict


SOURCE_OPS = frozenset({"Read", "RemoteTable", "DatabaseTable"})

# Verdicts: the SAME vocabulary as the faithfulness certificate (checker.Verdict),
# so a consumer comparing a lineage verdict to a certificate verdict (the pi answer
# gate does) never sees a divergent spelling.
VERIFIED = Verdict.VERIFIED.value
DISCREPANCY = Verdict.DISCREPANCY.value
COULD_NOT_VERIFY = Verdict.COULD_NOT_VERIFY.value


def _kwargs_to_dict(read_kwargs: object) -> dict[str, object]:
    """xorq serializes ``read_kwargs`` as a list of ``[key, value]`` pairs."""
    match read_kwargs:
        case list():
            return {k: v for pair in read_kwargs if len(pair) == 2 for k, v in [pair]}
        case dict():
            return dict(read_kwargs)
        case _:
            return {}


def _source_path(kwargs: dict[str, object]) -> str | None:
    return next((str(kwargs[k]) for k in PATH_KEYS if k in kwargs), None)


def _is_remote(path: str) -> bool:
    return urlparse(path).scheme in REMOTE_SCHEMES


def _extract_sources(expr: dict, profiles: dict) -> tuple[dict, ...]:
    """Every Read/RemoteTable/DatabaseTable node with its real args + connection."""
    nodes = (expr.get("definitions", {}) or {}).get("nodes", {}) or {}
    out: list[dict] = []
    for ref, node in nodes.items():
        if not isinstance(node, dict) or node.get("op") not in SOURCE_OPS:
            continue
        kwargs = _kwargs_to_dict(node.get("read_kwargs"))
        profile = node.get("profile")
        con = (profiles.get(profile) or {}).get("con_name") if profile else None
        out.append(
            {
                "node": ref,
                "op": node.get("op"),
                "method": node.get("method_name"),
                "path": _source_path(kwargs) or node.get("table"),
                "table": kwargs.get("table_name") or node.get("table"),
                "profile": profile,
                "con_name": con,
                "snapshot_hash": node.get("snapshot_hash"),
            }
        )
    return tuple(out)


def _edge_endpoints(edge: object) -> tuple[str, str]:
    """One lineage edge's (parent, child), across emitter format versions.

    xorq <= 0.3.32 serialized an edge as a pair ``["0", "1"]``; >= 0.3.38
    (lineage ``version: 2``) as ``{"from": ..., "to": ..., "scope": ...}``.
    An unrecognized shape raises — ``check_alias`` maps that to
    ``COULD-NOT-VERIFY`` (fail-closed), never a silent wrong traversal.
    """
    match edge:
        case {"from": a, "to": b}:
            return (str(a), str(b))
        case [a, b]:
            return (str(a), str(b))
        case _:
            raise ValueError(f"unrecognized lineage edge shape: {edge!r}")


def _traversable(meta: dict) -> bool:
    """Is there a path from the lineage root to a real source leaf?"""
    lineage = meta.get("lineage") or {}
    nodes = {n["id"]: n for n in lineage.get("nodes", []) if "id" in n}
    edges = lineage.get("edges") or []
    root = lineage.get("root")
    if not nodes or root is None:
        return False
    adj: dict[str, list[str]] = {}
    for a, b in map(_edge_endpoints, edges):
        adj.setdefault(a, []).append(b)
    seen: set[str] = set()
    stack = [str(root)]
    hit_source = False
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if nodes.get(cur, {}).get("type") in SOURCE_OPS:
            hit_source = True
        stack.extend(adj.get(cur, []))
    return hit_source


@cache
def _catalog_entry_names(catalog: Path) -> frozenset[str]:
    entries = catalog / "entries"
    if not entries.is_dir():
        return frozenset()
    return frozenset(p.stem for p in entries.glob("*.zip"))


@cache
def _catalog_alias_targets(catalog: Path) -> tuple[tuple[str, str], ...]:
    """Alias symlink targets present in the catalog (alias -> entry hash)."""
    aliases = catalog / "aliases"
    entries = catalog / "entries"
    if not aliases.is_dir() or not entries.is_dir():
        return ()
    entries_root = entries.resolve()
    pairs: list[tuple[str, str]] = []
    for alias_zip in aliases.glob("*.zip"):
        try:
            target = alias_zip.resolve(strict=True)
        except OSError:
            continue
        if target.parent == entries_root and target.suffix == ".zip":
            pairs.append((alias_zip.stem, target.stem))
    return tuple(sorted(pairs))


def check(
    expr: dict,
    profiles: dict,
    meta: dict,
    catalog: Path | None,
    alias: str | None,
    no_local: bool = False,
) -> dict:
    """Structural legitimacy of a single entry's source lineage (pure)."""
    sources = _extract_sources(expr, profiles)
    composed = tuple(meta.get("composed_from") or ())
    issues: list[str] = []

    if not sources:
        issues.append(
            "no Read/RemoteTable/DatabaseTable source node — the entry has no data source"
        )

    for s in sources:
        if s["profile"] and s["con_name"] is None:
            issues.append(
                f"source {s['op']}({s['node']}) uses profile {s['profile']!r} "
                f"absent from profiles.yaml"
            )
        path = s["path"]
        remote = isinstance(path, str) and _is_remote(path)
        # Snapshot sources read a bound snapshot of another catalog entry; the
        # composed-from check validates it exists. Skip the local-path-exists test.
        if not s["snapshot_hash"] and isinstance(path, str) and path and not remote:
            scheme = urlparse(path).scheme
            if not scheme or scheme == "file":
                local = Path(urlparse(path).path if scheme == "file" else path)
                if not (
                    local.exists()
                    or (catalog and not local.is_absolute() and (catalog / local).exists())
                ):
                    issues.append(f"local source path does not exist: {path}")
        # Optional strict policy: the source must be re-fetchable (remote). A local
        # or in-memory/bundled source — even one that exists — is rejected, because
        # it is not independently reproducible (the tell of hand-added data).
        if no_local and not remote:
            where = path if isinstance(path, str) and path else f"<{s['op']}:{s['table']}>"
            issues.append(
                f"local source not allowed: {s['op']}({s['node']}) reads {where} — "
                "ingest from a re-fetchable URL so the lineage is reproducible"
            )

    known = _catalog_entry_names(catalog) if catalog else frozenset()
    if catalog and known:
        alias_targets = dict(_catalog_alias_targets(catalog))
        for c in composed:
            name = c.get("entry_name")
            if name and name not in known:
                upstream_alias = c.get("alias")
                target = alias_targets.get(str(upstream_alias)) if upstream_alias else None
                if target in known:
                    continue
                issues.append(
                    f"composed-from entry {name!r} (@{c.get('alias')}) not found "
                    f"in catalog entries — fabricated lineage"
                )

    traversable = _traversable(meta)
    if sources and not traversable:
        issues.append("lineage DAG does not reach a source from its root")

    legit = not issues
    if legit:
        descs = ", ".join(
            f"{s['method'] or s['op']}({s['path']}) via {s['con_name'] or '?'}"
            for s in sources
        )
        detail = f"source(s) legitimate: {descs}"
    else:
        detail = "; ".join(issues)

    return {
        "alias": alias,
        "sources": list(sources),
        "upstream_entries": [dict(c) for c in composed],
        "traversable": traversable,
        "legit": legit,
        "issues": issues,
        "verdict": VERIFIED if legit else DISCREPANCY,
        "detail": detail,
    }


# --------------------------------------------------------------------------- #
# Bundle reading + alias resolution                                           #
# --------------------------------------------------------------------------- #


def _read_zip_members(zip_path: Path, *suffixes: str) -> dict[str, str | None]:
    """Read several members in ONE open of the bundle (which may be megabytes) —
    keyed by suffix, ``None`` for any member not present. Avoids re-opening and
    re-scanning the central directory once per member."""
    found: dict[str, str | None] = {suffix: None for suffix in suffixes}
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            for suffix in suffixes:
                if found[suffix] is None and name.endswith(suffix):
                    found[suffix] = zf.read(name).decode("utf-8")
    return found


def _entry_zip_for_alias(catalog: Path, alias: str) -> Path | None:
    """Resolve ``alias`` to its entry bundle: ``aliases/<alias>.zip`` (a symlink
    into ``entries/``), or ``entries/<alias>.zip`` when given an entry hash."""
    alias_zip = catalog / "aliases" / f"{alias}.zip"
    if alias_zip.is_symlink() or alias_zip.exists():
        try:
            return alias_zip.resolve(strict=True)
        except OSError:
            return None
    entry_zip = catalog / "entries" / f"{alias}.zip"
    return entry_zip if entry_zip.exists() else None


def check_alias(catalog_path: str, alias: str, no_local: bool = False) -> dict:
    """Read ``alias``'s entry bundle from the catalog and check its lineage.

    Fail-closed: an unreadable/absent bundle yields ``COULD-NOT-VERIFY`` (not a
    contradiction — the source could not be inspected), never a false pass.
    """
    catalog = Path(catalog_path)
    zip_path = _entry_zip_for_alias(catalog, alias)
    if zip_path is None:
        return {
            "alias": alias,
            "sources": [],
            "legit": False,
            "traversable": False,
            "issues": [f"no catalog entry for alias {alias!r}"],
            "verdict": COULD_NOT_VERIFY,
            "detail": f"no catalog entry for alias {alias!r}",
        }
    try:
        members = _read_zip_members(
            zip_path, "expr.yaml", "profiles.yaml", "expr_metadata.json"
        )
        expr = yaml.safe_load(members["expr.yaml"] or "") or {}
        profiles = yaml.safe_load(members["profiles.yaml"] or "") or {}
        meta = json.loads(members["expr_metadata.json"] or "{}")
    except (OSError, zipfile.BadZipFile, yaml.YAMLError, json.JSONDecodeError) as exc:
        return {
            "alias": alias,
            "sources": [],
            "legit": False,
            "traversable": False,
            "issues": [f"unreadable entry bundle: {exc}"],
            "verdict": COULD_NOT_VERIFY,
            "detail": f"unreadable entry bundle: {exc}",
        }
    try:
        return check(expr, profiles, meta, catalog, alias, no_local)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        # Metadata that parsed but doesn't have the shape this checker expects
        # (e.g. an emitter format newer than the shapes handled above). The source
        # could not be inspected — refuse to verify rather than crash or guess.
        return {
            "alias": alias,
            "sources": [],
            "legit": False,
            "traversable": False,
            "issues": [f"unrecognized bundle metadata shape: {exc}"],
            "verdict": COULD_NOT_VERIFY,
            "detail": f"unrecognized bundle metadata shape: {exc}",
        }
