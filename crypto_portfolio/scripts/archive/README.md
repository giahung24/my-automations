# scripts/archive

One-off scripts that have already done their job. Kept for reference, not maintained; the
orchestrators hard-code paths of the machine they ran on and refer to `ROADMAP_LEDGER.md`,
which has since been split into `ROADMAP.md`, `docs/LEDGER.md` and `docs/SPEC.md`.

| script | what it did |
|---|---|
| `autonomous_orchestrator.py` | drove the Phase 1 tasks (P0, F1-F3) through an implement / test / review loop, 2026-11-25 |
| `wave2_3_orchestrator.py` | same loop for M6, M4, M7, 2026-11-25 |
| `backfill_manual_btc.py` | data correction run on 2026-11-26: counts the cold-wallet BTC as held since the first checkpoint |
