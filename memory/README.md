# Project Memory — Bugs, Findings & Gotchas

Durable, in-repo knowledge base so we don't rediscover the same bugs. This is committed with
the code (unlike per-developer agent memory), so it's the shared source of truth for
"we already hit this — here's what it was and how to avoid it."

## Files

| File | What it holds |
|------|---------------|
| [`known-issues.md`](./known-issues.md) | The main ledger: every real bug/gotcha + root cause + fix + how to avoid, grouped by area (desktop app, distributed workers, local dev/deploy, general dev findings). |
| [`feedback-ui-mistakes.md`](./feedback-ui-mistakes.md) | UI-specific mistakes (layout/routing/tokens) in the `Why / How to apply` format. |

## Entry format (for `known-issues.md`)

Each entry is:

```
### <Area><n>. <Short symptom-oriented title>
**Symptom:** what you actually observe (log line, error string, UI state)
**Root cause:** the real reason (not the symptom)
**Fix:** what resolved it (files/commands where useful)
**How to avoid/detect:** the rule + a fast way to confirm it next time
```

## How to use

- **Before debugging** a worker/desktop/dispatch issue, skim `known-issues.md` — most
  head-scratchers here have already been paid for once.
- **After fixing** anything non-obvious, add an entry at the top of the relevant section.
  Never delete history; strike through and note if an entry is superseded.
- Keep it accurate: only document what was actually verified. Link to code paths, PRDs
  (`docs/prd/`), and ops runbooks (`docs/ops/`) rather than duplicating them.

## Areas covered in `known-issues.md`

- **A. Desktop worker app** — Tauri 2 bridge (withGlobalTauri + capabilities), PyInstaller
  freeze pitfalls (self-re-exec, sidecar embed, codesign), launch-crash triage.
- **B. Distributed workers** — fleet dispatch, capability declaration/matching, the job
  lifecycle errors (`no capable worker`, `already running`, `campaign_not_found`,
  `soul_missing`), and why a `done` run can still return 0 leads.
- **C. Local dev / deployment wiring** — bootstrap tokens, stale servers, keychain tokens,
  shared DB.
- **D. General findings** — untrusted-text parsing discipline, OpenRouter model churn, CDP
  live-run gotchas (port 9333), schema-migration versioning, SQLite leasing.
- **Cross-machine deployment gaps still OPEN.**
