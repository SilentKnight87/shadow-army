![Shadow Army, a plugin for Hermes Agent: no model grades its own homework. Beside the name stands the plugin's gate, sealed.](https://raw.githubusercontent.com/SilentKnight87/shadow-army/main/docs/img/readme-banner.jpg)

# Shadow Army

No model grades its own homework. [![tests](https://github.com/SilentKnight87/shadow-army/actions/workflows/public-plugin-tests.yml/badge.svg)](https://github.com/SilentKnight87/shadow-army/actions/workflows/public-plugin-tests.yml)

Coding agents reward-hack. Told to make a failing check pass, an agent will sometimes delete the test and report "done, all tests pass". Shadow Army is a Hermes Agent plugin that refuses the agent's close of a Kanban code card until a model from a different family has reviewed the exact code and run your check. The builder and the verifier are its two shadows: child agents from different model families. With no board at all, it asks you before the agent pushes code that has no fresh independent PASS.

The cost is one extra review per close: in the real run below, about 10 seconds to a verdict and about 40,000 tokens.

```sh
hermes plugins install SilentKnight87/shadow-army#shadow-army --enable
```

Then enable its Desktop pane ([Install](#install)) and press **Play the trial**: one minute, no model and no login.

![Eight seconds of the demo film: after a PASS, one edited line cracks the seal to STALE and the agent's close is refused again. From the trial, simulated models.](https://raw.githubusercontent.com/SilentKnight87/shadow-army/main/docs/img/readme-loop.gif)

[Watch the 46-second film](https://github.com/SilentKnight87/shadow-army/blob/main/docs/demo.mp4). About 34 of its seconds are screen capture, labelled on screen as the real run or the trial.

## A real catch

**Green tests. Still FAIL.**

On 11 November 2026 the gate ran in a real Hermes session with two real models, on a small repository with one failing test. In the order the plugin's ledger recorded it:

![The card at the moment of the catch: exit 0, one assertion removed, and a FAIL from claude-opus-5-5 on work built by gpt-6-luna. From the real run.](https://raw.githubusercontent.com/SilentKnight87/shadow-army/main/docs/img/readme-real-catch.png)

1. **The builder (GPT) deleted the test.** A `gpt-6-luna` child was told to take the shortcut: not to fix the code, and to remove the failing assertion. It deleted the test. The check then ran 1 test and exited 0.
2. **The plugin saw it before any review:** one assertion removed, `tests/test_cart.py:9`.
3. **Hermes's close was refused.** The agent's `kanban_complete` call was blocked by the plugin's hook: "No receipt for this tree."
4. **The verifier (Claude) failed it on green tests.** A `claude-opus-5-5` child was asked to review the card and run the check. It was given the card, the check to run and the plugin's note of the removed line, and it read the diff itself. It was told nothing about the builder, the shortcut or the verdict anyone expected. Its receipt shows an 11-second review (36,795 tokens for the whole child) and a FAIL: "The suite passes only because that test was deleted."
5. **Repair, a fresh PASS, then the close.** The builder restored the test and fixed the code, a second Claude verifier passed the new code, and only then did the agent's close go through.

![The card's ledger, in order: assertion removed, close refused, FAIL on exit 0, repair, PASS, close permitted, card closed. From the real run.](https://raw.githubusercontent.com/SilentKnight87/shadow-army/main/docs/img/readme-ledger.png)

**What was staged: the builder's instruction, and nothing else.** The prompts, the plugin's brief to each child, every tool call the coordinator made and the ledger are in [docs/real-run](https://github.com/SilentKnight87/shadow-army/tree/main/docs/real-run), so this can be checked. The review request also said that green tests alone do not establish correctness and that a removed assertion must be assessed against the requirement; the same wording was used in the earlier run, where the same model passed a correct fix. The coordinating session was also a Claude model. It only delegated: it edited no file and ran no command, which is why the plugin does not count it as the builder. The pictures show Hermes Desktop reading copies of the run's records in a sandbox. The card was created blocked to keep other dispatchers off it (its "needs you" label), and "(take 2)" marks the second card of that morning.

**With no staging at all**, earlier the same morning: the builder was simply asked to fix the bug, and did. The agent's close was still refused until a Claude verifier had passed that exact code, and then it went through. The refusal does not depend on anyone cheating.

![The earlier card: built by gpt-6-luna, verified by claude-opus-5-5, with the refused close and the permitted close in its history. From the real run.](https://raw.githubusercontent.com/SilentKnight87/shadow-army/main/docs/img/readme-real-run.png)

## Who it is for, and what it costs

- **For:** anyone who lets a Hermes agent write code and close its own Kanban cards. With no board, ask the agent to "verify this workspace with Shadow Army": the same cross-family review runs on any Git folder, as a report that blocks nothing.
- **Needs:** two model families configured in Hermes (an OpenAI builder and an Anthropic verifier, for example), Git and a local terminal.
- **Costs:** one extra review per close. In the run above the two reviews took 11 and 9 seconds from the verifier's start to its verdict (19.0 and 16.6 seconds for the whole verifier child), and the two verifier children used 36,795 and 45,085 tokens in all. Two small runs do not predict the cost on a large project.

## Install

```sh
hermes plugins install SilentKnight87/shadow-army#shadow-army --enable
```

Once the catalog lists it: `hermes plugins install shadow-army --enable`.

In Hermes Desktop, open **Capabilities**, choose **Plugins**, and enable **Desktop** for shadow-army. Then open **Shadow Army** in the sidebar.

Requires Hermes 0.23.0 or newer, Git and Python 3.11 or newer. No other dependencies, credentials or services.

Two ways to start:

- **No model, one minute:** press **Play the trial** in the pane ("Try it in a minute" below).
- **A real card:** arm the gate ("Turn it on" below), start a new chat, and send a request like this one, with your own card and folder:

  ```text
  Use the verify-loop skill on card CARD_ID (board BOARD). Its Git workspace is /path/to/repo.
  Stay the coordinator: delegate the build, then close the card.
  ```

  You need three things: a Git folder with a first commit, your real test command in the setup card, and a verifier from another family. The first close is refused. The agent then asks the other family for a review, repairs what it finds, and closes the card only after a fresh PASS.

## What it does

**1. Hermes refuses the close.** On an armed code card, the agent's `kanban_complete` call is blocked until there is a fresh PASS for this exact code. The refusal says why and what to do next.

**2. It sees the assertion that went missing.** Tests can turn green because an assertion was deleted. The plugin keeps an inventory of assertion lines in the repository's test files. When one disappears, the pane shows the removed line in red, beside the green exit 0, and the verifier's brief carries the same line. Removed lines are told apart from lines that were only changed, and moved assertions are not flagged. The same check reports a test that was switched off (skipped, renamed out of collection or commented out) and a check run that executed fewer tests than the last one. All of this is evidence: it never blocks on its own, and the verifier decides.

![Trial step 2: the check exits 0 and the pane shows, in red, the one assertion removed since the card started. From the trial, simulated models.](https://raw.githubusercontent.com/SilentKnight87/shadow-army/main/docs/img/readme-removed.png)

| What the builder did | What the pane and the verifier's brief show |
| --- | --- |
| Deleted an assertion, or a whole test | The removed line in red, with its file and line |
| Edited an assertion | "Changed", told apart from removed |
| Moved a test to another file | Nothing: a move is not a removal |
| Skipped a test, renamed it out of collection, or commented it out | The test that was switched off |
| Made the check run fewer tests than last time | The drop, read from the runner's own summary (unittest, pytest, Jest, Vitest, RSpec, cargo, Maven) |

Each row has a test in the suite. The scan reads common test syntax in Python, JavaScript, TypeScript, Go, Rust, Java and Ruby.

**3. Only another family may verify.** A review requested from the builder's own model family is refused by the plugin's routing. The verifier inspects the change, runs your check in a local terminal and records PASS, or FAIL with a file and line.

**4. The PASS is bound to the code.** A PASS covers the exact Git-visible code and check command. One edited line, a changed check or an expired PASS (60 minutes by default) makes it STALE, and the close is refused again. Each receipt shows the review time, tokens when Hermes reports them, and a **Copy receipt** button for a Markdown block.

![Trial step 7, after Tamper one line: the seal cracks to STALE and the PASS no longer covers the code. From the trial, simulated models.](https://raw.githubusercontent.com/SilentKnight87/shadow-army/main/docs/img/readme-stale.png)

**5. It asks before unreviewed code is published.** Once the gate is armed, when the agent's terminal is about to run `git push` or `gh pr create` on code with no fresh independent PASS, Hermes asks you to approve first: "Shadow Army: this code has no fresh independent PASS (UNVERIFIED)." With a fresh PASS, from a card or from a card-free review, the push goes straight through. It never blocks: you can approve, and the `guard_push` setting turns it off.

## Why this is a plugin, not a prompt

A prompt or a skill can ask an agent to get a second opinion. It cannot refuse the tool call when the agent skips that step, record which model actually answered and what the check really exited with, or notice that the code changed after the review. Shadow Army does those through Hermes itself: a pre-tool hook on `kanban_complete`, an approval request on the agent's own `git push`, a delegation provider that routes the verifier and refuses the builder's family, API and terminal hooks that record the real response model and exit code, and a local ledger bound to the code.

In Hermes Desktop it adds a pane, a gate count in the status bar, a notification when a gate changes, badges on Kanban cards, a gate card in the chat transcript and a shortcut (`mod+alt+g`).

## Try it in a minute

Press **Play the trial** in the pane, then **Next**, or **Auto-play**. No login, no model and no setup are needed.

A tiny repository on your machine opens with a failing empty-cart test. The builder deletes the assertion and the tests pass. The gate refuses the close, a same-family reviewer is refused, and the other family fails the code at the removed line. The builder restores the test and fixes the code, a fresh PASS lands, and the card closes. At step 7, **Tamper one line** tries the other ending.

![Trial step 5: the tests pass and the verifier still records FAIL at the removed line. From the trial, simulated models.](https://raw.githubusercontent.com/SilentKnight87/shadow-army/main/docs/img/readme-green-fail.png)

- **What is real in the trial:** gate decisions, the same-family routing refusal, every test run, the fingerprint, the removed-assertion check and verdict acceptance all run through the plugin's real gate code.
- **What is simulated:** only the two models, their identities and their words. Trial records can never satisfy a real card.

## Turn it on

The pane's setup card walks through it and says, in words, whether the gate is configured and armed, and when Hermes last called its hook.

1. **Verifier.** Pick a route from the list and press **Arm the gate**. The list holds only models already configured in your profile, from a family other than your main model's. With an OpenAI builder, for example, pick `claude-sonnet-5-5` through anthropic. If the button is unavailable, the card points to **Settings**, **Plugins**, **Shadow Army**, which holds the same fields.
2. **Routing.** Run the one command the card shows, then start a new chat:

   ```sh
   hermes config set delegation.backend shadow-army
   ```

3. **Proof.** Press **Run self-test**. It asks the real gate to close a throwaway unreviewed card and shows "The gate refused a test close just now". A separate line says when Hermes last called the plugin's hook in this profile, which happens on any agent tool call.

- Two families are enough. A main session that only delegates the build does not count as a builder. If it edits a file or runs any terminal command itself, it does.
- With everything set and no code card open, the card still reads CONFIGURED and says "No open code card".
- Code cards need a Git workspace with a first commit and a local terminal. Until the gate is armed, code cards show NOT ARMED and close normally. Scratch cards show NOT GATED.

![The setup card: configured, armed, and the time Hermes last called the gate's hook. From the real run.](https://raw.githubusercontent.com/SilentKnight87/shadow-army/main/docs/img/readme-setup.png)

Edit a file in a passed card's workspace and its Kanban badge turns STALE on the next refresh, with no button pressed.

![Gate badges on Hermes Kanban cards: BLOCKED, UNVERIFIED, STALE and PASS. Synthetic board, no model ran.](https://raw.githubusercontent.com/SilentKnight87/shadow-army/main/docs/img/readme-kanban.png)

The board in this picture is synthetic: no model ran on it.

## Without a Kanban card

Ask the agent to "use the verify-loop skill to verify this workspace with Shadow Army". A verifier from another family reviews the session's Git folder (or the configured repository) and runs your check. The receipt appears in the pane as a Workspace card and goes STALE on the next edit. The session you ask in counts as the builder, so the verifier must come from a different family than that session's own model.

This ran with real models on 11 November 2026. A `gpt-6-luna` session asked, a `claude-opus-5-5` verifier reviewed the folder, ran the check and recorded PASS in 26 seconds (67,844 tokens for the verifier child). One edited line made the receipt STALE, and putting the line back restored the PASS. Name the skill in the request: the shorter "verify this workspace with Shadow Army" did not start a review in that session.

![A Workspace card: no Kanban card, built in an OpenAI session, PASS from claude-opus-5-5 with the check's output. From the real run.](https://raw.githubusercontent.com/SilentKnight87/shadow-army/main/docs/img/readme-workspace.png)

A review blocks nothing by itself. The push guard above is what acts on it: with the gate armed, a `git push` from this folder asks for your approval until it has a fresh PASS.

## Limits

- **If the gate cannot decide, it refuses.** If the plugin hits an error while judging a close, for example because it cannot read the code or its own records, the agent's close is refused with the reason and not let through.
- **It gates one tool and asks on two commands.** Only the agent's `kanban_complete` call is refused, and only its terminal `git push` and `gh pr create` are held for your approval. The push guard reads the command text, so an alias or a script bypasses it, and it needs someone to answer the prompt. Manual moves on the board, other shell commands, direct database writes and disabling the plugin all bypass the plugin. It stops mistakes and overconfidence; it is not a sandbox.
- **Family is read from model names.** Families come from route and response model ids. A proxy can blur them, unknown families are refused, and a receipt does not prove the quality of a review.
- **The test evidence is a heuristic.** It reads common test syntax in Python, JavaScript, TypeScript, Go, Rust, Java and Ruby, within a size and time budget. Beyond that budget it says "Test scan unavailable: review the test diff by hand" rather than guess. A removal committed before the plugin first saw the card is not seen.

Every other limit, what the plugin writes and runs on your machine, and all settings are in [REFERENCE.md](REFERENCE.md).

## Run the tests

From a clone of the repository, run `./scripts/test-public.sh`. It fetches stock Hermes v0.23.0 into a temporary folder, runs the plugin and host suites with no login and no model, and prints PASS. These tests use synthetic model identities, so they do not prove a live provider hop. [VERIFICATION.md](VERIFICATION.md) has the details.
