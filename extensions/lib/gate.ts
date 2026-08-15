/**
 * Pure answer-gate logic for the pi-xorq-verifier extension.
 *
 * Everything here is dependency-free and side-effect-free so it can be imported
 * both by the extension (via pi's jiti loader) and by `node --test` directly
 * (node strips the erasable type syntax natively). This file lives under
 * extensions/lib/ — NOT extensions/ — because pi discovers every direct *.ts in
 * an extensions directory as an extension and errors on a file with no default
 * factory; a subdirectory without an index.ts is skipped.
 *
 * The gate's stance (see docs/adr/0001 §5 and the extension header): the model
 * cannot self-certify. Prose analysis here is a HEURISTIC demoted to a coverage
 * auditor — a match can only ever *refuse* the ✅ banner (fail closed), never
 * grant it; granting requires the deterministic checker's own certificates.
 */

export const GATE_MARK = "⟦xorq-checker⟧"; // banner sentinel (idempotency + identification)

// Parse a value to a number for matching (strip commas/percent). null unless the
// WHOLE string is numeric — parseFloat's prefix semantics would bless a label like
// "680 apples" as 680, letting a text grid cell back a claim.
export function gnum(v: any): number | null {
  if (v == null) return null;
  const s = String(v).replace(/,/g, "").replace(/%/g, "").trim();
  if (!/^-?\d+(?:\.\d+)?$/.test(s)) return null;
  const f = parseFloat(s);
  return Number.isFinite(f) ? f : null;
}

// The numbers the checker actually DISCHARGED in this certificate: a claim is
// "backed" only if it matches one of these (surface, selected cell, grid cells
// of a DISCHARGED obligation). A REFUTED / COULD-NOT-DISCHARGE obligation blesses
// nothing.
export function dischargedValues(cert: any): number[] {
  const out: number[] = [];
  for (const o of cert?.obligations ?? []) {
    if (o.status !== "DISCHARGED") continue;
    for (const v of [o.surface, o.selected_cell]) {
      const n = gnum(v);
      if (n !== null) out.push(n);
    }
    if (o.grid) for (const row of o.grid.rows ?? []) for (const k of Object.keys(row)) {
      const n = gnum(row[k]);
      if (n !== null) out.push(n);
    }
  }
  return out;
}

// Canonical key for supersession matching: numeric surfaces normalize through
// gnum (so a discharge of 32 clears a refuted "32"), text surfaces compare as
// trimmed strings, and a surfaceless obligation falls back to null.
function surfKey(v: any): string | null {
  if (v == null) return null;
  const n = gnum(v);
  if (n !== null) return String(n);
  const s = String(v).trim();
  return s ? s : null;
}

// The turn's standing (not yet superseded) refutations and coverage gaps.
export type CertFold = { refuted: string[]; uncovered: string[] };

export const emptyFold = (): CertFold => ({ refuted: [], uncovered: [] });

// Fold one certificate into the turn's standing state — value-level supersession.
// New refutations and uncovered reply values join the standing set; any surface
// THIS certificate DISCHARGED is cleared from both, past and present. So the
// analyst's sanctioned repair loop (obligation refuted for a mis-declared
// witness → fix the predicate → re-verify the SAME surface) converges to a clean
// state, while a refuted claim that is never re-discharged stands and refuses
// the banner. This does not weaken the gate against witness-shopping: a shopped
// witness that discharges was never distinguishable from a repaired one by
// certificate data alone (nor visible at all when shopped on the first try) —
// the standing-refutation rule keeps exactly the signal the checker provides.
export function foldCertificate(prev: CertFold, cert: any): CertFold {
  const discharged = new Set<string>();
  const refutedNow: string[] = [];
  for (const o of cert?.obligations ?? []) {
    const key = surfKey(o.surface) ?? surfKey(o.selected_cell) ?? `(${o.id ?? "unnamed"})`;
    if (o.status === "DISCHARGED") discharged.add(key);
    else if (o.status === "REFUTED") refutedNow.push(key);
  }
  const uncoveredNow = (cert?.coverage?.uncovered ?? []).map(
    (u: any) => surfKey(u) ?? String(u),
  );
  const stands = (k: string) => !discharged.has(k);
  return {
    refuted: [...prev.refuted, ...refutedNow].filter(stands),
    uncovered: [...prev.uncovered, ...uncoveredNow].filter(stands),
  };
}

