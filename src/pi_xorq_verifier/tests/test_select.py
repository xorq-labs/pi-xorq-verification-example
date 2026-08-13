"""``pi-xorq-check select`` — the snapshot-cached peek path (compute, not verify).

The contract under test: the snapshot cache wraps the *alias* (the source), so
every peek on an alias shares ONE fetch of its sources, while ``--no-cache``
(the verify-shaped path) always goes back to the source.

The alias reads from a local HTTP server that counts GETs — a *local file*
source would prove nothing here, because ``xorq build`` archives local reads
into the entry (``reads/*.csv``), which then answers even with the file gone.
Remote reads are exactly what the cache exists for, and the hit counter turns
"did it re-fetch?" into an exact assertion. Each select runs as a SUBPROCESS,
exactly like the extension invokes it: an in-process runner would keep the
backend (and its registered tables) alive across calls, silently standing in
for the on-disk snapshot under test.
"""

import http.server
import shutil
import subprocess
import sys
import threading

import pytest


pytestmark = pytest.mark.integration

_CSV = b"state,markets\nCA,648\nNY,555\nPA,426\n"
_BUILD = """\
import xorq.api as xo

con = xo.pandas.connect()
expr = xo.deferred_read_csv(
    "http://127.0.0.1:{port}/markets.csv",
    con,
    table_name="markets",
)
"""


class _CountingHandler(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):
        type(self).hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Length", str(len(_CSV)))
        self.end_headers()
        self.wfile.write(_CSV)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def peek_catalog(tmp_path_factory):
    """A catalog whose ``markets`` alias reads a GET-counting local HTTP source.

    Yields ``(catalog_path, hits, server)`` — ``hits`` grows by one per source
    fetch, and shutting ``server`` down makes the source unreachable.
    """
    if shutil.which("xorq") is None:
        pytest.skip("xorq not on PATH")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CountingHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        work = tmp_path_factory.mktemp("select-catalog")
        (work / "build_markets.py").write_text(
            _BUILD.format(port=server.server_address[1])
        )
        build_path = work / "bp.txt"
        build = subprocess.run(
            ("xorq", "build", str(work / "build_markets.py"),
             "--builds-dir", str(work / "builds"),
             "--emit-build-path-to", str(build_path)),
            capture_output=True,
            text=True,
        )
        if build.returncode != 0:
            pytest.skip(f"xorq build failed: {build.stderr[-400:]}")
        catalog = work / "catalog"
        subprocess.run(
            ("xorq", "catalog", "-p", str(catalog), "init"),
            capture_output=True, text=True,
        )
        add = subprocess.run(
            ("xorq", "catalog", "-p", str(catalog), "add",
             build_path.read_text().strip(), "-a", "markets", "--no-sync"),
            capture_output=True, text=True,
        )
        if add.returncode != 0:
            pytest.skip(f"xorq catalog add failed: {add.stderr[-400:]}")
        yield str(catalog), _CountingHandler.hits, server
    finally:
        server.shutdown()
        server.server_close()


def _select(catalog: str, compose: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        (sys.executable, "-m", "pi_xorq_verifier", "select",
         "--catalog-path", catalog, "--on", "markets", "-c", compose, *extra),
        capture_output=True,
        text=True,
    )


def test_limit_appended_only_when_compose_has_none(peek_catalog):
    catalog, _, _ = peek_catalog

    capped = _select(catalog, 'source.select("state")', "--limit", "2")
    assert capped.returncode == 0, capped.stderr
    assert capped.stdout == '"state"\n"CA"\n"NY"\n'

    explicit = _select(catalog, 'source.select("state").limit(1)', "--limit", "2")
    assert explicit.returncode == 0, explicit.stderr
    assert explicit.stdout == '"state"\n"CA"\n'


def test_peeks_share_one_source_fetch(peek_catalog):
    catalog, hits, server = peek_catalog

    first = _select(catalog, "source.order_by(source.markets.desc()).limit(1)")
    assert first.returncode == 0, first.stderr
    assert '"CA",648' in first.stdout
    warm = len(hits)  # the snapshot exists after the first peek

    # A compose the snapshot has never seen answers with ZERO new fetches.
    second = _select(catalog, "source.aggregate(total=source.markets.sum())")
    assert second.returncode == 0, second.stderr
    assert second.stdout == '"total"\n1629\n'
    assert len(hits) == warm

    # --no-cache is the verify-shaped path: it MUST re-fetch from the source.
    live = _select(
        catalog, "source.aggregate(total=source.markets.sum())", "--no-cache"
    )
    assert live.returncode == 0, live.stderr
    assert live.stdout == '"total"\n1629\n'
    assert len(hits) == warm + 1

    # With the source UNREACHABLE the snapshot still answers fresh composes…
    server.shutdown()
    server.server_close()
    third = _select(catalog, "source.aggregate(n=source.count())")
    assert third.returncode == 0, third.stderr
    assert third.stdout == '"n"\n3\n'

    # …but the verify-shaped path fails loudly: cached peeks never leak into it.
    dead = _select(catalog, "source.aggregate(n=source.count())", "--no-cache")
    assert dead.returncode == 1, dead.stdout


def test_unknown_alias_is_exit_1(peek_catalog):
    catalog, _, _ = peek_catalog
    result = subprocess.run(
        (sys.executable, "-m", "pi_xorq_verifier", "select",
         "--catalog-path", catalog, "--on", "nope", "-c", "source.limit(1)"),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "did not load" in result.stderr
