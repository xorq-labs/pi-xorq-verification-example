#!/usr/bin/env bash
#
# seed_semantic_catalog.sh — build the reviewed BSL semantic model
# (bench/bsl_us_markets.py) and add it to a catalog under one alias:
#
#   us_markets  the per-state semantic cube (dimensions + measures); consumers
#               query its measures by name — see skills/semantic-model/SKILL.md
#
# Usage: bench/seed_semantic_catalog.sh [catalog_path]    default: .xorq/catalog
#
# Run from the repo root — xorq build and catalog add must share a cwd — with
# a git HEAD present (builds record git state). duel.sh chains this into its
# catalog setup for the denominator-us-semantic trap.

set -euo pipefail

CATALOG="${1:-.xorq/catalog}"
BUILDS=".xorq/builds"
mkdir -p "$BUILDS"

xorq catalog -p "$CATALOG" info >/dev/null 2>&1 || xorq catalog -p "$CATALOG" init

seed() { # seed <expr-var> <alias>
  local bp
  bp="$(mktemp)"
  xorq build bench/bsl_us_markets.py -e "$1" --builds-dir "$BUILDS" --emit-build-path-to "$bp"
  xorq catalog -p "$CATALOG" add "$(cat "$bp")" -a "$2" --no-sync
  rm -f "$bp"
}

seed model_expr us_markets

xorq catalog -p "$CATALOG" list-aliases
