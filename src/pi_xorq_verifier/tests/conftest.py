"""Fixtures for integration tests that need a live xorq catalog.

``sample_catalog`` yields a catalog path with the ``flights-by-origin`` alias:
it reuses the repo's ``.xorq/catalog`` when present, otherwise builds a fresh one
from ``sample/flights_pipeline.py``. Tests skip cleanly when xorq is absent.
"""

import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]
PIPELINE = REPO / "sample" / "flights_pipeline.py"
ALIAS = "flights-by-origin"


def _has_alias(catalog: Path) -> bool:
    if shutil.which("xorq") is None or not catalog.exists():
        return False
    proc = subprocess.run(
        ("xorq", "catalog", "-p", str(catalog), "list-aliases"),
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0 and ALIAS in proc.stdout


@pytest.fixture(scope="session")
def sample_catalog(tmp_path_factory) -> str:
    if shutil.which("xorq") is None:
        pytest.skip("xorq not on PATH")

    repo_catalog = REPO / ".xorq" / "catalog"
    if _has_alias(repo_catalog):
        return str(repo_catalog)

    if not PIPELINE.exists():
        pytest.skip("sample/flights_pipeline.py missing")

    work = tmp_path_factory.mktemp("xorq-catalog")
    builds, catalog, build_path = work / "builds", work / "catalog", work / "bp.txt"

    build = subprocess.run(
        ("xorq", "build", str(PIPELINE), "--builds-dir", str(builds),
         "--emit-build-path-to", str(build_path)),
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        pytest.skip(f"xorq build failed: {build.stderr[-400:]}")

    subprocess.run(
        ("xorq", "catalog", "-p", str(catalog), "init"),
        capture_output=True,
        text=True,
    )
    add = subprocess.run(
        ("xorq", "catalog", "-p", str(catalog), "add", build_path.read_text().strip(),
         "-a", ALIAS, "--no-sync"),
        capture_output=True,
        text=True,
    )
    if not _has_alias(catalog):
        pytest.skip(f"xorq catalog add failed: {add.stderr[-400:]}")
    return str(catalog)
