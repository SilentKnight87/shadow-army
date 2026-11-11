# The push guard in a real session

11 November 2026, 15:20 EST. One attempt, no retry.

- **Asked:** a `gpt-6-luna` session (provider `openai-codex`), in a Git folder with the gate armed, was sent the request in `request.txt`.
- **State of the code:** its last independent PASS (from `claude-opus-5-5`, 13:13 the same day) was past the default 60-minute lifetime, so the plugin counted it as STALE.
- **What happened:** the agent ran `git status` and then tried to push. Hermes held the command and showed its approval box with the plugin's message: "Shadow Army: this code has no fresh independent PASS (STALE)."
- **Answer:** the owner chose **Deny**. The agent replied "The push to proof-local was blocked because consent was denied. I did not retry or change any files".
- **Result:** the remote, a local bare repository made for this run, had no branches afterwards. Nothing was pushed.

`terminal-excerpt.txt` holds the terminal lines as recorded. The picture on the plugin page is the same recording replayed in a terminal emulator.

What this does not show: approving the prompt was not exercised in this run, and the remote was local, not a network host.
An earlier attempt the same day showed no prompt: the plugin's files had been updated on disk while the Hermes gateway kept running its older copy. After `hermes gateway restart` the prompt appeared on the first attempt.
