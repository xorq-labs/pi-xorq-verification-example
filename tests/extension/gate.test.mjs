// Unit tests for the answer gate's pure logic (extensions/lib/gate.ts).
// Run: node --test tests/extension/
// Plain node (>=23.6 strips erasable TS natively); no pi runtime, no deps.

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  answerClaims,
  answerSuperlatives,
  dischargedExtremal,
  emptyFold,
  foldCertificate,
  gateBanner,
  gbacked,
} from "../../extensions/lib/gate.ts";

// The exact terminal answer of the 2026-07-05 dogfood session (turn 1), where a
// superlative with no figure attached shipped under a ✅ banner: every number was
// discharged, but "the highest concentration among all U.S. states" was false
// (Rhode Island leads at 7.6× California's rate) and no argmax obligation existed.
const SMUGGLED_CALLOUT =
  "California's farmers markets are **17.0839% organic-vendor**, delivering " +
  "**0.2948 organic-vendor markets per 100,000 residents**—the highest " +
  "concentration among all U.S. states.";

// The values the checker actually discharged that turn (full precision).
const TURN1_BLESSED = [17.0839, 17.083946980854197, 0.2948, 0.2947505760912714];

const gate = (over) =>
  gateBanner({
    hasCert: true,
    catalogState: "sha256:b74141aa00000000",
    unbacked: [],
    lineageFails: [],
    refuted: [],
    uncoveredCert: [],
    lineageChecked: true,
    superlatives: [],
    hasExtremal: false,
    ...over,
  });

// ---------------------------------------------------------------- detection //

test("superlative in the smuggled callout is detected", () => {
  const found = answerSuperlatives(SMUGGLED_CALLOUT);
  assert.ok(found.includes("highest"), `expected "highest" in ${JSON.stringify(found)}`);
});

test("data superlatives and rank assertions are detected", () => {
  for (const text of [
    "Rhode Island has the largest per-capita density.",
    "ATL is the busiest origin.",
    "California leads the nation in organic access.",
    "No other state comes close.",
    "It outnumbers than any other state.", // clumsy phrasing still carries the claim
    "Texas is the second-largest market.",
    "Vermont ranks first per capita.",
    "the most organic-accessible state",
  ]) {
    assert.ok(answerSuperlatives(text).length > 0, `expected a match in: ${text}`);
  }
});

test("plain prose and code-only superlatives do not trigger", () => {
  assert.deepEqual(answerSuperlatives("California has 679 farmers markets."), []);
  assert.deepEqual(
    answerSuperlatives("Run `source.order_by(source.n.desc()).limit(1)` — the highest row.")
      .includes("highest"),
    true,
    "prose outside backticks still counts",
  );
  assert.deepEqual(
    answerSuperlatives("```\nthe highest value\n```\nDone."), [],
    "fenced code is stripped",
  );
  assert.deepEqual(answerSuperlatives("I checked `maximality` and `ordered`."), []);
});

// ---------------------------------------------------------- extremal backing //

const cert = (obligations) => ({ verdict: "VERIFIED", obligations });

test("argmax/ranking discharges back a superlative; scalars never do", () => {
  assert.equal(
    dischargedExtremal(cert([{ status: "DISCHARGED", checks: { maximality: true } }])),
    true,
  );
  assert.equal(
    dischargedExtremal(
      cert([{ status: "DISCHARGED", checks: { maximality_within_scope: true } }]),
    ),
    true,
  );
  assert.equal(
    dischargedExtremal(
      cert([{ status: "DISCHARGED", checks: { row_count: true, ordered: true, cells_match: true } }]),
    ),
    true,
    "a verified ranking table is extremal backing",
  );
  assert.equal(
    dischargedExtremal(cert([{ status: "DISCHARGED", checks: { typed_eq: true } }])),
    false,
    "a scalar discharge cannot back a superlative",
  );
  assert.equal(
    dischargedExtremal(cert([{ status: "REFUTED", checks: { maximality: false } }])),
    false,
    "a refuted extremum backs nothing",
  );
});

// ------------------------------------------------------------------- banner //

test("regression: the smuggled callout is refused without an extremal certificate", () => {
  const claims = answerClaims(SMUGGLED_CALLOUT);
  const unbacked = claims.filter((c) => !gbacked(c, TURN1_BLESSED)).map((c) => c.n);
  assert.deepEqual(unbacked, [], "every figure was genuinely discharged that turn");
  const banner = gate({
    unbacked,
    superlatives: answerSuperlatives(SMUGGLED_CALLOUT),
  });
  assert.ok(banner.includes("NOT VERIFIED"), banner);
  assert.ok(banner.includes("superlative"), banner);
  assert.ok(banner.includes('"highest"'), "the refusal names the wording");
});

