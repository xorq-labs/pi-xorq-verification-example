#!/usr/bin/env bash
#
# duel.sh — open a tmux session with three side-by-side panes:
#   left   : `claude` in a fresh tmp dir (empty context)
#   middle : `pi` in the directory you launch this from
#   right  : xorq catalog setup, then its TUI, in this directory
#
# The catalog is initialized FIRST; only once it's set up do we type the
# prompt into claude and pi and submit it.
#
# The prompt is one of the hallucination traps from bench/hallucination_prompts.py
# — prompts whose only defensible answer is recomputable from the source CSVs, so
# a wrong answer is provable, not arguable. Pass a trap id to pick one:
#
#   ./duel.sh                    # default trap (denominator-us)
#   ./duel.sh national-sum       # any id from: python bench/hallucination_prompts.py
#
# --no-claude drops the bare-agent pane: two panes, pi + the catalog TUI —
# the harness-only view (watch the verification, no duel):
#
#   ./duel.sh --no-claude
#   ./duel.sh --no-claude national-sum
#
# Note: `split-window -h` makes side-by-side panes (vertical dividers).
#       Swap it for `-v` if you'd rather stack them.

set -euo pipefail

command -v tmux >/dev/null 2>&1 || { echo "tmux is not installed" >&2; exit 1; }

SESSION="verification-duel"
REPO_DIR="$(pwd)"
TMP_DIR="$(mktemp -d)"

# Seconds to wait before typing into each thing. Bump if yours are slow.
#   BOOT_WAIT    — how long pi takes to come up
#   CATALOG_WAIT — how long the catalog init chain takes before its TUI is up
#   CLAUDE_WAIT  — settle time after claude's trust prompt (fast: no MCP servers)
BOOT_WAIT="${BOOT_WAIT:-2}"
CATALOG_WAIT="${CATALOG_WAIT:-4}"
CLAUDE_WAIT="${CLAUDE_WAIT:-2}"

# Args: an optional trap id, and --no-claude for the two-pane harness-only view.
SOLO=0
TRAP_ID_ARG=""
for arg in "$@"; do
  case "$arg" in
    --no-claude) SOLO=1 ;;
    *) TRAP_ID_ARG="$arg" ;;
  esac
done

# The prompt is baited so a wrong answer is provable against the cited sources.
# Pick a trap by id; ids and oracles live in bench/hallucination_prompts.py.
# The default is a cross-dataset ratio (markets total ÷ census US population):
# it needs both files ingested and the metric composed as an expression, so the
# whole verification pipeline is exercised — and the denominator has two
# tempting wrong readings that a bare agent falls into in provable ways.
TRAP_ID="${TRAP_ID_ARG:-denominator-us}"
if ! PROMPT="$(python bench/hallucination_prompts.py --duel "$TRAP_ID")"; then
  printf '%s\n' "$PROMPT" >&2  # on an unknown id, bench prints the valid ids
  exit 1
fi
CATALOG_SETUP="rm -rf .xorq/ && mkdir .xorq && xorq catalog -p .xorq/catalog init && xorq catalog -p .xorq/catalog tui"

# Pre-authorize claude in its fresh dir so it never prompts for tool use —
# no --dangerously-skip-permissions needed. The allow-list grants the built-in
# tools; the empty MCP config (used with --strict-mcp-config below) starts
# claude bare, with none of your user/project MCP servers loaded.
if [ "$SOLO" -eq 0 ]; then
mkdir -p "$TMP_DIR/.claude"
cat > "$TMP_DIR/.claude/settings.json" <<'JSON'
{
  "permissions": {
    "allow": [
      "Bash",
      "Read",
      "Edit",
      "Write",
      "MultiEdit",
      "Glob",
      "Grep",
      "WebFetch",
      "WebSearch",
      "TodoWrite",
      "NotebookEdit"
    ],
    "defaultMode": "acceptEdits"
  }
}
JSON
printf '%s\n' '{"mcpServers":{}}' > "$TMP_DIR/.claude/empty-mcp.json"
CLAUDE_CMD="claude --model claude-haiku-4-5 --name demo --strict-mcp-config --mcp-config '$TMP_DIR/.claude/empty-mcp.json'"
fi

