/**
 * pi-xorq-verifier extension
 *
 * Tools a plain pi session uses to answer/verify data questions against a
 * re-runnable xorq catalog (see docs/adr/0001). Two surfaces:
 *
 *   - catalog inspection  → shell to the `xorq` CLI (must be on PATH)
 *   - obligation discharge → shell to the deterministic Python checker,
 *                            `pi-xorq-check` (or whatever PI_XORQ_CHECK names)
 *
 * The model proposes obligations (surface + predicate); the deterministic
 * checker synthesizes/re-runs the witness and folds the verdict — it is the
 * trust root, not the model.
 *
 * pi's runtime injects ExtensionAPI and typebox at load time; imports resolve
 * then, not at standalone tsc time.
 */
// @ts-nocheck
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { Type } from "typebox";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  GATE_MARK,
  answerClaims,
  answerSuperlatives,
  dischargedExtremal,
  dischargedValues,
  emptyFold,
  foldCertificate,
  gateBanner,
  gbacked,
  type CertFold,
} from "./lib/gate.ts";

const SHORT = 30_000;
const LONG = 300_000;

// How to invoke the deterministic checker. Defaults to the `pi-xorq-check`
// console script on PATH; override with PI_XORQ_CHECK to run it straight from a
// source checkout — no wheel/pip install required, e.g.
//   PI_XORQ_CHECK="uv run --project /path/to/pi-xorq-verifier pi-xorq-check"
//   PI_XORQ_CHECK="python -m pi_xorq_verifier"
function checkCmd(sub: string, ...args: string[]): [string, string[]] {
  const override = (process.env.PI_XORQ_CHECK ?? "").trim();
  const parts = override ? override.split(/\s+/) : ["pi-xorq-check"];
  return [parts[0], [...parts.slice(1), sub, ...args]];
}

// ======================================================================= //
// Certificate rendering.                                                   //
//                                                                          //
// We do NOT spit the raw certificate JSON out. The tool RESULT is a         //
// readable plain-text certificate card (verdict, per-obligation checks, a   //
// table's grid, witness refs) — captured by the transcript AND by external  //
// viewers like AgentsView, and read by the model. In the pi TUI the same    //
// certificate is drawn as a styled card via ToolDefinition.renderResult.    //
// (We avoid sendMessage: a custom transcript message renders in the pi TUI  //
// but is dropped by AgentsView, and it also breaks tool-result capture.)    //
// PI_XORQ_CERT_RENDER=off restores the raw-JSON result (debugging).         //
// ======================================================================= //

const CERT_RENDER = (process.env.PI_XORQ_CERT_RENDER ?? "on").toLowerCase() !== "off";

// ======================================================================= //
// Answer gate (message_end enforcement).                                   //
//                                                                          //
// The verdict must NOT be self-attested by the model. On every terminal    //
// assistant answer, the extension re-stamps the AUTHORITATIVE verdict from  //
// the checker's own certificates (tracked from xorq_verify) — a number the  //
// checker did not DISCHARGE cannot be presented as verified, no matter what //
// the model narrates. Behaviour (A): downgrade + annotate (non-destructive: //
// prepend the checker-authored banner, keep the answer body). Turn off with //
// PI_XORQ_GATE=off.                                                         //
// ======================================================================= //

const GATE = (process.env.PI_XORQ_GATE ?? "on").toLowerCase() !== "off";
// The gate's pure logic — claim extraction, precision-aware backing, superlative
// detection, extremal backing, banner authoring — lives in ./lib/gate.ts so it is
// unit-testable with plain `node --test` (this file needs pi's injected deps).