test("the same callout passes once an extremal obligation discharged", () => {
  const banner = gate({
    superlatives: answerSuperlatives(SMUGGLED_CALLOUT),
    hasExtremal: true,
  });
  assert.ok(banner.startsWith("⟦xorq-checker⟧ ✅ VERIFIED"), banner);
});

test("a prose-only superlative with no numbers is still gated", () => {
  const text = "California leads the nation in organic market access.";
  assert.equal(answerClaims(text).length, 0, "no numeric claims to catch");
  const banner = gate({ hasCert: false, lineageChecked: false,
    superlatives: answerSuperlatives(text) });
  assert.ok(banner.includes("NOT VERIFIED"), banner);
});

test("all-backed figures with no superlative still pass (no regression)", () => {
  const banner = gate({});
  assert.ok(banner.startsWith("⟦xorq-checker⟧ ✅ VERIFIED"), banner);
});

// -------------------------------------------------- refutation supersession //

// The 2026-08-10 cascade run: verify #1 REFUTED georgia_additional_markets
// (surface 32, witness selected the wrong cell) and could not discharge the
// rate; verify #2 discharged a different Georgia framing, leaving reply value
// "32" uncovered; verify #3 discharged everything, 32 included. The old
// verdict-boolean gate refused that turn forever — the analyst prompt's own
// "fix the obligation and re-run until VERIFIED" loop could never produce ✅.
const CASCADE_CERTS = [
  {
    verdict: "DISCREPANCY",
    obligations: [
      { id: "total_markets", status: "DISCHARGED", surface: "7946" },
      { id: "national_rate", status: "COULD-NOT-DISCHARGE", surface: "2.3249" },
      { id: "states_above", status: "DISCHARGED", surface: "29" },
      { id: "georgia_additional", status: "REFUTED", surface: "32", selected_cell: "231" },
    ],
  },
  {
    verdict: "COULD-NOT-VERIFY",
    obligations: [
      { id: "total_markets", status: "DISCHARGED", surface: "7946" },
      { id: "national_rate", status: "DISCHARGED", surface: "2.3249" },
      { id: "states_above", status: "DISCHARGED", surface: "29" },
      { id: "georgia_needed", status: "DISCHARGED", surface: "262.7724" },
    ],
    coverage: { uncovered: ["32"] },
  },
  {
    verdict: "VERIFIED",
    obligations: [
      { id: "total_markets", status: "DISCHARGED", surface: "7946" },
      { id: "national_rate", status: "DISCHARGED", surface: "2.3249" },
      { id: "states_above", status: "DISCHARGED", surface: "29" },
      { id: "georgia_additional", status: "DISCHARGED", surface: "32" },
    ],
  },
];

test("a refutation re-discharged by a later certificate is superseded", () => {
  const fold = CASCADE_CERTS.reduce(foldCertificate, emptyFold());
  assert.deepEqual(fold, { refuted: [], uncovered: [] });
  const banner = gate({ refuted: fold.refuted, uncoveredCert: fold.uncovered });
  assert.ok(banner.startsWith("⟦xorq-checker⟧ ✅ VERIFIED"), banner);
});

test("a refutation never re-discharged stands and refuses, naming the surface", () => {
  const fold = CASCADE_CERTS.slice(0, 2).reduce(foldCertificate, emptyFold());
  assert.deepEqual(fold.refuted, ["32"]);
  assert.deepEqual(fold.uncovered, ["32"]);
  const banner = gate({ refuted: fold.refuted, uncoveredCert: fold.uncovered });
  assert.ok(banner.includes("NOT VERIFIED"), banner);
  assert.ok(banner.includes("REFUTED and never re-discharged: 32"), banner);
});

test("supersession matches numerically, and a same-cert discharge clears its own refutation", () => {
  // "32" (string) vs 32 (number) must fold to the same key; a surfaceless
  // refutation keys on its id and stands until that id's claim is discharged.
  const fold = foldCertificate(
    { refuted: ["32", "(entity_claim)"], uncovered: [] },
    { obligations: [{ id: "x", status: "DISCHARGED", surface: 32 }] },
  );
  assert.deepEqual(fold.refuted, ["(entity_claim)"]);
});

// ------------------------------------------- precision backing (pre-existing) //

test("precision-aware backing: correct rounding passes, off-by-one does not", () => {
  const [c] = answerClaims("the share is 17.0839%");
  assert.equal(gbacked(c, [17.083946980854197]), true);
  const [d] = answerClaims("about 680 markets");
  assert.equal(gbacked(d, [679]), false, "680 is not a rounding of 679 at 0 decimals");
});

test("claim extraction skips years, per-capita denominators, small ordinals", () => {
  const ns = answerClaims(
    "In 2025 the top 5 states had 0.2948 markets per 100,000 residents.",
  ).map((c) => c.n);
  assert.deepEqual(ns, [0.2948]);
});
