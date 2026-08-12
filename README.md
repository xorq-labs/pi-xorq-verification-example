# pi-xorq verification duel

A side-by-side demo of **verified-by-construction data answers**: the same
hallucination-bait question is fed to two coding agents at once, and you watch
one of them answer from memory-ish vibes while the other is forced to *prove*
every number it prints.

```
┌───────────────┬──────────────────────────┬──────────────────────────┐
│ claude (bare) │ pi + xorq verification   │ xorq catalog TUI         │
│ fresh tmp dir │ this repo                │ this repo                │
│ no tools but  │ every number selected    │ watch expressions and    │
│ the basics    │ from a catalog expr and  │ verify-<id> witnesses    │
│               │ re-checked by a          │ appear as the agent      │
│               │ deterministic checker    │ works                    │
└───────────────┴──────────────────────────┴──────────────────────────┘
```

The right pane is the point: verification here is not "the model says it
double-checked." A deterministic Python checker (`pi-xorq-check`) re-runs every
claimed value as a fresh *witness expression* against a re-runnable
[xorq](https://github.com/xorq-labs/xorq) catalog, compares under typed
equality, and folds a verdict (`VERIFIED | DISCREPANCY | COULD-NOT-VERIFY |
NO-OP`). The agent cannot stamp its own answer — the extension's answer gate
re-stamps every terminal answer from the checker's certificate, and even
superlative *wording* ("highest", "second-largest") must be backed by a
discharged `argmax`/`argmin` obligation or the answer is marked **NOT
VERIFIED**. The conceptual model is in
[docs/adr/0001](docs/adr/0001-formal-verification-as-obligation-discharge.md).

## Run it

Requirements: [Nix](https://nixos.org) with flakes, plus the `claude` CLI on
your PATH for the left pane (the duel still works without it — the pane just
errors — but then it's less of a duel). `pi` needs an Anthropic (or other
provider) API key in your environment.

```bash
nix develop        # python env (xorq + the checker) + pi + tmux, all pinned
./duel.sh          # default trap: tlc-tip-card (NYC taxi data, 3.7M rows)
```

That's it. The script opens the three tmux panes, initializes a fresh catalog,
and types the same prompt into both agents.

Pick a different trap by id:

```bash
python bench/hallucination_prompts.py   # validates every oracle, lists the ids
./duel.sh join-hazard-dc                # e.g. the case-sensitive-join trap
```

## What the prompts are

Every prompt in `bench/hallucination_prompts.py` is a **trap with an executable
oracle**: it pins its terms to real public data files (farmers-markets CSV +
census estimates, NOAA GHCN-Daily, NYC TLC trip records), so there is exactly
one defensible answer and it is recomputable. A wrong answer is *provably*
hallucinated, not merely disputed. The default trap exploits a documented
booby trap in the TLC data: cash tips are not recorded, so "average tip" has
one defensible reading and several tempting wrong ones — including one that
requires choosing per-trip mean over ratio-of-sums across 3.7M rows.

## What to watch for

- **Left (bare claude):** it will fetch the data and compute *something* —
  often a plausible, confidently worded, wrong number (the per-family bait is
  listed in the bench file). Nothing checks it.
- **Middle (pi + verifier):** the analyst role (in [AGENTS.md](AGENTS.md))
  forbids stating any number not obtained via `xorq_select` on a declared
  catalog alias. It ingests the sources into the catalog, composes the metric
  as a catalog expression, declares one proof obligation per claim, and calls
  `xorq_verify`. The checker synthesizes the witnesses, re-runs them, renders
  the certificate card, and the gate stamps the banner.
- **Right (catalog TUI):** the ingested aliases and persisted `verify-<id>`
  witnesses show up live. After the duel you can re-run any witness yourself:
  `xorq catalog run <alias>` — the certificate is re-checkable after the
  agents are gone.

## Using the checker without any LLM

The deterministic checker is the trust root and has no dependency on pi. Gate
any answer-shaped JSON against a catalog:

```bash
# build the tiny offline sample catalog
xorq build sample/flights_pipeline.py --builds-dir .xorq/builds \
  --emit-build-path-to .xorq/last_build.txt
xorq catalog -p .xorq/catalog init
xorq catalog -p .xorq/catalog add "$(cat .xorq/last_build.txt)" \
  -a flights-by-origin --no-sync

pi-xorq-check verify sample/answer_request.json   # print the full certificate
pi-xorq-check gate   sample/answer_request.json   # exit 0 only if VERIFIED/NO-OP
```

`schemas/request.schema.json` and `schemas/certificate.schema.json` are the
contracts.

## What's in here

```
duel.sh                        the demo: three tmux panes, one prompt
bench/hallucination_prompts.py trap prompts + executable oracles (self-checking)
src/pi_xorq_verifier/          the deterministic checker (trust root, plain Python)
  └ prompts/analyst.md         the single role prompt
extensions/xorq.ts             pi extension: xorq_select / xorq_verify / catalog tools
extensions/lib/gate.ts         the answer gate (re-stamps every terminal answer)
skills/xorq-catalog/           catalog orientation skill for pi
schemas/                       request / certificate contracts
AGENTS.md                      the analyst role (pi auto-loads it)
sample/                        offline sample catalog + a worked request
docs/adr/0001                  the verification model, in full
flake.nix                      pinned env: python (xorq + checker), pi, tmux
```

This repo is a self-contained snapshot of
[pi-xorq-verifier](https://github.com/xorq-labs/pi-xorq-verifier) packaged as a
runnable demo; see that repo for the maintained checker and extension.