# Pin pi to the SAME model as the claude pane, so the duel compares harnesses,
# not models — any difference you see is the verification machinery. Override
# with PI_MODEL=provider/model (run `/model` inside pi to browse the catalog).
#
# --approve pre-trusts THIS repo's project-local files (.pi/settings.json →
# the xorq extension + skill) for the run, so pi never blocks on its project
# trust prompt. It only covers this repo — no global trust state is written.
PI_MODEL="${PI_MODEL:-anthropic/claude-haiku-4-5}"
PI_CMD="pi --model $PI_MODEL --approve"

# Start clean if a session by this name already exists.
tmux kill-session -t "$SESSION" 2>/dev/null || true

# Every pane gets THIS shell's PATH (-e), so pi/xorq from the nix dev shell are
# found even when the tmux server was started outside it.

if [ "$SOLO" -eq 1 ]; then
  # Two panes: pi (left, 55%) and the catalog TUI (right) — no bare agent.
  PANE_PI=$(tmux new-session -d -s "$SESSION" -c "$REPO_DIR" -e PATH="$PATH" -P -F '#{pane_id}')
  PANE_CAT=$(tmux split-window -h -l 45% -t "$PANE_PI" -c "$REPO_DIR" -e PATH="$PATH" -P -F '#{pane_id}')
else
  # Left pane: detached session rooted in the tmp dir (empty context for claude).
  PANE_CLAUDE=$(tmux new-session -d -s "$SESSION" -c "$TMP_DIR" -e PATH="$PATH" -P -F '#{pane_id}')

  # Middle pane: pi takes 75% of the width, leaving claude at 1/4 of the screen.
  PANE_PI=$(tmux split-window -h -l 75% -t "$PANE_CLAUDE" -c "$REPO_DIR" -e PATH="$PATH" -P -F '#{pane_id}')

  # Right pane: split pi's space evenly with the catalog (~3/8 of the screen each).
  PANE_CAT=$(tmux split-window -h -t "$PANE_PI" -c "$REPO_DIR" -e PATH="$PATH" -P -F '#{pane_id}')
fi

# Set up the catalog FIRST — everything else depends on it existing.
tmux send-keys -t "$PANE_CAT" "$CATALOG_SETUP" Enter

# Boot the agent(s) while the catalog initializes.
# claude starts bare (no MCP) with pre-approved permissions from the settings
# file we wrote above — the only startup prompt left is "trust this folder?".
if [ "$SOLO" -eq 0 ]; then
  tmux send-keys -t "$PANE_CLAUDE" "$CLAUDE_CMD" Enter
fi
tmux send-keys -t "$PANE_PI" "$PI_CMD" Enter

# Feed the prompts in the background (two independent jobs) so we can attach
# right away — the panes show instantly and the prompts arrive live.

# --- pi (middle): wait for the catalog + pi boot, then send the prompt ---
{
  PREFIRE_WAIT=$(( BOOT_WAIT > CATALOG_WAIT ? BOOT_WAIT : CATALOG_WAIT ))
  sleep "$PREFIRE_WAIT"
  tmux send-keys -l -t "$PANE_PI" "$PROMPT"
  sleep 1
  tmux send-keys -t "$PANE_PI" Enter
} &

# --- claude (left): accept the folder-trust prompt, then send the prompt ---
# Bare claude still shows "trust the files in this folder?" for the fresh dir.
# Its default is "Yes, proceed", so a single Enter accepts it — and we never
# send a Down, so we can't land on "No".
#
# The claude pane is narrow (1/4 width), so the dialog text WRAPS across lines.
# capture-pane -J un-wraps it, and we match the single word "trust" (which can't
# be split by a wrap) so detection is reliable no matter how narrow the pane is.
if [ "$SOLO" -eq 0 ]; then
{
  for _ in $(seq 1 15); do
    sleep 1
    screen="$(tmux capture-pane -p -J -t "$PANE_CLAUDE" 2>/dev/null || true)"
    if printf '%s' "$screen" | grep -qi "trust"; then
      tmux send-keys -t "$PANE_CLAUDE" Enter
      break
    fi
  done

  # Let claude finish coming up (no MCP, so quick), then send the prompt.
  sleep "$CLAUDE_WAIT"
  tmux send-keys -l -t "$PANE_CLAUDE" "$PROMPT"
  sleep 1
  tmux send-keys -t "$PANE_CLAUDE" Enter
} &
fi

# Attach immediately so the panes are visible from the start.
tmux attach-session -t "$SESSION"
