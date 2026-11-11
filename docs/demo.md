# Shadow Army demo: shot lists

Two separate films. Version B is the built-in trial plus a seeded board; it can be recorded today. Version A is a real run with two live models, for when one is recorded. Never cut footage from one into the other, and never label trial footage as a live run.

## Rules for both

- Show the plugin working. No settings-form tour, no near-black frame, no click circle or cursor highlight over buttons.
- Full 16:9 with no side bars. Size the Hermes Desktop window to 16:9 (for example 1600 by 900, captured at 2x) and export 1920 by 1080. The Shadow Army header must be in frame in the first shot.
- Captions are short, one per shot, held for the whole shot, readable without sound, and placed over empty canvas, never over text.
- A caption never says a model did something a simulated actor did. In Version B the actors are "the builder" and "the reviewer", and the persistent label says they are simulated.
- Use a disposable Hermes home and board. Keep private paths, hostnames, addresses and tokens out of frame. Wait for each animation to settle before the next press.
- The end card's install line is the one that works today: `hermes plugins install SilentKnight87/shadow-army#shadow-army --enable`. Show `hermes plugins install shadow-army --enable` only after the catalog lists the entry.

## Version B: the trial and a seeded board (78 seconds)

Persistent label, small, bottom right, on every trial shot: `Trial · models simulated · gate, tests and diff real`. On the board shots it reads `Synthetic board · no model ran · fingerprint real`. It never disappears.

Before recording: two takes of the trial in sandbox A. Take 1 runs steps 1 to 8 without tamper. Take 2 presses **Tamper one line** at step 7, then **Next** once (close refused), then **Next** twice to step 10 (closed). Sandbox B holds an armed synthetic board seeded with no model: a few code cards with real check runs, one PASS card whose workspace you will edit, Kanban enabled, and a terminal window sized to fit inside the 16:9 frame.

| Time | Shot | Caption |
| --- | --- | --- |
| 0:00 to 0:02 | Take 1, step 2, full window with header. The check run reads `Ran 2 tests ... OK`, `exit 0`; the red `- self.assertEqual(total([]), 0, ...)` line at `test_cart.py:9` sits right under it. | Green tests. |
| 0:02 to 0:05 | Hard cut to step 5 as the FAIL seal stamps and the `FAIL test_cart.py:9` tag lands on the same run. | An assertion is gone. Still FAIL. |
| 0:05 to 0:08 | Title over the first-run hero view (bright, not dimmed). | Shadow Army. No model grades its own homework. |
| 0:08 to 0:14 | Play the trial, step 1. The real empty-cart test fails, exit 1. | A real test fails: an empty cart crashes. |
| 0:14 to 0:23 | Next, step 2. The builder deletes the assertion. Exit 0, and the plugin's red removed line appears with "1 assertion removed since the card started". | The builder deletes the assertion. The plugin sees the missing line. |
| 0:23 to 0:27 | Next, step 3. GATE REFUSED CLOSE, "No receipt for this tree". | Hermes tries to close. Refused. |
| 0:27 to 0:31 | Next, step 4. The same-family reviewer row turns REFUSED. | Same family as the builder? Refused. |
| 0:31 to 0:41 | Next, step 5. The verifier rises from the other side; hold on BLOCKED, "The tests pass. The reviewer still failed it.", exit 0 beside the red line. | The tests pass. The reviewer fails it at the line the plugin found. |
| 0:41 to 0:46 | Next, step 6. Assertion restored, `cart.py` fixed, exit 0, nothing removed. | Test restored. Code fixed. |
| 0:46 to 0:51 | Next, step 7. The PASS seal stamps. | A fresh review of this exact code: PASS. |
| 0:51 to 0:57 | Take 2: Tamper one line. The seal cracks to STALE; Next, close refused. One beat, no repeat. | One line changes. The PASS is void. Close refused. |
| 0:57 to 1:00 | Take 2, step 10, CARD CLOSED with its reviewed PASS. Skip step 9. | Reviewed again, it closes. |
| 1:00 to 1:09 | Sandbox B, Kanban view with the PASS card's badge and the small terminal. Append one line to a tracked file in that card's workspace; wait, without pressing anything, until the badge turns STALE. | A file changes on disk. The badge goes STALE on its own. |
| 1:09 to 1:14 | Sandbox B, the armed board in the Shadow Army pane, seals at rest. | Armed: every code card waits for the other family. |
| 1:14 to 1:18 | End card over the bright hero art: SHADOW ARMY, the tagline, and the install line. | `hermes plugins install SilentKnight87/shadow-army#shadow-army --enable` |

Total 78 seconds. If a beat runs long, trim the step 1 and step 6 holds first; never trim the cold open or the board beat.

## Version A: a real two-model run (about 80 seconds)

Record only after a full real run succeeds; a real reviewer may respond differently from the trial, and nothing scripted may stand in for what it does. Follow [Prove the live model hop](../shadow-army/REFERENCE.md#prove-the-live-model-hop) in a disposable profile. Use a small Git repository with an empty-cart style bug and a check whose test covers it. Arm the gate with a builder and a verifier from two different known families, both already available in that profile. Split the 16:9 frame: Hermes chat on the left, the Shadow Army pane on the right, header visible.

Captions may name the real models only where the receipt shows the actual response model. The persistent label reads `Live run · real models · real gate`.

| Time | Beat | Caption |
| --- | --- | --- |
| 0:00 to 0:05 | Cold open: the real check output `OK`, exit 0, the red removed line, and the real verifier's FAIL on it. | Green tests. A deleted assertion. FAIL. |
| 0:05 to 0:08 | Title over the pane. | Shadow Army. No model grades its own homework. |
| 0:08 to 0:20 | The builder works the card. It deletes the failing assertion; the check goes green; the pane shows the removed line. | The builder makes the tests pass by deleting one. |
| 0:20 to 0:28 | In chat, the builder calls `kanban_complete`; the refusal arrives in the conversation; the pane reads UNVERIFIED. | It says done. The close is refused in chat. |
| 0:28 to 0:45 | The verifier child runs on the other family's model, runs the check (exit 0) and records FAIL at the removed line. Show the child's model in the receipt. | The other family runs the tests, then fails it at the removed line. |
| 0:45 to 0:58 | Repair: the builder restores the assertion and fixes the code; a fresh review records PASS. | Restored, fixed, reviewed again: PASS. |
| 0:58 to 1:06 | `kanban_complete` succeeds; the card moves to Done with its receipt. | Only reviewed code closes. |
| 1:06 to 1:12 | Copy receipt: the Markdown block with the tree, models, check, review time and tokens. | The receipt, ready for a pull request. |
| 1:12 to 1:20 | End card with the install line. | `hermes plugins install SilentKnight87/shadow-army#shadow-army --enable` |

Do not claim the live run succeeded unless the receipt names the actual response models and the recording shows the check and the close. If the real verifier misses the removed assertion, record what happened and do not publish this version as the catch.
