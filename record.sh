#!/usr/bin/env bash
#
# record.sh — capture the duel as a clean asciinema cast.
#
# Recording a live `tmux attach` naively produces garbled playback for two
# reasons, both fixed here:
#
#   1. geometry drift — tmux repaints with absolute cursor moves. A detached
#      session boots at 80x24, snaps to your terminal on attach, and any size
#      change mid-cast corrupts the replay. Here the session is created at a
#      FIXED size and pinned (window-size=manual) before anything boots.
#   2. terminal dialect — tmux speaks to its client in the OUTER $TERM's
#      language, so recording under ghostty/kitty TERMs bakes escape
#      sequences into the cast that players don't implement. The recording
#      client runs plain TERM=xterm-256color.
#
# usage:
#   ./record.sh demo.cast                     # record the 3-pane duel
#   ./record.sh demo.cast --no-claude         # record the harness-only view
#   COLS=180 ROWS=45 ./record.sh demo.cast    # override the 213x50 default
#
# Detach (prefix-d) — or let the panes exit — to stop the recording. Don't
# resize the terminal while recording. Play back with `asciinema play` in a
# terminal at least COLSxROWS, or render a gif with:  agg demo.cast demo.gif
# (For a margin-free cast, size your terminal to exactly COLSxROWS first.)

set -euo pipefail

command -v asciinema >/dev/null 2>&1 || {
  echo "asciinema not found — it's in the nix dev shell (nix develop)" >&2; exit 1;
}

OUT="${1:?usage: ./record.sh out.cast [duel.sh args…]}"
shift

COLS="${COLS:-213}"
ROWS="${ROWS:-50}"

# The cast records at the current terminal's size; the pinned tmux window must
# fit inside it or the view is clipped.
tty_cols=$(tput cols)
tty_rows=$(tput lines)
if [ "$tty_cols" -lt "$COLS" ] || [ "$tty_rows" -lt "$ROWS" ]; then
  echo "terminal is ${tty_cols}x${tty_rows} — smaller than the ${COLS}x${ROWS} recording." >&2
  echo "Enlarge the window, or record smaller: COLS=$tty_cols ROWS=$tty_rows ./record.sh …" >&2
  exit 1
fi

# Build the session pinned at COLSxROWS, detached; then record a plain-TERM
# client attached to it. Detaching (or the session ending) ends the cast.
DUEL_DETACH=1 DUEL_COLS="$COLS" DUEL_ROWS="$ROWS" ./duel.sh "$@"
TERM=xterm-256color asciinema rec "$OUT" -c "tmux attach -t verification-duel"

echo
echo "cast written to $OUT"
echo "the session may still be running: tmux kill-session -t verification-duel"
