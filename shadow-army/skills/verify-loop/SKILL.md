---
name: verify-loop
description: Build and independently verify a Kanban card before closing it.
---

1. Read army_gate(card, board). If UNKNOWN, follow its reason: restore the worktree, make
   a first commit, or commit or ignore the files it names, then retry. Give code cards a Git workspace.
   UNGATED and UNARMED cards close normally. A done or archived card needs no further review.
   After a refused close, show the user the gate by writing `::shadow-army-gate{card="CARD_ID"}`
   on its own paragraph, with CARD_ID replaced by the actual card id.
2. Delegate building with duty="build" and card set to the Kanban card id.
3. When the build finishes, delegate a clean task with duty="verify" and the same card.
   One verifier may be in flight per card. Wait for its result before starting another.
4. The verifier inspects the actual code, runs the exact owner-configured check with
   Hermes terminal (set workdir explicitly to the repository and wait for completion),
   then calls army_verdict(card, verdict, findings). Findings contain file, line and text.
5. If FAIL, read the findings, delegate duty="repair", then request a new verifier.
6. Read army_gate again. On an armed code card, only a fresh PASS permits kanban_complete.
   Any edit, check-definition change or expired PASS requires new verification.

Unknown or builder-family routes refuse verification. Do not work around a refusal.
This gates the completion tool only. Manual edits and shell/database changes bypass it.
Proxies may obscure family identity. The tree check and completion are not atomic.

To verify this workspace with Shadow Army, delegate duty="verify" with no card.
The child reviews the session folder first, then the terminal scope folder, then the optional settings repository; no process-directory fallback is used. It reviews that Git directory and calls army_verdict without a card.
Workspace receipts appear in the pane, become STALE on edits, and block no completion tool.
