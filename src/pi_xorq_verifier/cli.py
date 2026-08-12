"""`pi-xorq-check` — run the deterministic verification checker over a request.

Two surfaces over the same decision procedure (see ``docs/adr/0001-*``):

- ``verify`` — discharge a request and print the certificate (always exits 0).
- ``gate``   — the producer-declares CI/agent gate: discharge and exit non-zero
  unless the verdict clears the gate, so a contradicted or unconfirmable number
  never ships silently.
"""

import importlib.resources
import json
from pathlib import Path

import click

from pi_xorq_verifier.checker import (
    DEFAULT_GATE_ALLOW,
    Verdict,
    certificate_for_request,
    certificate_to_dict,
    gate_passes,
)


# The analyst role prompt ships as package data so it is reachable after a plain
# `pip install` (not only from a checkout). `init` writes it into a project's
# AGENTS.md, which pi auto-discovers — so `pi "<question>"` answers
# verified-by-construction with no --append-system-prompt flag.
_ANALYST_BEGIN = (
    "<!-- pi-xorq-verifier:analyst BEGIN "
    "(managed — `pi-xorq-check init` overwrites this block) -->"
)
_ANALYST_END = "<!-- pi-xorq-verifier:analyst END -->"


def _analyst_prompt() -> str:
    return (
        importlib.resources.files("pi_xorq_verifier")
        .joinpath("prompts", "analyst.md")
        .read_text(encoding="utf-8")
    )


def _render_block(catalog_path: str | None) -> str:
    body = _analyst_prompt().strip()
    if catalog_path:
        body = (
            f"Default catalog path: `{catalog_path}` — use it unless the question "
            "names another.\n\n" + body
        )
    return f"{_ANALYST_BEGIN}\n{body}\n{_ANALYST_END}\n"


def _merge_block(existing: str, block: str) -> str:
    """Insert/replace the managed block, preserving the user's other content."""
    if _ANALYST_BEGIN in existing and _ANALYST_END in existing:
        start = existing.index(_ANALYST_BEGIN)
        end = existing.index(_ANALYST_END) + len(_ANALYST_END)
        return existing[:start] + block.strip("\n") + existing[end:]
    if not existing.strip():
        return block
    return existing.rstrip("\n") + "\n\n" + block


@click.group()
def cli() -> None:
    """Deterministic verification checker for xorq catalog claims."""


@cli.command()
@click.argument("request", type=click.File("r"))
@click.option(
    "--catalog-path",
    default=".xorq/catalog",
    help="Catalog path (overridden by request.catalog_path if present).",
)
@click.option(
    "--catalog-witnesses",
    is_flag=True,
    help="Persist each DISCHARGED witness to the catalog as verify-<id> (writes).",
)
@click.option(
    "--no-local-sources",
    is_flag=True,
    help="No-fabricated-inputs policy: reject an alias not backed by re-fetchable "
    "data — a local/scratch path, an in-memory/hand-built table, or a hardcoded "
    "data constant in its arithmetic fails (remote_sources / no_magic_constants).",
)
def verify(
    request: click.utils.LazyFile,
    catalog_path: str,
    catalog_witnesses: bool,
    no_local_sources: bool,
) -> None:
    """Discharge the obligations in REQUEST (JSON) and print a certificate.

    REQUEST shape: {"catalog_path"?, "expressions"?, "reply_values"?,
                    "obligations": [{id, kind, surface, witness, predicate, ...}]}

    Exit codes: 0 = certificate printed, 2 = malformed request.
    """
    try:
        cert = certificate_for_request(
            json.load(request), catalog_path, catalog_witnesses, no_local_sources
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError, AttributeError) as exc:
        click.echo(f"error: malformed request: {exc}", err=True)
        raise SystemExit(2) from exc
    click.echo(json.dumps(certificate_to_dict(cert), indent=2))


