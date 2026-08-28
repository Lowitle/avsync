# Dev Notes / Session Checkpoints

This folder tracks the working history of AI-assisted development sessions on this
fork, so we never lose the thread between conversations (context resets, new
machines, new chat sessions, etc.).

## Convention

- One file per session (or per meaningful chunk of a long session):
  `YYYY-MM-DD-session-NN.md`
- Each checkpoint file should summarize, in this rough order:
  1. **Goal** of the session (what was asked).
  2. **Changes made** (files touched, and *why* — not just what).
  3. **Decisions/tradeoffs** and anything explicitly rejected or deferred.
  4. **Validation** performed (tests run, results observed).
  5. **Open items / next steps** carried over to the next session.
- Keep entries factual and concise — this is a working log, not marketing copy.
- Do not duplicate what's already in `README.md` (user-facing docs) or
  `OBJETIVOS_INMEDIATOS.txt` (the current priority backlog) — link to them
  instead of repeating their content.

## Index

- [2026-08-27 — Session 01](2026-08-27-session-01.md): Audio quality
  preservation pass (splice silence-vs-copy, decoupled loudnorm, auto
  codec/bitrate, level-matching gain), fork-vs-upstream diff analysis, README
  overhaul.
- [2026-08-28 — Session 02](2026-08-28-session-02.md): Anchor density/tail-
  desync fixes (find_partial_anchor run-based rewrite, tail jump detection,
  checkpoint-cache bug, offset-consistency relaxed acceptance, interior gap
  densification, anchor diagnostic CSV) found while batch-processing a real
  season.
