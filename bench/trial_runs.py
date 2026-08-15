"""Measure the two harnesses' hit rates on a trap, N runs each, headless.

Runs the same trap prompt through:

  claude   bare Claude Code (`claude -p`) in a FRESH tmp dir per run — the
           duel's left pane, minus tmux: no MCP servers, tool allow-list
           pre-authorized, empty context.
  pi       the verification harness (`pi -p --approve`) in an ISOLATED copy of
           this project per run — extension + skill + AGENTS.md, its own
           freshly-initialized catalog — the duel's middle pane, minus tmux.

Each run is scored deterministically against the trap's oracle: the answer
region either contains the oracle value (right), one of the trap's named baits
(wrong, attributably), some other figure (wrong, other), or errored/timed out.
pi runs additionally record the answer gate's banner (⟦xorq-checker⟧
✅ VERIFIED / ⚠ NOT VERIFIED) so "right" and "stamped verified" can be
cross-tabulated. Full stdout/stderr of every run is kept for audit, and each
run's token usage and API cost are recorded in results.json (claude via its
JSON result payload; pi via `--mode json` event-stream usage).

Run inside `nix develop` (needs pi, xorq, pi-xorq-check, claude on PATH):

    python bench/trial_runs.py                          # both harnesses, n=20
    python bench/trial_runs.py --harness pi --n 5
    python bench/trial_runs.py --trap national-sum --jobs 4
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
GATE_MARK = "⟦xorq-checker⟧"
MODEL_CLAUDE = "claude-haiku-4-5"
MODEL_PI = "anthropic/claude-haiku-4-5"

# Per-trap deterministic scoring: `right` must match the oracle value as the
# prompt pins it; `baits` are the trap's NAMED wrong readings (attributable
# failure modes), checked only when `right` is absent.
SCORING = {
    # The two-value alternations pair either numerator (7,942 territories
    # excluded, or 7,944 whole-file) with the same wrong denominator.
    "denominator-us": {
        "right": r"2\.3237",
        "baits": {
            "territories-in-numerator (2.3243)": r"2\.3243",
            "double-count-all-rows (0.5796/0.5797)": r"0\.579[67]",
            "sumlev40-adds-PR (2.3022/2.3028)": r"2\.302[28]",
        },
    },
    "national-sum": {
        "right": r"\b7,?942\b",
        "baits": {
            "memory-prior (~8,6xx-8,7xx)": r"\b8[,.]?[67]\d{2}\b",
            "whole-file-total (7,944)": r"\b7,?944\b",
        },
    },
    # Scores like denominator-us, but the prompt has no scope hints: the pi
    # runs get a catalog pre-seeded with the reviewed BSL model (see
    # SEED_TRAPS below), so "right" means the agent bound to the reviewed
    # metric instead of re-deriving the scope.
    "denominator-us-semantic": {
        "right": r"2\.3237",
        "baits": {
            "territories-in-numerator (2.3243)": r"2\.3243",
            "double-count-all-rows (0.5796/0.5797)": r"0\.579[67]",
            "sumlev40-adds-PR (2.3022/2.3028)": r"2\.302[28]",
        },
    },
}

# Traps whose pi runs need the catalog pre-seeded with the reviewed BSL
# semantic model before the agent starts (bare claude runs get nothing —
# that asymmetry is the experiment).
SEED_TRAPS = {"denominator-us-semantic"}
SEED_FILES = ("bench/bsl_us_markets.py", "bench/seed_semantic_catalog.sh")

# Passed via --allowedTools: in headless -p mode a fresh dir is UNTRUSTED, so a
# project .claude/settings.json allow-list is ignored (interactively, the duel's
# human accepts the folder-trust prompt; headless there is no prompt). The CLI
# flag is operator-provided and therefore trusted. Domain-scoped WebFetch rules
# cover the traps' data sources — a permission wall would score as a failure
# for the wrong reason.
CLAUDE_ALLOWED_TOOLS = ",".join((
    "Bash", "Read", "Edit", "Write", "MultiEdit", "Glob", "Grep",
    "WebFetch", "WebSearch", "TodoWrite", "NotebookEdit",
    "WebFetch(domain:harvestlymarkets.com)",
    "WebFetch(domain:gist.githubusercontent.com)",
    "WebFetch(domain:raw.githubusercontent.com)",
    "WebFetch(domain:www2.census.gov)",
))

# What each pi run's project copy needs: the extension package, the skill, the
# analyst role, pi's project settings — and the Python project files
# (pyproject/uv.lock/LICENSE/src), because `xorq catalog add` builds a wheel of
# the enclosing project and errors without them. The catalog is created fresh.
PI_PROJECT_FILES = ("package.json", "AGENTS.md", "pyproject.toml", "uv.lock", "LICENSE")
PI_PROJECT_DIRS = ("extensions", "skills", ".pi", "src")


def duel_prompt(trap_id: str) -> str:
    out = subprocess.run(
        (sys.executable, str(REPO / "bench" / "hallucination_prompts.py"), "--duel", trap_id),
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def claude_tokens(payload: dict) -> dict:
    """Sum token counts across ALL models in a claude JSON result — the top-level
    `usage` covers only the main agent loop, not sidechannel calls."""
    mu = payload.get("modelUsage") or {}
    return {
        "input": sum(m.get("inputTokens", 0) for m in mu.values()),
        "output": sum(m.get("outputTokens", 0) for m in mu.values()),
        "cacheRead": sum(m.get("cacheReadInputTokens", 0) for m in mu.values()),
        "cacheWrite": sum(m.get("cacheCreationInputTokens", 0) for m in mu.values()),
    }


def pi_answer_and_usage(stdout: str) -> tuple[str, dict, float | None]:
    """Parse a `pi --mode json` event stream: the terminal answer (last assistant
    message with text and no tool calls — the gate's banner is prepended into that
    same message), summed token usage, and total cost."""
    answer = ""
    tokens = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
    cost = 0.0
    priced = False
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "message_end":
            continue
        msg = ev.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        u = msg.get("usage") or {}
        if u:
            priced = True
            for k in tokens:
                tokens[k] += u.get(k, 0)
            cost += (u.get("cost") or {}).get("total", 0.0)
        content = [p for p in (msg.get("content") or []) if isinstance(p, dict)]
        texts = [p.get("text", "") for p in content if p.get("type") == "text"]
        if texts and not any(p.get("type") == "toolCall" for p in content):
            answer = "\n".join(texts)
    return answer, (tokens if priced else {}), (round(cost, 6) if priced else None)


def classify(answer: str, trap_id: str) -> str:
    rules = SCORING[trap_id]
    if re.search(rules["right"], answer):
        return "right"
    for name, pat in rules["baits"].items():
        if re.search(pat, answer):
            return f"bait: {name}"
    return "wrong: other"


def run_claude(prompt: str, trap_id: str, out_dir: Path, idx: int, timeout: int,
               model: str = MODEL_CLAUDE) -> dict:
    work = Path(tempfile.mkdtemp(prefix=f"trial-claude-{idx}-"))
    (work / ".claude").mkdir()
    (work / ".claude" / "empty-mcp.json").write_text('{"mcpServers":{}}')
    cmd = (
        "claude", "-p", prompt, "--model", model, "--output-format", "json",
        "--strict-mcp-config", "--mcp-config", str(work / ".claude" / "empty-mcp.json"),
        "--allowedTools", CLAUDE_ALLOWED_TOOLS,
    )
    t0 = time.time()
    rec = {"harness": "claude", "run": idx, "model": model}
    try:
        proc = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=timeout)
        (out_dir / f"claude-{idx:02d}.stdout").write_text(proc.stdout)
        (out_dir / f"claude-{idx:02d}.stderr").write_text(proc.stderr)
        try:
            payload = json.loads(proc.stdout)
            answer = payload.get("result", "")
            rec["cost_usd"] = payload.get("total_cost_usd")
            rec["tokens"] = claude_tokens(payload)
        except (json.JSONDecodeError, AttributeError):
            answer = proc.stdout
        rec["verdict"] = classify(answer, trap_id) if answer.strip() else "error: empty"
        rec["answer"] = answer[-400:]
    except subprocess.TimeoutExpired:
        rec["verdict"] = "error: timeout"
    finally:
        rec["seconds"] = round(time.time() - t0, 1)
        shutil.rmtree(work, ignore_errors=True)
    return rec


def run_pi(prompt: str, trap_id: str, out_dir: Path, idx: int, timeout: int,
           model: str = MODEL_PI, thinking: str | None = None) -> dict:
    work = Path(tempfile.mkdtemp(prefix=f"trial-pi-{idx}-"))
    for f in PI_PROJECT_FILES:
        shutil.copy2(REPO / f, work / f)
    for d in PI_PROJECT_DIRS:
        shutil.copytree(
            REPO / d, work / d,
            ignore=shutil.ignore_patterns("npm", "git", "node_modules"),
        )
    if trap_id in SEED_TRAPS:
        (work / "bench").mkdir()
        for f in SEED_FILES:
            shutil.copy2(REPO / f, work / f)
    subprocess.run(
        ("xorq", "catalog", "-p", str(work / ".xorq" / "catalog"), "init"),
        capture_output=True, check=True,
    )
    # A build needs a git HEAD (xorq records git state).
    subprocess.run(("git", "init", "-q"), cwd=work, capture_output=True)
    subprocess.run(("git", "add", "-A"), cwd=work, capture_output=True)
    subprocess.run(
        ("git", "-c", "user.email=trial@local", "-c", "user.name=trial",
         "commit", "-qm", "trial fixture"),
        cwd=work, capture_output=True,
    )
    # Seeded traps: put the reviewed semantic model into the fresh catalog
    # before the agent starts (after the commit — builds record git state).
    if trap_id in SEED_TRAPS:
        subprocess.run(
            ("bash", "bench/seed_semantic_catalog.sh", ".xorq/catalog"),
            cwd=work, capture_output=True, check=True,
        )
    cmd = ("pi", "-p", "--approve", "--no-session", "--mode", "json",
           "--model", model,
           *(("--thinking", thinking) if thinking else ()), prompt)
    t0 = time.time()
    rec = {"harness": "pi", "run": idx, "model": model}
    if thinking:
        rec["thinking"] = thinking
    try:
        proc = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=timeout)
        (out_dir / f"pi-{idx:02d}.stdout").write_text(proc.stdout)
        (out_dir / f"pi-{idx:02d}.stderr").write_text(proc.stderr)
        text = proc.stdout
        answer, tokens, cost = pi_answer_and_usage(text)
        if tokens:
            rec["tokens"] = tokens
        if cost is not None:
            rec["cost_usd"] = cost
        rec["verdict"] = classify(answer, trap_id) if text.strip() else "error: empty"
        # The gate prepends its banner into the TERMINAL answer message, so read
        # it from the parsed answer — the raw event stream also contains
        # ✅ VERIFIED strings from per-witness tool results, which are not the
        # gate's verdict on the final answer.
        if GATE_MARK in answer:
            rec["banner"] = "verified" if "✅ VERIFIED" in answer else "not-verified"
        else:
            rec["banner"] = "absent"
        rec["answer"] = answer[-400:]
    except subprocess.TimeoutExpired:
        rec["verdict"] = "error: timeout"
        rec["banner"] = "absent"
    finally:
        rec["seconds"] = round(time.time() - t0, 1)
        shutil.rmtree(work, ignore_errors=True)
    return rec


def summarize(records: list[dict], harness: str) -> list[str]:
    rows = [r for r in records if r["harness"] == harness]
    if not rows:
        return []
    lines = [f"\n{harness} — {len(rows)} runs "
             f"(median {sorted(r['seconds'] for r in rows)[len(rows) // 2]}s):"]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    for verdict in sorted(counts, key=counts.get, reverse=True):
        lines.append(f"  {counts[verdict]:2d}/{len(rows)}  {verdict}")
    costs = [r["cost_usd"] for r in rows if r.get("cost_usd") is not None]
    if costs:
        lines.append(f"  cost: ${sum(costs):.2f} total over {len(costs)} priced runs "
                     f"(${sum(costs) / len(costs):.3f}/run)")
    toks = [r["tokens"] for r in rows if r.get("tokens")]
    if toks:
        tot = {k: sum(t[k] for t in toks) for k in ("input", "output", "cacheRead", "cacheWrite")}
        lines.append(f"  tokens: {sum(tot.values()):,} total (input {tot['input']:,} / "
                     f"output {tot['output']:,} / cacheRead {tot['cacheRead']:,} / "
                     f"cacheWrite {tot['cacheWrite']:,})")
    if harness == "pi":
        both = sum(1 for r in rows if r["verdict"] == "right" and r.get("banner") == "verified")
        stamped = sum(1 for r in rows if r.get("banner") == "verified")
        honest = sum(1 for r in rows if r["verdict"] != "right" and r.get("banner") != "verified")
        lines.append(f"  banner: {stamped} stamped VERIFIED; {both} right AND stamped; "
                     f"{honest} of the non-right runs were NOT stamped (fail-honest)")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trap", default="denominator-us", choices=sorted(SCORING))
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--harness", default="both", choices=("both", "claude", "pi"))
    ap.add_argument("--claude-model", default=MODEL_CLAUDE,
                    help="model for the bare claude harness (e.g. claude-opus-5)")
    ap.add_argument("--pi-model", default=MODEL_PI,
                    help="model for the pi harness (pi provider/id form)")
    ap.add_argument("--pi-thinking", default=None,
                    choices=("off", "minimal", "low", "medium", "high", "xhigh", "max"),
                    help="pi --thinking level; omit for pi's default")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--claude-timeout", type=int, default=600)
    ap.add_argument("--pi-timeout", type=int, default=1200)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    prompt = duel_prompt(args.trap)
    out_dir = Path(args.out) if args.out else REPO / "bench" / "trials" / args.trap
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "prompt.txt").write_text(prompt)

    tasks = []
    if args.harness in ("both", "claude"):
        tasks += [("claude", i) for i in range(1, args.n + 1)]
    if args.harness in ("both", "pi"):
        tasks += [("pi", i) for i in range(1, args.n + 1)]

    records: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {}
        for h, i in tasks:
            if h == "claude":
                fut = pool.submit(run_claude, prompt, args.trap, out_dir, i,
                                  args.claude_timeout, args.claude_model)
            else:
                fut = pool.submit(run_pi, prompt, args.trap, out_dir, i,
                                  args.pi_timeout, args.pi_model, args.pi_thinking)
            futures[fut] = (h, i)
        for fut in as_completed(futures):
            rec = fut.result()
            records.append(rec)
            done = len(records)
            print(f"[{done}/{len(tasks)}] {rec['harness']}-{rec['run']:02d}: "
                  f"{rec['verdict']}"
                  + (f" [{rec['banner']}]" if "banner" in rec else "")
                  + f" ({rec['seconds']}s)", flush=True)

    records.sort(key=lambda r: (r["harness"], r["run"]))
    (out_dir / "results.json").write_text(json.dumps(records, indent=2))
    print(f"\ntrap: {args.trap}\nprompt: {prompt[:100]}…")
    for harness in ("claude", "pi"):
        for line in summarize(records, harness):
            print(line)
    print(f"\nartifacts: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