@cli.command()
@click.argument("request", type=click.File("r"))
@click.option(
    "--catalog-path",
    default=".xorq/catalog",
    help="Catalog path (overridden by request.catalog_path if present).",
)
@click.option(
    "--allow",
    "allowed",
    multiple=True,
    type=click.Choice([v.value for v in Verdict]),
    help="Verdict that passes the gate (repeatable). Default: VERIFIED, NO-OP.",
)
@click.option(
    "--quiet", is_flag=True, help="Print only the verdict, not the full certificate."
)
@click.option(
    "--catalog-witnesses",
    is_flag=True,
    help="Persist each DISCHARGED witness to the catalog as verify-<id> (writes).",
)
@click.option(
    "--no-local-sources",
    is_flag=True,
    help="No-fabricated-inputs policy: reject an alias not backed by re-fetchable "
    "data — a local/scratch path, an in-memory/hand-built table, or a hardcoded "
    "data constant in its arithmetic fails (remote_sources / no_magic_constants).",
)
def gate(
    request: click.utils.LazyFile,
    catalog_path: str,
    allowed: tuple[str, ...],
    quiet: bool,
    catalog_witnesses: bool,
    no_local_sources: bool,
) -> None:
    """Discharge REQUEST and exit non-zero unless the verdict clears the gate.

    The producer-declares gate for CI or an agent: after declaring the
    obligations that back an answer, gate on the result before publishing.

    Exit codes: 0 = pass, 1 = gate failure (verdict not allowed), 2 = bad request.
    """
    try:
        cert = certificate_for_request(
            json.load(request), catalog_path, catalog_witnesses, no_local_sources
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError, AttributeError) as exc:
        click.echo(f"error: malformed request: {exc}", err=True)
        raise SystemExit(2) from exc

    allow = tuple(Verdict(a) for a in allowed) or DEFAULT_GATE_ALLOW
    passed = gate_passes(cert, allow)
    click.echo(
        cert.verdict.value if quiet else json.dumps(certificate_to_dict(cert), indent=2)
    )
    click.echo(
        f"gate: {'PASS' if passed else 'FAIL'} (verdict={cert.verdict.value}; "
        f"allowed={','.join(v.value for v in allow)})",
        err=True,
    )
    if not passed:
        raise SystemExit(1)


@cli.command()
@click.option("--alias", required=True, help="The catalog alias to check.")
@click.option(
    "--catalog-path", default=".xorq/catalog", help="Catalog path."
)
@click.option(
    "--no-local",
    is_flag=True,
    help="Require a re-fetchable (remote) source — reject a local/in-memory source "
    "even if it exists on disk.",
)
def lineage(alias: str, catalog_path: str, no_local: bool) -> None:
    """Deterministic source-lineage check for one catalog ALIAS.

    Reads the entry bundle (expr.yaml / profiles.yaml / expr_metadata.json) and
    prints a JSON verdict grounded in the alias's ACTUAL serialized source — the
    profile resolves, a local path exists (remote URIs trusted when well-formed),
    composed-from entries are real catalog entries, and the lineage DAG reaches a
    source from its root. Structural only: no engine, no network. Always exits 0;
    the JSON carries `verdict` (VERIFIED / DISCREPANCY / COULD-NOT-VERIFY).
    """
    from pi_xorq_verifier.checker import env_flag  # noqa: PLC0415
    from pi_xorq_verifier.lineage import COULD_NOT_VERIFY, check_alias  # noqa: PLC0415

    # The operator can force the strict (remote-only) policy for lineage too, so a
    # hardcoded/in-memory source is flagged even if --no-local was not passed. Same
    # env_flag the verify/gate path uses, so the policy override is honored uniformly.
    no_local = no_local or env_flag("PI_XORQ_NO_LOCAL_SOURCES")
    try:
        result = check_alias(catalog_path, alias, no_local)
    except Exception as exc:  # noqa: BLE001 — the exit-0 JSON-verdict contract
        # holds even for a checker bug: refusing to verify is the loud, fail-closed
        # outcome; a traceback gives the calling gate nothing machine-readable.
        result = {
            "alias": alias,
            "sources": [],
            "legit": False,
            "traversable": False,
            "issues": [f"lineage checker error: {type(exc).__name__}: {exc}"],
            "verdict": COULD_NOT_VERIFY,
            "detail": f"lineage checker error: {type(exc).__name__}: {exc}",
        }
    click.echo(json.dumps(result, indent=2))