const ESC = "\x1b[";
const C = {
  reset: ESC + "0m", bold: ESC + "1m", dim: ESC + "2m",
  green: ESC + "32m", red: ESC + "31m", yellow: ESC + "33m",
  cyan: ESC + "36m", gray: ESC + "90m",
};
const paint = (s: string, code: string) => code + s + C.reset;
const stripAnsi = (s: string) => s.replace(/\x1b\[[0-9;]*m/g, "");

// Terminal COLUMN width of one code point. The badges we emit (✅ ❌ ⚠) and any
// emoji/CJK are DOUBLE-width; counting them as 1 (as `.length` does) makes a card
// line render one column past the limit and the pi TUI aborts. Conservative: unsure
// wide → 2 (truncates early, never overflows). VS16 (U+FE0F) is zero-width itself.
const WIDE_BADGES = new Set([0x2705, 0x274c, 0x26a0]); // ✅ ❌ ⚠
function charWidth(cp: number): number {
  if (cp === 0xfe0f || cp === 0x200d) return 0; // variation selector / ZWJ
  if (WIDE_BADGES.has(cp)) return 2;
  if (cp >= 0x1f000) return 2; // emoji & symbol planes
  if (
    (cp >= 0x1100 && cp <= 0x115f) || // Hangul Jamo
    (cp >= 0x2e80 && cp <= 0xa4cf) || // CJK & radicals
    (cp >= 0xac00 && cp <= 0xd7a3) || // Hangul syllables
    (cp >= 0xf900 && cp <= 0xfaff) || // CJK compat
    (cp >= 0xff00 && cp <= 0xff60) || // fullwidth forms
    (cp >= 0xffe0 && cp <= 0xffe6)
  )
    return 2;
  return 1;
}

// Visible COLUMN width (ANSI stripped, wide chars counted as 2).
function vwidth(s: string): number {
  let w = 0;
  for (const ch of stripAnsi(s)) w += charWidth(ch.codePointAt(0) as number);
  return w;
}

// Truncate to `width` visible columns, preserving ANSI codes, adding an ellipsis.
function clip(s: string, width: number): string {
  if (width <= 1) return vwidth(s) <= width ? s : "…"; // no room for content + ellipsis
  if (vwidth(s) <= width) return s;
  let out = "", vis = 0, i = 0;
  while (i < s.length) {
    if (s[i] === "\x1b") {
      const m = s.slice(i).match(/^\x1b\[[0-9;]*m/);
      if (m) { out += m[0]; i += m[0].length; continue; }
    }
    const cp = s.codePointAt(i) as number;
    const ch = String.fromCodePoint(cp);
    const cw = charWidth(cp);
    if (vis + cw > width - 1) break; // reserve 1 column for the ellipsis
    out += ch; vis += cw; i += ch.length;
  }
  return out + "…" + C.reset;
}

function parseCert(stdout: string): any | null {
  try { return JSON.parse(stdout); } catch { return null; }
}

// Render a certificate ({verdict, obligations, coverage, catalog_state}).
function certLines(cert: any, width: number): string[] {
  const w = Math.max(24, width || 80);
  const verdict = cert.verdict ?? "?";
  const obs = cert.obligations ?? [];
  const badge = ({ VERIFIED: "✅", DISCREPANCY: "❌", "COULD-NOT-VERIFY": "⚠", "NO-OP": "·" } as any)[verdict] ?? "•";
  const color = verdict === "VERIFIED" ? C.green : verdict === "DISCREPANCY" ? C.red : C.yellow;
  const cat = cert.catalog_state ? String(cert.catalog_state).replace("sha256:", "").slice(0, 6) : "";
  const lines: string[] = [];
  lines.push(clip(
    paint(` ${badge} ${verdict} `, C.bold + color) +
      paint(`  ${obs.length} obligation${obs.length === 1 ? "" : "s"}` + (cat ? `  ·  catalog ${cat}…` : ""), C.dim),
    w));
  const uncovered = cert.coverage?.uncovered ?? [];
  if (uncovered.length) lines.push(clip("   " + paint("uncovered: " + uncovered.join(", "), C.yellow), w));
  for (const o of obs) {
    const mark = o.status === "DISCHARGED" ? paint("✓", C.green)
      : o.status === "REFUTED" ? paint("✗", C.red) : paint("∅", C.yellow);
    if (o.grid) {  // a table obligation: the content is the grid, not a cell
      const n = (o.grid.rows ?? []).length;
      lines.push(clip(`   ${mark} ${paint(String(o.id), C.bold)}   ${paint(`table · ${n} row${n === 1 ? "" : "s"}`, C.dim)}`, w));
    } else {
      const cell = o.selected_cell != null ? String(o.selected_cell) : "—";
      const surf = o.surface != null && o.surface !== "" ? `${o.surface} ` : "";
      lines.push(clip(`   ${mark} ${paint(String(o.id), C.bold)}   ${surf}${paint("⟶", C.dim)} ${cell}`, w));
    }
    if (o.checks) {
      const parts = Object.entries(o.checks).map(
        ([k, val]) => (val ? paint("✓", C.green) : paint("✗", C.red)) + " " + paint(k, C.dim));
      lines.push(clip("      " + parts.join("  "), w));
    }
    if (o.grid) for (const gl of gridLines(o.grid, w)) lines.push(gl);
    if (o.witness_ref) lines.push(clip("      " + paint(`witness ${o.witness_ref}${o.witness_hash ? "  " + o.witness_hash : ""}`, C.cyan), w));
    if (o.status !== "DISCHARGED" && o.detail) lines.push(clip("      " + paint(String(o.detail), C.yellow), w));
  }
  return lines;
}

// Render a table obligation's verified grid as aligned rows (header dim), each
// clipped to width. Small (top-k) grids only; that's what tables carry.
function gridLines(grid: any, width: number): string[] {
  const rows = grid.rows ?? [];
  const cols: string[] = (grid.columns && grid.columns.length)
    ? grid.columns : Object.keys(rows[0] ?? {});
  if (!cols.length || !rows.length) return [];
  const wcol = cols.map((c) =>
    Math.max(c.length, ...rows.map((r: any) => String(r[c] ?? "").length)));
  const fmt = (vals: string[]) =>
    cols.map((_c, i) => String(vals[i] ?? "").padEnd(wcol[i])).join("  ");
  const out = [clip("      " + paint(fmt(cols), C.dim), width)];
  for (const r of rows) out.push(clip("      " + fmt(cols.map((c) => r[c])), width));
  return out;
}

const certComponent = (cert: any) => ({ render: (width: number) => certLines(cert, width) });

// Terse, plain-text (no ANSI) verdict summary — the tool RESULT the model sees,
// in place of the raw JSON. Enough to act on and to report witness refs; the full
// detail lives in the transcript card.
function certText(cert: any): string {
  // The same card the TUI draws, as plain text (no ANSI) — this is the tool
  // result content, so external viewers and the model get the full certificate.
  return stripAnsi(certLines(cert, 200).join("\n"));
}

// Render a source-lineage verdict ({verdict, alias, sources, upstream_entries,
// traversable, issues, detail}) as the SAME styled card as a certificate, so a
// lineage check reads like a verify check in the transcript.
const REMOTE_URI = /^[a-z0-9]+:\/\//i;
function lineageLines(v: any, width: number): string[] {
  const w = Math.max(24, width || 80);
  const verdict = v.verdict ?? (v.legit ? "VERIFIED" : "DISCREPANCY");
  const badge = ({ VERIFIED: "✅", DISCREPANCY: "❌", "COULD-NOT-VERIFY": "⚠" } as any)[verdict] ?? "•";
  const color = verdict === "VERIFIED" ? C.green : verdict === "DISCREPANCY" ? C.red : C.yellow;
  const srcs = v.sources ?? [];
  const lines: string[] = [];
  lines.push(clip(
    paint(` ${badge} ${verdict} `, C.bold + color) +
      paint(`  lineage · ${v.alias ?? "?"}  ·  ${srcs.length} source${srcs.length === 1 ? "" : "s"}`, C.dim),
    w));
  const trav = v.traversable ? paint("✓", C.green) : paint("✗", C.red);
  lines.push(clip("   " + trav + " " + paint("lineage reaches a source from its root", C.dim), w));
  for (const s of srcs) {
    const path = s.path ?? s.table ?? "?";
    const remote = typeof path === "string" && REMOTE_URI.test(path);
    const mark = remote ? paint("✓", C.green) : paint("·", C.yellow);
    lines.push(clip(
      `   ${mark} ${paint(String(s.method || s.op || "source"), C.bold)}(${path})` +
        paint(`  via ${s.con_name ?? "?"}`, C.dim),
      w));
  }
  for (const up of v.upstream_entries ?? []) {
    lines.push(clip("      " + paint(`composed-from ${up.entry_name ?? "?"} (@${up.alias ?? "?"})`, C.cyan), w));
  }
  for (const issue of v.issues ?? []) {
    lines.push(clip("   " + paint("✗ " + String(issue), C.red), w));
  }
  return lines;
}

const lineageComponent = (v: any) => ({ render: (width: number) => lineageLines(v, width) });
function lineageText(v: any): string {
  return stripAnsi(lineageLines(v, 200).join("\n"));
}

export default function (pi: ExtensionAPI) {
  // Discharged values accumulated for the CURRENT answer/turn, the ground truth the
  // gate re-stamps from. Accumulated within a turn (an agent verifies piecemeal —
  // share, count, population in separate calls) and RESET after each terminal answer
  // (see message_end), so a figure discharged for an earlier, unrelated question — or
  // against a since-mutated catalog — cannot silently back a later claim.
  let blessed: number[] = [];
  let hasCert = false; // was xorq_verify run for this answer
  // Standing refutations / coverage gaps, folded per certificate with value-level
  // supersession: a later cert that DISCHARGES a surface clears its earlier
  // refutation or uncoverage (the sanctioned repair loop); anything never
  // re-discharged stands and refuses the banner.
  let fold: CertFold = emptyFold();
  let sawExtremal = false; // did any certificate DISCHARGE an argmax/argmin/ranking —
  // the only backing a superlative in prose can have (a scalar cannot back "highest")
  let lastCatalogState = ""; // most recent catalog hash, for the banner
  // Source-lineage verdict per alias (set by xorq_check_lineage). The gate reads
  // these too: a DISCHARGED value over a lineage the checker REJECTED is not a
  // verified answer, so a lineage DISCREPANCY downgrades the banner. A DISCREPANCY
  // is sticky within the turn — a later, more permissive re-check cannot launder it.
  let lineageByAlias = new Map<string, any>();
  const resetGateState = () => {
    blessed = [];
    hasCert = false;
    fold = emptyFold();
    sawExtremal = false;
    lastCatalogState = "";
    lineageByAlias = new Map<string, any>();
  };

  // The tool result content is the plain-text card (captured everywhere); in the pi
  // TUI, renderResult redraws it as the styled card from `details`. One factory for
  // both the certificate and the lineage card — they differ only in the text
  // renderer and the "is this a valid parse" test. `parsed` may be supplied by the
  // caller (the handler already parsed it once) to avoid a second JSON.parse.
  const makeResult =
    (toText: (v: any) => string) =>
    (stdout: string, parsed?: any) => {
      const v = parsed ?? (CERT_RENDER ? parseCert(stdout) : null);
      if (!v) return { content: [{ type: "text", text: stdout }] }; // off / unparseable → raw
      return { content: [{ type: "text", text: toText(v) }], details: v };
    };
  const makeInlineRender =
    (toComponent: (v: any) => any, valid: (v: any) => boolean) =>
    (result: any) => {
      const v = result.details ?? parseCert(result.content?.[0]?.text ?? "");
      return v && valid(v)
        ? toComponent(v)
        : { render: (w: number) => [clip(String(result.content?.[0]?.text ?? ""), w)] };
    };
  const certResult = makeResult(certText);
  const inlineRender = makeInlineRender(certComponent, (c) => !!c);
  const lineageResult = makeResult(lineageText);
  const inlineRenderLineage = makeInlineRender(
    lineageComponent, (v) => !!(v.verdict || v.legit !== undefined),
  );
  // ----------------------------------------------------------------------- //
  // catalog inspection                                                      //
  // ----------------------------------------------------------------------- //
  pi.registerTool({
    name: "xorq_catalog_list_aliases",
    label: "List catalog aliases",
    description:
      "List declared aliases in the xorq catalog. The verifier's witnesses may " +
      "only compose on these declared aliases (never a bare raw source).",
    parameters: Type.Object({
      catalog_path: Type.String({ description: "Path to the xorq catalog" }),
    }),
    async execute(_id, params, signal) {
      const r = await pi.exec(
        "xorq",
        ["catalog", "-p", params.catalog_path, "list-aliases"],
        { timeout: SHORT, signal },
      );
      if (r.code !== 0) throw new Error(`xorq catalog list-aliases: ${r.stderr}`);
      return { content: [{ type: "text", text: r.stdout }] };
    },
  });

  pi.registerTool({
    name: "xorq_catalog_info",
    label: "Catalog info",
    description: "Show catalog metadata: path, remotes, entry/alias counts.",
    parameters: Type.Object({
      catalog_path: Type.String({ description: "Path to the xorq catalog" }),
    }),
    async execute(_id, params, signal) {
      const r = await pi.exec(
        "xorq",
        ["catalog", "-p", params.catalog_path, "info"],
        { timeout: SHORT, signal },
      );
      if (r.code !== 0) throw new Error(`xorq catalog info: ${r.stderr}`);
      return { content: [{ type: "text", text: r.stdout }] };
    },
  });

  pi.registerTool({
    name: "xorq_catalog_schema",
    label: "Alias schema",
    description:
      "Show a declared alias's typed schema (column names + dtypes), read from " +
      "the entry's serialized metadata — no execution, no source fetch. Use this " +
      "to learn which columns a witness may select BEFORE composing it — the " +
      "verifier discovers schema through the catalog, never by reading source " +
      "files.",
    parameters: Type.Object({
      catalog_path: Type.String({ description: "Path to the xorq catalog" }),
      alias: Type.String({
        description: "A declared alias from xorq_catalog_list_aliases",
      }),
    }),
    async execute(_id, params, signal) {
      const r = await pi.exec(
        "xorq",
        ["catalog", "-p", params.catalog_path, "schema", params.alias],
        { timeout: SHORT, signal },
      );
      if (r.code !== 0) throw new Error(`xorq catalog schema: ${r.stderr}`);
      return { content: [{ type: "text", text: r.stdout }] };
    },
  });

  // ----------------------------------------------------------------------- //
  // compute (for PRODUCERS): obtain a value FROM an expression              //
  // ----------------------------------------------------------------------- //
  pi.registerTool({
    name: "xorq_select",
    label: "Select from catalog",
    description:
      "Compute a value FROM the catalog: compose an expression on a DECLARED alias, " +
      "run it, and return the result rows as CSV. Use this to OBTAIN every number you " +
      "will state — never invent a figure, select it from an expression. Compose on " +
      "`source`, e.g. source.order_by(source.n.desc()).limit(1).select('origin','n'). " +
      "Fast to call repeatedly: the alias's sources are fetched ONCE into a local " +
      "snapshot, and every later compose on that alias short-circuits to it (peeks are " +
      "cheap; xorq_verify always re-executes from the real sources, so the certificate " +
      "is unaffected). Results are capped at `limit` rows (default 50) so you don't " +
      "flood context — to COUNT a population, don't page through rows, use an aggregate " +
      "(source.aggregate(n=source.count())) or declare a `count` obligation. Reuse the " +
      "SAME compose string as the witness so the number is verified-by-construction.",
    parameters: Type.Object({
      catalog_path: Type.String({ description: "Path to the xorq catalog" }),
      on: Type.String({ description: "A DECLARED alias from xorq_catalog_list_aliases" }),
      compose: Type.String({
        description: "Expression over `source` (ideally with a .limit(...))",
      }),
      limit: Type.Optional(
        Type.Number({
          description:
            "Max rows returned (default 50). A `.limit(...)` already in `compose` is respected; otherwise this is appended. Pass 0 to uncap (avoid — large results flood context; aggregate instead).",
        }),
      ),
    }),
    async execute(_id, params, signal) {
      // Cap the reply by default so a select can't dump a whole table into
      // context (and to nudge counting via an aggregate, not row-paging).
      // The checker's `select` respects a .limit(...) already in `compose`.
      // It composes on a SNAPSHOT of the alias (fetched once, reused across
      // peeks); only xorq_verify re-executes from the declared sources.
      const n = params.limit === undefined ? 50 : params.limit;
      const [cmd, argv] = checkCmd(
        "select",
        "--catalog-path", params.catalog_path,
        "--on", params.on,
        "-c", params.compose,
        "--limit", String(n),
      );
      const r = await pi.exec(cmd, argv, { timeout: LONG, signal });
      if (r.code !== 0) throw new Error(`xorq_select: ${r.stderr}`);
      const capped = n > 0 && !/\.limit\s*\(/.test(params.compose);
      const note = capped
        ? `\n(showing up to ${n} rows; pass limit or aggregate to count — do not page)`
        : "";
      return { content: [{ type: "text", text: r.stdout + note }] };
    },
  });

  // ----------------------------------------------------------------------- //
  // obligation discharge (the deterministic decision procedure)             //
  // ----------------------------------------------------------------------- //
  pi.registerTool({
    name: "xorq_verify",
    label: "Discharge obligations",
    description:
      "Run the deterministic checker over a set of DECLARED expressions and " +
      "OBLIGATIONS and return one re-checkable certificate. Each obligation is " +
      "{id, kind, surface, witness:{on, compose}, predicate, value_type, " +
      "requires_sources}. Verify a TABLE/RANKING as one `kind:table` obligation " +
      "(predicate.columns/rows/ordered/metric_col, population in witness.compose) so every cell and the " +
      "ordering are checked — do not cherry-pick scalars from a table. Put EVERY " +
      "number you will print in `reply_values` (uncovered values downgrade to " +
      "COULD-NOT-VERIFY). A superlative/ranking claim in your prose ('highest', " +
      "'leads all', 'second-largest') MUST be backed by a DISCHARGED " +
      "argmax/argmin/table obligation this turn — scalar certificates cannot back " +
      "it, and the answer gate stamps NOT VERIFIED otherwise; rephrase without the " +
      "superlative if you did not verify it. Discharged witnesses are persisted as re-runnable " +
      "`verify-<id>` catalog entries by default (catalog_witnesses). Renders the " +
      "certificate as a card in the transcript and returns a terse verdict " +
      "SUMMARY (verdict + per-obligation status + verify-<id> refs), NOT the raw " +
      "JSON — do not re-emit a certificate; state the verdict and name the refs. " +
      "The verdict is folded by the checker (monotone) — you do not decide it. " +
      "See docs/adr/0001 for the schema.",
    parameters: Type.Object({
      request: Type.Object(
        {
          catalog_path: Type.String(),
          expressions: Type.Optional(Type.Array(Type.Any())),
          obligations: Type.Array(Type.Any()),
          reply_values: Type.Optional(Type.Array(Type.String())),
          catalog_witnesses: Type.Optional(
            Type.Boolean({
              description:
                "Persist each DISCHARGED witness as a composed `verify-<id>` catalog entry (re-runnable, reusable; the summary lists each `verify-<id>`). DEFAULTS TO TRUE — a verified answer leaves its witness in the catalog. Pass false only for a throwaway check you do not want recorded.",
            }),
          ),
        },
        { additionalProperties: true },
      ),
    }),
    async execute(_id, params, signal) {
      // Persist witnesses by default: a verified answer should leave a
      // re-runnable `verify-<id>` entry in the catalog, not discard the
      // expression it was checked against. Explicit false opts out.
      const request = {
        catalog_witnesses: true,
        ...params.request,
      };
      const dir = mkdtempSync(join(tmpdir(), "pi-xorq-verify-"));
      const reqPath = join(dir, "request.json");
      writeFileSync(reqPath, JSON.stringify(request), "utf8");
      const [cmd, argv] = checkCmd("verify", reqPath);
      const r = await pi.exec(cmd, argv, { timeout: LONG, signal });
      if (r.code !== 0) throw new Error(`pi-xorq-check verify: ${r.stderr}`);
      // Accumulate this certificate's discharged values into the session union —
      // the gate re-stamps from what was verified across the whole session, not
      // from the model's prose or a single (last) call. Refutations and coverage
      // gaps fold with value-level supersession (see foldCertificate).
      const parsed = parseCert(r.stdout);
      if (parsed) {
        hasCert = true;
        if (parsed.catalog_state) lastCatalogState = String(parsed.catalog_state);
        fold = foldCertificate(fold, parsed);
        if (dischargedExtremal(parsed)) sawExtremal = true;
        for (const v of dischargedValues(parsed)) blessed.push(v);
      }
      // Result content = plain-text certificate (captured by AgentsView & model);
      // the TUI draws it as a card via renderResult below. Reuse the parse above.
      return certResult(r.stdout, parsed);
    },
    renderShell: CERT_RENDER ? "self" : undefined,
    renderResult: CERT_RENDER ? inlineRender : undefined,
  });

  // ----------------------------------------------------------------------- //
  // source lineage (deterministic, SEPARATE from value faithfulness)         //
  // ----------------------------------------------------------------------- //
  pi.registerTool({
    name: "xorq_check_lineage",
    label: "Check source lineage",
    description:
      "Deterministic SOURCE-lineage check for one catalog alias — SEPARATE from " +
      "xorq_verify (which grounds values). Reads the alias's serialized entry " +
      "bundle (expr.yaml/profiles.yaml/expr_metadata.json) and verifies its ACTUAL " +
      "source is legitimate: the profile resolves, a local path EXISTS (remote " +
      "URIs trusted when well-formed), composed-from entries are REAL catalog " +
      "entries (no fabricated lineage), and the lineage DAG reaches a source. " +
      "Returns a JSON verdict {verdict, legit, sources, issues}. Call it on every " +
      "alias you produced or answered from: a hallucinated, broken, or " +
      "non-reproducible source fails here even when the value is faithful. Pass " +
      "no_local:true to require a re-fetchable (remote) source — a local/in-memory " +
      "source is rejected even if it exists (data hand-added to the catalog).",
    parameters: Type.Object({
      catalog_path: Type.String({ description: "Path to the xorq catalog" }),
      alias: Type.String({ description: "A declared alias to check" }),
      no_local: Type.Optional(
        Type.Boolean({ description: "Require a remote (re-fetchable) source." }),
      ),
    }),
    async execute(_id, params, signal) {
      const [cmd, argv] = checkCmd(
        "lineage",
        "--alias", params.alias,
        "--catalog-path", params.catalog_path,
        ...(params.no_local ? ["--no-local"] : []),
      );
      const r = await pi.exec(cmd, argv, { timeout: SHORT, signal });
      if (r.code !== 0) throw new Error(`pi-xorq-check lineage: ${r.stderr}`);
      // Track the verdict per alias so the answer gate can honour it at message_end.
      // A DISCREPANCY is sticky: a subsequent, more permissive re-check of the same
      // alias (e.g. dropping no_local) cannot overwrite a rejection into a pass.
      const v = parseCert(r.stdout);
      if (v && v.alias) {
        const prior = lineageByAlias.get(String(v.alias));
        if (!(prior?.verdict === "DISCREPANCY" && v.verdict !== "DISCREPANCY")) {
          lineageByAlias.set(String(v.alias), v);
        }
      }
      // Same as xorq_verify: plain-text card as the result, styled card in the TUI.
      return lineageResult(r.stdout, v);
    },
    renderShell: CERT_RENDER ? "self" : undefined,
    renderResult: CERT_RENDER ? inlineRenderLineage : undefined,
  });

  // (assert_fact removed: a single-fact check has no coverage audit, so it let
  // an answer's other numbers ship unverified. Everything goes through
  // xorq_verify — one obligation per claim + reply_values — so nothing you print
  // is unchecked.)

  // ----------------------------------------------------------------------- //
  // answer gate: re-stamp the authoritative verdict on the terminal answer   //
  // ----------------------------------------------------------------------- //
  // The model cannot self-certify. On each terminal assistant answer (one with no
  // tool calls) that states numbers, we compare its figures against THIS turn's
  // discharged values and prepend the checker's own verdict banner. Non-destructive:
  // the answer body is untouched. State is reset after each terminal answer, so a
  // later answer is judged only on its own verification.
  if (GATE) {
    const stampedIds = new Set<string>();
    // Lines the model itself wrote containing the sentinel are stripped before
    // extracting claims and before stamping, so a forged banner can neither suppress
    // the claim check nor stand in for the checker's verdict.
    const stripForged = (s: string) =>
      s.split("\n").filter((line) => !line.includes(GATE_MARK)).join("\n");
    pi.on("message_end", async (event) => {
      const msg: any = event?.message;
      if (!msg || msg.role !== "assistant" || !Array.isArray(msg.content)) return;
      // Skip non-terminal messages (those that request tools) — gate only answers.
      if (msg.content.some((p: any) => p && p.type === "toolCall")) return;
      const textParts = msg.content.filter((p: any) => p && p.type === "text");
      if (!textParts.length) return;
      const mid = String(msg.id ?? msg.messageId ?? "");
      if (mid && stampedIds.has(mid)) return; // already processed this message
      const answer = stripForged(textParts.map((p: any) => p.text).join("\n"));
      const claims = answerClaims(answer);
      // Superlatives are gated even when the answer states NO number — "Region X
      // leads the nation in per-store sales" is a data claim the numeric net cannot
      // see (the demonstrated smuggle: a ✅ turn whose prose appended "the highest
      // concentration among all regions" with no argmax obligation).
      const superlatives = answerSuperlatives(answer);
      // Snapshot state, then reset for the next turn regardless of outcome.
      const [_hasCert, _cat, _fold, _extremal, _lineageMap] =
        [hasCert, lastCatalogState, fold, sawExtremal, lineageByAlias] as const;
      const _blessed = blessed;
      resetGateState();
      if (mid) stampedIds.add(mid);
      if (!claims.length && !superlatives.length) return; // no data claims → nothing to certify
      const unbacked = claims.filter((c) => !gbacked(c, _blessed)).map((c) => c.n);
      // A lineage the checker REJECTED (DISCREPANCY) blocks the answer even when the
      // values are discharged — the source is what a hardcoded/fabricated input
      // poisons. (COULD-NOT-VERIFY lineage does not block: unread ≠ refuted.)
      const lineageFails = [..._lineageMap.values()].filter(
        (v) => v && v.verdict === "DISCREPANCY",
      );
      const banner = gateBanner({
        hasCert: _hasCert,
        catalogState: _cat,
        unbacked,
        lineageFails,
        refuted: _fold.refuted,
        uncoveredCert: _fold.uncovered,
        lineageChecked: _lineageMap.size > 0,
        superlatives,
        hasExtremal: _extremal,
      });
      if (!banner) return;
      return {
        message: {
          ...msg,
          content: [{ type: "text", text: banner + "\n\n" }, ...msg.content],
        },
      };
    });
  }

  // ----------------------------------------------------------------------- //
  // status                                                                  //
  // ----------------------------------------------------------------------- //
  pi.on("session_start", async (_event, ctx) => {
    const r = await pi.exec("xorq", ["--version"], { timeout: SHORT });
    if (r.code === 0) {
      ctx.ui.setStatus(
        "xorq",
        `xorq catalog tools loaded — certificate card: ${CERT_RENDER ? "on" : "off"} · answer gate: ${GATE ? "on" : "off"}`,
      );
    } else {
      ctx.ui.setStatus("xorq", "xorq not on PATH — `pip install xorq`");
    }
  });
}
