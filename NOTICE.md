# Notice

ARISE is an independent implementation, not a fork. Its design was informed by
Firstmate by Kun Chen, https://github.com/kunchenguid/firstmate, inspected at
commit 8d2ee291107d14f37ca7ef280199bebed22578c4, licensed MIT.

The following Firstmate primitives informed ARISE's design:

- One-liaison contract (AGENTS.md)
- Per-worker git worktrees
- Visible session backend
- Event-driven watcher
- Restart-proof on-disk state
- Ship vs scout task shapes
- Per-project delivery modes
- Turn-end guard

ARISE adds the following:

- Gate-rank E..S model routing with multi-provider lanes
- Mandatory cross-family verification gate with evidence freshness
- Non-engineering shadows

Firstmate is distributed under the MIT License:

```
MIT License

Copyright (c) 2026 Kun Chen

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
