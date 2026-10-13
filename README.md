# Linefox evals

Six browser-only tasks, three browser agents, one model, two trials each.
Everything needed to rerun the comparison is here: the task prompts, the
runner for the two open-source arms, their pinned configs, every raw run, and
the per-call token counts.

## Result

| Arm | Runs | Avg wall time | Avg cost / run | Avg tokens / run |
|---|---|---|---|---|
| Linefox (Pro tier) | 10 | 82 s | $0.28 | 240k |
| OpenClaw 2026.9.4, headless | 12 | 235 s | $0.67 | 2.3M |
| Claude Code 2.1.261 + Playwright MCP 0.0.83, headless | 11 | 141 s | $0.82 | 2.5M |

Per task in `results.csv`. Linefox is cheaper on five of the six tasks and two
to five times faster on the four heavy ones. OpenClaw wins Autotrader outright
(see below). Claude Code without a browser was run as a control and declined
every task in about five seconds; its rows are in `raw/competitor_runs.csv`
under `claude-cli`.

All three arms ran **Claude Sonnet 5**. Costs are at Sonnet 5 list price
(input $2, cache read $0.20, cache write $2.50, output $10 per MTok): Linefox
from its own per-call token log, the other two as their CLIs report. "Tokens"
is uncached input + cache reads + output.

## Why Linefox is cheaper, with the numbers

The two open-source agents are **snapshot agents**: every browser action
appends a page snapshot to the conversation, and every model call re-reads
the whole conversation. OpenClaw's context per call on these runs started at
about 7.8k tokens and ended at 58k to 103k; the heavy tasks took 43 to 97
calls. Nearly all of that is cache reads, which are cheap per token, but
2 to 6 million of them per run is $0.40 to $1.20. Cost per step grows with
the step count, so cost per task grows roughly with the square of the steps.

Linefox sends the model **one message per step**: the task memory, the last
few actions, and the current page as a numbered element list. Nothing from
earlier pages is re-sent. On these runs that is 4k to 35k uncached input
tokens per step, flat across the run, plus about 13.5k cache reads for the
fixed system prompt. The heavy tasks took 9 to 18 steps. Cost per step is
constant, so cost per task grows linearly with the steps. The per-step series
for every Linefox run is in `raw/linefox/<task>-<trial>/usage.json`.

The crossover is around five steps. Below it a snapshot agent is cheaper,
because its first few calls are tiny and Linefox pays full price for a page
element list on every step. That is exactly the Autotrader row: OpenClaw
built a search URL with all the filters in it and finished in two to four
calls, 29k tokens, $0.03; Linefox took three to seven steps and paid for
three page reads, $0.16. Above five steps the curves separate, and the
tasks people actually hand to a browser agent are mostly above it.

The same mechanism is why Linefox is faster: fewer, smaller calls, and no
screenshot or snapshot round trips. In the September version of this
comparison (short tasks, median nine steps) the arms traded wins on dollars;
the task set here is harder and that is where the difference shows.

## What the tasks are, and why they need a browser

`tasks.json` has the exact prompts. Each one depends on state a URL cannot
carry, or on a site that refuses scripts: a delivery ZIP set in a popover, a
multi-step pricing configurator with a share link as the receipt, an ad
details modal on a site that returns 403 to scripts, a canvas map with an
open-now filter, a dealer site behind Akamai, and a guest cart with a
quantity change. No task needs a login. A plain `curl` with a browser user
agent was tried on each site first and cannot do any of them.

Linefox's prompt says "save to memory ... then complete", which is its own
protocol; the CLI arms get the same text with "report ..." instead.

## How the arms were run

- **Linefox**: Pro tier, cloud VM with Chrome and the NVDA element list, driven
  from an internal batch page. Raw per-run status (final answer, memory) and
  per-call usage in `raw/linefox/`.
- **OpenClaw**: `tools/run_cli_bench.py --tool openclaw --trials 2` with
  `tools/openclaw-bench.json5` (model pinned, managed headless Chrome,
  web_fetch allowed, utility model off). Raw final answers in `raw/openclaw/`.
- **Claude Code + Playwright**: `tools/run_cli_bench.py --tool claude-browser --trials 2`,
  which runs `claude -p` with `tools/playwright-mcp.json` (headless, isolated
  profile). Raw final answers in `raw/claude-code-playwright/`.
- Competitor runs 12 October 2026, Linefox runs 13 October 2026, same tasks.

## Caveats, stated plainly

- Two trials per arm per task. Enough to see the gap, not enough for tight
  error bars.
- Two Linefox trial-1 runs are **excluded** from the averages and marked in
  `results.csv`: the VM's Chrome still had the page from a previous batch open,
  so those runs never navigated and finished in 36 s and 18 s. Only trial 2
  counts for `amazon-zip-delivery` and `aws-calculator-share`. The raw rows are
  kept in `raw/linefox/` so you can see them.
- Correctness was checked by cross-arm agreement in the same hour: the AWS
  total ($126.60), the three Facebook Ad Library IDs and the cart subtotal
  ($190.99) match on every arm. The three rows where the arms disagreed (an
  Autotrader price, the Google Maps five, and which platforms Linefox read for
  the ads) were checked by hand against the live pages on 13 October 2026; the
  disagreements are noted in `results.csv`. Speed and cost do not depend on
  them.
- Playwright's headless Chrome was stopped by Akamai on Autotrader both times.
  That is a browser-fingerprint difference, not a model one.
- No arm can read a Google Maps price level without being signed in.

## Rerunning

Set `ANTHROPIC_API_KEY` (or the macOS Keychain item the runner reads), put
Node 24 on the path for OpenClaw, then run the two commands above from
`tools/`. The runner appends one CSV row per run and writes raw outputs next
to it. The Linefox arm needs a Linefox account; the batch page it uses is not
in this repo.
