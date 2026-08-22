# Blog-post trial runs

The nine n=100 trial runs behind the blog table, isolated from the exploratory
trials that stayed in `bench/trials/`. Every figure in the table was recomputed
from each directory's `results.json` and matches digit-for-digit ("Tokens" is
the sum of all token fields — input, output, cache read, cache creation —
across the 100 runs).

| Prompt | Harness | Model | Semantic model | Right | Median | Agent-time | Turns | Cost | Tokens | Directory |
|---|---|---|---|---|---|---|---|---|---|---|
| scoped | Vanilla Claude Code | Haiku 4.5 | No | 4/100 | 14.9s | 1,561s | 704 / 7 | $6.71 / $0.067 | 17.15M | `n100-denominator-us-claude-haiku` |
| scoped | pi + Xorq | Haiku 4.5 | No | 97/100 | 98.3s | 12,818s | 2,430 / 24 | $10.03 / $0.100 | 43.22M | `n100-denominator-us-pi` |
| scoped | Vanilla Claude Code | Sonnet 5 | No | 100/100 | 18.0s | 1,830s | 551 / 6 | $15.80 / $0.158 | 20.87M | `n100-denominator-us-claude-sonnet` |
| scoped | Vanilla Claude Code | Opus 5 | No | 100/100 | 21.2s | 2,117s | 542 / 6 | $18.45 / $0.185 | 13.47M | `n100-denominator-us-claude-opus` |
| hint-free | Vanilla Claude Code | Haiku 4.5 | No | 0/100 | 14.9s | 1,519s | 677 / 7 | $6.53 / $0.065 | 16.61M | `n100-semantic-claude-haiku` |
| hint-free | Vanilla Claude Code | Sonnet 5 | No | 0/100 | 14.2s | 1,440s | 395 / 4 | $11.85 / $0.118 | 14.83M | `n100-semantic-claude-sonnet` |
| hint-free | Vanilla Claude Code | Opus 5 | No | 94/100 | 23.3s | 2,381s | 501 / 5 | $16.52 / $0.165 | 12.43M | `n100-semantic-claude-opus` |
| hint-free | pi + Xorq | Haiku 4.5 | No | 0/100 | 84.0s | 12,850s | 2,227 / 21 | $9.09 / $0.091 | 38.67M | `n100-semantic-nomodel-pi` |
| hint-free | pi + Xorq | Haiku 4.5 | Yes | 100/100 | 21.3s | 3,818s | 457 / 4 | $2.16 / $0.022 | 4.34M | `n100-semantic-pi-frictionfix` |

Prompt mapping: "scoped" is the `denominator-us` trap (scope hints in the
prompt); "hint-free" is `denominator-us-semantic` (no scope hints; the pi row
with a semantic model runs against the seeded `us_markets` BSL model).

Near-miss to keep straight: `n100-semantic-pi` (still in `bench/trials/`) is
the pre-frictionfix version of the last row — 99/100, $2.32, 5.23M tokens. It
is NOT the run in the table; `n100-semantic-pi-frictionfix` is.