@cli.command()
@click.option("--catalog-path", default=".xorq/catalog", help="Catalog path.")
@click.option("--on", "alias", required=True, help="Declared alias to compose on.")
@click.option("-c", "--code", "compose", required=True, help="Expression over `source`.")
@click.option(
    "--limit",
    default=50,
    show_default=True,
    help="Cap rows by appending .limit(N) when CODE has none; 0 uncaps.",
)
@click.option(
    "--cache-dir",
    default=None,
    help="Snapshot cache directory (default: `select-cache` beside the catalog).",
)
@click.option(
    "--no-cache",
    is_flag=True,
    help="Execute against the live sources (skip the snapshot).",
)
def select(
    catalog_path: str,
    alias: str,
    compose: str,
    limit: int,
    cache_dir: str | None,
    no_cache: bool,
) -> None:
    """Compose -c CODE on a snapshot-cached ALIAS and print the rows as CSV.

    The compute path for *proposing* a number: the alias's sources are fetched
    once into a local parquet snapshot beside the catalog, and every later
    compose on that alias — however different — short-circuits to the snapshot
    instead of re-fetching. Verification never reads this cache: ``verify`` /
    ``gate`` witnesses always re-execute from the declared sources, so the
    certificate, not the snapshot, remains the trust root.

    Exit codes: 0 = rows printed, 1 = the alias or compose failed.
    """
    from pi_xorq_verifier import witness  # noqa: PLC0415

    try:
        rows = witness.cached_select(
            catalog_path,
            alias,
            compose,
            limit=limit,
            cache_dir=cache_dir,
            use_cache=not no_cache,
        )
    except Exception as exc:  # noqa: BLE001 — one actionable line for the calling tool
        hint = (
            " (the safe evaluator rejects `&`/`|` — chain filters instead: "
            "source.filter(a).filter(b))"
            if ("&" in compose or "|" in compose)
            else ""
        )
        click.echo(f"select: {type(exc).__name__}: {exc}{hint}", err=True)
        raise SystemExit(1) from exc
    click.echo(rows, nl=False)


@cli.command()
def prompt() -> None:
    """Print the analyst role prompt (for --append-system-prompt or piping).

    Location-independent — works after a plain ``pip install`` with no checkout:
    ``pi --append-system-prompt <(pi-xorq-check prompt) "<question>"``.
    """
    click.echo(_analyst_prompt(), nl=False)


@cli.command()
@click.option(
    "--path",
    "target_dir",
    default=".",
    type=click.Path(file_okay=False),
    help="Project directory whose AGENTS.md to set up (default: cwd).",
)
@click.option(
    "--catalog-path",
    default=None,
    help="Bake a default catalog path into the block so questions can omit it.",
)
@click.option(
    "--print",
    "to_stdout",
    is_flag=True,
    help="Print the managed AGENTS.md block instead of writing the file.",
)
@click.option(
    "--force", is_flag=True, help="Rewrite the block even if already up to date."
)
def init(
    target_dir: str, catalog_path: str | None, to_stdout: bool, force: bool
) -> None:
    """Write the analyst role into <path>/AGENTS.md (idempotent managed block).

    pi auto-discovers AGENTS.md, so after this ``pi "<question>"`` answers
    verified-by-construction with no flag. Re-run after upgrading the package to
    refresh the block; content outside the managed markers is preserved.
    """
    block = _render_block(catalog_path)
    if to_stdout:
        click.echo(block, nl=False)
        return
    agents = Path(target_dir) / "AGENTS.md"
    existing = agents.read_text(encoding="utf-8") if agents.exists() else ""
    updated = _merge_block(existing, block)
    if updated == existing and not force:
        click.echo(f"{agents}: analyst role already up to date", err=True)
        return
    agents.write_text(updated, encoding="utf-8")
    click.echo(
        f"{agents}: analyst role {'updated' if existing else 'created'} — "
        'pi now answers verified-by-construction; try `pi "which origin is busiest?"`',
        err=True,
    )
