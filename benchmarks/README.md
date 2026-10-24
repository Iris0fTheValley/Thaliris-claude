# Controller overhead benchmark

`controller_overhead.py` replays one realistic Controller management session against two adapter trees and reports
before/after numbers:

* OLD: Claude adapter 0.4.0 (commit `14ecc96`, vendored Core `eb540d0`). Extracted with `git archive` into a scratch
  directory, or pass `--old PATH` to use an existing checkout.
* NEW: this checkout (0.4.1, vendored Core `e29c837`).

Both are driven through the real launcher `bin/thaliris.py` and the real hook entry `bin/thaliris-hook` (via `sh`, with the
`--filter` flag `hooks/hooks.json` uses). A Controller runner call is first offered to the PreToolUse hook, which appends the
native identity, and the rewritten command is then executed, as Claude Code does. Child dispatches simulate the
PreToolUse / PostToolUse / SubagentStart / SubagentStop / UserPromptSubmit payloads the way `tests/test_e2e_lifecycle.py`
does. Nothing touches the installed plugin or `~/.thaliris`: every run uses temporary Git projects and a temporary
`THALIRIS_CLAUDE_HOME`.

## Run

```
python benchmarks/controller_overhead.py --out results.json --md report.md
python benchmarks/controller_overhead.py --repeat 3 --sensitivity     # medians of wall time; also OLD with inline --json
```

Python >= 3.11 and `sh` on `PATH` are required (`--hook-entry launcher` calls `bin/thaliris.py hook EVENT` directly).
A full run takes a few minutes per variant, because the hooks are real subprocesses. `PYTHONUTF8` is not set unless you pass
`--utf8` (a Host does not set it; 0.4.0 cannot read a Chinese Git path without it, 0.4.1 can).

## The workload

One task with 7 requirements. 18 child dispatches in 4 background waves of 4/5/4/5 launches (one Controller message each).
12 child observations, two siblings recording the same local id `O1`. 33 requirement judgments at 6 decision points (after
each wave, then two re-judgments of the repaired candidate `C2`). 8 task-context updates with 2-4 records each, 5 of 8 in
Chinese, four of them made while a wave is running. 6 explicit status reads (one before each decision point). `close` at the
end. The project is a Git repository whose path contains Chinese and a space; if a tree cannot admit the task there, that
is recorded as an error of that tree and the same workload is replayed in an ASCII path.

Each tree is operated the way its own guidance tells the Controller:

| operation | OLD (0.4.0) | NEW (0.4.1) |
| --- | --- | --- |
| contract | Write a JSON file, then `task-start --contract` | `task-start` with flags |
| register | inline `--json` | `--requirement R1=x ...` |
| task-update | Write a JSON file, then `--input` (or `--json` with `--old-update inline`) | flags |
| judge | one call per judgment (`--json`) | one `--judgment` batch per decision point |
| revision | not stated by acknowledgements after child activity | latest acknowledgement or "task revision now N" |
| conflict | policy A: `status`, then retry. Policy B: `status` before a write after child activity | one retry from `details.current_revision` |

What the Controller "knows" about the revision is derived from what it was actually shown (acknowledgements, `status`,
evidence-packet lines, error details), not from the benchmark's internal state.

## Metrics

Controller tool calls (runner + Write; Agent launches are identical and listed apart), sequential model-tool round trips (a
message of parallel launches counts once; dependent calls, including a Write followed by the call that reads the file, each
count), runner stdout bytes, model-visible hook `additionalContext` bytes (PostToolUse, SubagentStart, UserPromptSubmit,
SessionStart; PreToolUse command rewrites excluded), errors by code, mechanical wall time (sum of runner and hook subprocess
times), approximate tokens (bytes / 4, an undercount for CJK text).

The saved-time estimate multiplies saved round trips by the median model gap before a runner call (4.4 s) and saved runner
calls by the median Host/tool wait of a runner call (0.67 s), both measured in the audited real session. They are
estimates; the replay itself does not measure them.

## Correctness checks (run for every variant)

* all 12 observations are stored under their own handoff with the producer, text and requirement refs that were sent (two
  siblings both used `O1`);
* all 33 judgments and all task-context records read back with the fields that were sent;
* revisions acknowledged for successful writes strictly increase, and a deliberately stale write just before `close` is
  refused and changes nothing;
* in a separate project: `close` is refused while a dispatch is unresolved and succeeds after `dispose`.

## What this cannot measure

Real model latency and reasoning cost (only counted round trips), Host scheduling, permission prompts and parallel hook
execution (hook times are summed, not overlapped), prompt-cache effects of the larger or smaller tool results, and
how a real model actually phrases a call. The interleaving of children and Controller writes inside a wave is a fixed,
documented assumption (`EARLY_OBSERVERS`), not a measurement. Background children are modelled as completing through a
`UserPromptSubmit` notification turn per wave, not through a PostToolUse result.

## Reference results

One run on Windows 11, Python 3.11, `sh` hooks, `PYTHONUTF8` unset, median of 3 repetitions for wall time (counts and bytes are
deterministic). OLD = 0.4.0 `14ecc96`, NEW = 0.4.1 `22cd197` (the script is the only addition on top of it). OLD-A writes with the last
revision it saw and recovers with `status` on a conflict; OLD-B runs `status` before a write after child activity.

| metric | OLD-A | OLD-B | NEW | NEW vs OLD-A | NEW vs OLD-B |
|---|---:|---:|---:|---:|---:|
| Controller tool calls (runner + Write) | 66 | 64 | 26 | -61% | -59% |
| sequential model-tool round trips | 70 | 68 | 30 | -57% | -56% |
| runner stdout bytes (Controller) | 203,566 | 212,032 | 25,091 | -88% | -88% |
| hook additionalContext bytes | 29,835 | 29,835 | 25,484 | -15% | -15% |
| model-visible bytes (stdout + hook ctx) | 233,401 | 241,867 | 50,575 | -78% | -79% |
| approx tokens (bytes/4) | 58,350 | 60,466 | 12,643 | -78% | -79% |
| errors in the replayed run | 3 | 0 | 2 | | |
| mechanical wall s (runner + hooks, summed) | 77.1 | 79.8 | 51.5 | -33% | -36% |
| Chinese+space Git path admission | fails at `bootstrap` (`NATIVE_IDENTITY_UNKNOWN`) | same | ok | | |

Saved round trips x 4.4 s median model gap = about 170 s of model turnaround (estimate). One cost moves the other way: the
NEW `observe` acknowledgement a child reads is larger (3,557 B vs 1,236 B for the 12 child observations).
Run `--report-only results.json` to re-render a saved raw result.