// Whether this certificate DISCHARGED an extremal/ranking obligation — the only
// thing that can back a superlative in prose. Read off the checker's own check
// names, never the request: an argmax/argmin discharge always carries
// `maximality` (or `maximality_within_scope` for a restricted population), and a
// verified ranking table carries `ordered`. A REFUTED/COULD-NOT extremum backs
// nothing.
export function dischargedExtremal(cert: any): boolean {
  for (const o of cert?.obligations ?? []) {
    if (o.status !== "DISCHARGED") continue;
    const checks = o.checks ?? {};
    if (
      checks["maximality"] === true ||
      checks["maximality_within_scope"] === true ||
      checks["ordered"] === true
    )
      return true;
  }
  return false;
}

// Drop code (fenced + inline) before any prose analysis — `source.order_by(...)`
// or a check name like `maximality` inside backticks is not a data claim.
function stripCode(text: string): string {
  return text.replace(/```[\s\S]*?```/g, " ").replace(/`[^`]*`/g, " ");
}

// A numeric claim stated in the answer, with the decimal places it was written to
// (so a displayed rounding can be matched against a full-precision discharged value).
export type Claim = { n: number; dec: number; pct: boolean };

// Numeric claims stated in the answer prose. Conservative: drops witness refs /
// catalog hashes, 4-digit years, per-capita denominators ("per 100,000"), and
// small bare ordinals ("top 5", "3 rows") — a smuggled figure that is a decimal /
// percentage / non-trivial count is what we catch.
export function answerClaims(text: string): Claim[] {
  const cleaned = stripCode(text)
    .replace(/sha256:[0-9a-f]+/gi, " ")
    .replace(/\bverify-[\w-]+/gi, " ")
    // Hash-strip only true hex (must contain a-f) so a plain 8+ digit number
    // (a population like 39538223) stays a claim instead of vanishing.
    .replace(/\b(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b/gi, " ");
  const out: Claim[] = [];
  // No leading sign and a non-digit/non-dot left boundary: a hyphen in "2019-2023"
  // or "COVID-19" must not spawn a phantom negative claim (-2023) that can never be
  // backed and stamps a correct answer NOT VERIFIED.
  const re = /(?<![\d.])\d[\d,]*(?:\.\d+)?%?/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(cleaned)) !== null) {
    const raw = m[0];
    const n = gnum(raw);
    if (n === null) continue;
    const bare = raw.replace(/,/g, "").replace(/%/g, "");
    if (/^\d{4}$/.test(bare) && n >= 1900 && n <= 2099) continue; // year
    if (/per\s*$/i.test(cleaned.slice(Math.max(0, m.index - 5), m.index))) continue; // "per 100,000"
    // Scope idiom, not a data claim: "the 50 states", "50 U.S. states + DC".
    if (/^\s+(?:U\.?S\.?\s+)?states\b/i.test(cleaned.slice(m.index + raw.length))) continue;
    const dot = bare.indexOf(".");
    const dec = dot < 0 ? 0 : bare.length - dot - 1;
    const hasPct = raw.includes("%");
    if (dec === 0 && !hasPct && Math.abs(n) < 10) continue; // small ordinal ("top 5")
    out.push({ n, dec, pct: hasPct });
  }
  return out;
}

// Superlative / ranking wording stated in the answer prose — the claims a numeric
// net can never catch ("the highest concentration among all U.S. states" carries
// no figure). Matches only data-flavored superlatives and rank assertions, not
// every "-est" word, to keep false positives on conversational answers low. The
// matched snippets are surfaced verbatim in the banner so a refusal names exactly
// which wording needs an argmax/argmin/table obligation (or a rephrase).
const SUPERLATIVE_RES: RegExp[] = [
  // superlative adjectives over data
  /\b(?:highest|lowest|largest|smallest|biggest|greatest|busiest|fewest|densest|fastest[- ]growing|top[- ]ranked|record[- ](?:high|low))\b/gi,
  // superlative determiners: "the most cost-efficient", "the fewest stores"
  /\bthe\s+(?:most|least|fewest)\s+[\w-]+/gi,
  // ordinal ranks: "second-largest", "third-highest"
  /\b(?:second|third|fourth|fifth)[- ](?:largest|highest|lowest|biggest|most)\b/gi,
  // rank/uniqueness assertions
  /\b(?:no\s+other\s+\w+|than\s+any\s+other|than\s+every\s+other|number\s+one|#1|ranks?\s+(?:first|last|highest|lowest)|lead(?:s|er|ing)?\s+(?:all|the\s+nation)|nation[- ]leading)\b/gi,
];

export function answerSuperlatives(text: string): string[] {
  const cleaned = stripCode(text);
  const out: string[] = [];
  for (const re of SUPERLATIVE_RES) {
    re.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = re.exec(cleaned)) !== null) out.push(m[0].trim());
  }
  return [...new Set(out.map((s) => s.toLowerCase()))];
}

// A claim is backed if some discharged value rounds to it at the precision it was
// written: |claim − value| ≤ half a unit in the claim's last place. So a displayed
// 17.084 is backed by the full-precision 17.08394… (a correct rounding), while 680
// is NOT backed by 679 — precise, unlike a flat tolerance.
export const gbacked = (c: Claim, blessed: number[]): boolean => {
  const tol = 0.5 * Math.pow(10, -c.dec) + 1e-9;
  // A "%"-written claim (17.08%) may be discharged as either the percent number
  // (17.08) or the underlying fraction (0.1708); accept the correct restatement in
  // either unit. The fraction is two decimals more precise, so tighten its tolerance.
  const fracTol = 0.5 * Math.pow(10, -(c.dec + 2)) + 1e-9;
  return blessed.some(
    (b) => Math.abs(c.n - b) <= tol || (c.pct && Math.abs(c.n / 100 - b) <= fracTol),
  );
};

// Scope STANDING refutations to the answer that states them: a numeric surface
// the final answer never claims (a typo'd obligation the agent abandoned, e.g.
// 8395 refuted then the correct 7942 discharged) must not condemn an otherwise
// fully-backed answer — the sanctioned repair is to discharge the right figure
// and not print the wrong one. TEXT surfaces stay unconditional: a refuted
// entity/category often appears in prose in wording the numeric net cannot
// match, which is exactly why refutations are status-based at all.
export function statedRefuted(refuted: string[], claims: Claim[], answer: string): string[] {
  return refuted.filter((key) => {
    const n = Number(key);
    if (!Number.isFinite(n)) return true; // text surface: stands regardless
    return claims.some((c) => gbacked(c, [n]));
  });
}

// Same scoping for standing coverage gaps (reply values no certificate covered):
// numeric ones matter only if the answer states them; text ones only if the
// answer contains them — an undischarged value the answer never printed is not
// a claim to refuse.
export function statedUncovered(uncovered: string[], claims: Claim[], answer: string): string[] {
  const lower = answer.toLowerCase();
  return uncovered.filter((key) => {
    const n = Number(key);
    if (Number.isFinite(n)) return claims.some((c) => gbacked(c, [n]));
    return lower.includes(key.toLowerCase());
  });
}

// A one-line reason for a lineage that failed its source check (from
// xorq_check_lineage): the alias and why. `lineageFails` are the tracked
// DISCREPANCY verdicts — value-faithfulness alone cannot clear an answer whose
// SOURCE the deterministic lineage check rejected (the demonstrated hole: a ✅
// value certificate over a hardcoded-population metric the lineage check refused).
export function lineageReason(v: any): string {
  const why =
    (v.issues && v.issues.length ? v.issues.join("; ") : v.detail) || "illegitimate source";
  return `source lineage FAILED for ${v.alias ?? "?"}: ${why}`;
}

export type GateInputs = {
  hasCert: boolean; // was xorq_verify ever run this turn
  catalogState: string;
  unbacked: number[]; // answer figures matched by NO discharged value
  lineageFails: any[]; // tracked lineage DISCREPANCY verdicts
  refuted: string[]; // STANDING refuted surfaces (refuted, never re-discharged)
  uncoveredCert: string[]; // standing reply values no certificate covered
  lineageChecked: boolean; // did xorq_check_lineage run at all
  superlatives: string[]; // superlative/ranking wording found in the prose
  hasExtremal: boolean; // did any certificate DISCHARGE an argmax/argmin/ranking
};

// Author the banner from SESSION-accumulated state. Accumulating across certs
// is essential: an agent verifies piecemeal (share, count, population, per-100k in
// separate calls), so a last-cert-only gate falsely flags earlier-verified figures.
// Refutations and coverage gaps accumulate with value-level supersession
// (foldCertificate): only those still standing at answer time refuse.
export function gateBanner(g: GateInputs): string | null {
  const reasons: string[] = [];
  if (!g.hasCert)
    reasons.push("no checker certificate — xorq_verify was not run for this answer");
  // A REFUTED obligation is the checker contradicting a claim; value-backing
  // alone must not clear it — only a later certificate DISCHARGING the same
  // surface does (the sanctioned fix-the-witness-and-re-verify loop). A
  // refutation often carries no number the prose net catches (a wrong
  // entity/category), so this is checked on obligation status, not just on
  // unbacked figures.
  if (g.refuted.length)
    reasons.push(
      `claims the checker REFUTED and never re-discharged: ${[...new Set(g.refuted)].join(", ")}`,
    );
  if (g.unbacked.length)
    reasons.push(`figures with no discharged witness: ${[...new Set(g.unbacked)].join(", ")}`);
  // The certificate's own coverage audit: reply values it could not cover (incl.
  // non-numeric ones the prose regex can never catch).
  if (g.uncoveredCert.length)
    reasons.push(
      `values the certificate left uncovered: ${[...new Set(g.uncoveredCert)].join(", ")}`,
    );
  // A superlative/ranking claim is checkable only by an extremal obligation —
  // "highest among all states" with two scalar certificates is the smuggle the
  // numeric net cannot see. Existence-level on purpose: whether the discharged
  // extremum's population matches the prose's is the correctness boundary the
  // checker does not decide (ADR-0001 §5) — but with zero extremal certificates,
  // a superlative is certainly unverified.
  if (g.superlatives.length && !g.hasExtremal)
    reasons.push(
      "superlative/ranking wording with no discharged argmax/argmin/table obligation: " +
        `${g.superlatives.map((s) => `"${s}"`).join(", ")} — verify it (kind: argmax/argmin/table) or rephrase without the claim`,
    );
  for (const v of g.lineageFails) reasons.push(lineageReason(v));

  if (g.hasCert && reasons.length === 0) {
    const cat = g.catalogState
      ? ` (catalog ${g.catalogState.replace("sha256:", "").slice(0, 6)}…)`
      : "";
    // Only claim the lineage was checked if xorq_check_lineage actually ran.
    const lin = g.lineageChecked ? " and its source lineage checked" : "";
    return `${GATE_MARK} ✅ VERIFIED — every figure discharged${lin}${cat}.`;
  }
  return [
    `${GATE_MARK} ⚠ NOT VERIFIED`,
    "The deterministic checker did not certify this answer:",
    ...reasons.map((r) => "  · " + r),
    "Treat every number below as UNVERIFIED. This banner is the checker's — the agent's own wording does not decide the verdict.",
  ].join("\n");
}
