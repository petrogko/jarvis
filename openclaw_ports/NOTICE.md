# OpenClaw Ports — Attribution

Modules in this directory are ported from OpenClaw
(https://github.com/openclaw/openclaw), MIT-licensed.

## Pinned upstream commit

`125d82cab2952f87f532106a368d54e526141026` (as of 2026-05-25)

## Per-port table

| Module             | Upstream path                                              | Ported at SHA                              | Last resync |
|--------------------|------------------------------------------------------------|--------------------------------------------|-------------|
| `tts_local_cli.py` | `extensions/tts-local-cli/speech-provider.ts`              | `125d82cab2952f87f532106a368d54e526141026` | 2026-05-25  |
| `gh_issues.py`     | `skills/gh-issues/SKILL.md`                                | `125d82cab2952f87f532106a368d54e526141026` | 2026-05-25  |

## Resync workflow

1. Look up the current ported SHA in this table.
2. `cd /Users/<user>/Development/github/openclaw && git diff <old_sha> HEAD -- <upstream-path>` — read the diff.
3. Forward-port changes by hand. Run the port's tests. Commit with `chore(openclaw_ports): resync <name> to <new_sha>`.
4. Update the table above with the new SHA and date.

## MIT License (verbatim from OpenClaw upstream)

MIT License

Copyright (c) 2026 OpenClaw Foundation

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
