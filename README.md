# Shadow Army

No model grades its own homework.

![Shadow Army](docs/img/readme-banner.jpg)

Shadow Army is a Hermes Agent plugin. Once armed, the agent cannot close a Kanban code card until a verifier from a different model family has run the owner's check on the exact code. The plugin and its guide are in [shadow-army](shadow-army/README.md).

The `test_*.py` files, `host_probe.py` and `verify_archive.py` test the plugin. [shadow-army/VERIFICATION.md](shadow-army/VERIFICATION.md) gives the commands. [docs/demo.md](docs/demo.md) is the demo shot list.

**A real run:** on 11 November 2026 a `gpt-6-luna` builder's work was refused at close, verified by a `claude-opus-5-5` child and only then closed. The receipt is in [the plugin's guide](shadow-army/README.md#a-real-run).

**Demo film (62 seconds):** [docs/demo.mp4](docs/demo.mp4). The plugin and its checks in it are real; the built-in trial's two models are simulated, and the closing shot is the ledger of the real run.

**Install:** `hermes plugins install SilentKnight87/shadow-army#shadow-army --enable`

MIT licensed. See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md).
