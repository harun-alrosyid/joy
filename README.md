# Joy

A terminal coding assistant built with Python, aisuite, prompt_toolkit, and Rich.
Chat with a model using streaming replies, Markdown, and conversation history.
The OpenAI SDK handles requests; aisuite defines and executes the four tools.

Joy speaks two OpenAI wire formats and can point at any OpenAI-compatible
gateway: the **Responses API** (`/v1/responses`, OpenAI's default with medium
reasoning preserved between tool calls) or the **Chat Completions API**
(`/v1/chat/completions`, supported by virtually every provider, including
[9Router](https://9router.com)). Pick one with `JOY_API`.

Includes four tools: **read** files, **write** files, **edit** text, and **bash**
to run commands and tests. Tool activity appears while replies stream.

## Run

Requires Python 3.10–3.13 and an API key.

```sh
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
export OPENAI_API_KEY="your-api-key"
joy
```

Set the key in your shell; `.env` files are not loaded automatically.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `JOY_API` | `responses` | `responses` or `chat` (aliases like `chat_completions` work) |
| `JOY_MODEL` | `gpt-5.6-luna` | Model name sent to the provider |
| `JOY_API_KEY` | `$OPENAI_API_KEY` | API key |
| `JOY_BASE_URL` | `$OPENAI_BASE_URL` | Provider base URL; omit for OpenAI |
| `JOY_TIMEOUT` | `300` | Per-read timeout in seconds; raise it for slow reasoning models |

`JOY_*` variables win over their `OPENAI_*` fallbacks, so an existing OpenAI
setup keeps working unchanged.

### 9Router

9Router exposes an OpenAI-compatible endpoint. Start it, copy a key from its
dashboard, then point Joy at the Chat Completions API:

```sh
export JOY_API=chat
export JOY_BASE_URL="http://localhost:20128/v1"
export JOY_API_KEY="your-9router-key"
export JOY_MODEL="cc/claude-opus-4-7"   # any model enabled in the dashboard
joy
```

Use `JOY_API=responses` instead if you want 9Router's Responses passthrough with
reasoning-capable OpenAI models. The Chat Completions API is the most portable.

Some routed models are *thinking* models that stay silent for tens of seconds
before the first token, and can briefly return `503` when their upstream is busy.
Raise `JOY_TIMEOUT` (for example `export JOY_TIMEOUT=600`) if you hit timeouts,
and retry or switch `JOY_MODEL` on `503`.

## Usage

- **Enter** — send a message once the current reply finishes.
- **Alt+Enter** — insert a newline.
- **Esc** — interrupt the pending reply.
- **Ctrl+D** on empty input or **/exit** — exit.

You can draft your next message while a reply streams. Conversation history lasts
until you exit. Cancelling stops further tool calls; completed file changes remain.
The API request may still finish in the background.

File tools operate inside the directory where you launch Joy. Bash runs with your
user permissions and a default 30-second timeout; it is not sandboxed.

Edit uses a unique text replacement with [Pi-style matching](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/tools/edit-diff.ts):
exact text first, then Unicode punctuation and trailing-whitespace normalization.
It preserves BOM/newline style, rejects ambiguous or unchanged replacements, and
returns a diff. Joy accepts one replacement per call; Pi also supports batches.

## Coding tools

All file paths resolve inside Joy's launch directory. Path traversal, invalid
paths, missing files, and directory targets are rejected. Symlinks are resolved
before the workspace-boundary check. File content limits use encoded UTF-8 bytes:
`MAX_FILE_SIZE = 2 * 1024 * 1024` (2 MiB).

### `read`

Reads UTF-8 text with preserved, 1-based line numbers. `start_line` and
`end_line` are optional and inclusive. Compatibility aliases `offset` (1-based)
and `limit` (1–2,000 lines) are also accepted.

```json
{"path":"src/main.py","start_line":1,"end_line":100}
```

Ranges outside the file, invalid encodings, files over 2 MiB, and directories
are rejected. Empty files are handled without modification. A `Read failed`
activity message includes the cause when available: check that the path exists
inside Joy's launch directory, names a readable UTF-8 file, is at most 2 MiB,
and that the requested range is valid.
Tool responses are capped at 20,000 characters; request a smaller line range
when reading a long file.

### `write`

Creates or overwrites a UTF-8 file, creating missing parent directories.

```json
{"path":"src/example.py","content":"print('Hello Joy')\n"}
```

Writes use a temporary file in the destination directory, flush and `fsync`,
then atomic `os.replace`. Existing permissions are preserved. Failed or
cancelled writes clean up temporary files and never leave partial targets.

### `edit`

Replaces exactly one unique text section in an existing file.

```json
{"path":"src/main.py","old_text":"print('Hello')","new_text":"print('Hello from Joy')"}
```

Missing files, empty or unmatched text, ambiguous matches, unchanged results,
and oversized results are rejected. BOMs, newline conventions, permissions,
and concurrent-modification safety are preserved. Changes use the same atomic
replacement path as `write`.

### `bash`

Runs a command with `bash -c` in Joy's launch directory using the current user's
permissions. It is not sandboxed. The default timeout is 30 seconds; callers
may provide a timeout from 1 to 300 seconds.

```json
{"command":"python -m unittest discover -s tests","timeout":30}
```

Stdout and stderr are captured separately, output is bounded, and the result
includes status, exit code, and duration. Nonzero exits are failures. Timeout
or cancellation terminates the complete process group to avoid orphaned
children.

## Changes viewer

Successful `write` and `edit` operations produce structured file-change records
with operation, path, addition/deletion counts, and complete Git-style unified
diffs. At completion, the terminal displays one combined changes viewer with a
compact preview for every changed file and one global `Show details` / `Hide
details` control. All diffs start collapsed; expanding shows every hunk for all
files. Addition counts and lines are green; deletion counts and lines are red.
Hunk headers are cyan/blue. The combined addition total is green and deletion
total red.

In an interactive terminal, Joy displays a collapsed diff after each `write`
or `edit` is committed. Expand it to inspect the changes.

Expanded diffs show every unified-diff hunk and can be scrolled. In an
interactive terminal:

- `F4` focuses Activity and the File Changes viewer.
- `Up` / `Down` moves through the changes.
- `Enter` or `Space` toggles details for all changed files.
- `Right` expands all diffs; `Left` collapses them.
- `PageUp` / `PageDown` scrolls long expanded diffs.
- `Esc` returns focus to chat input, or interrupts an active turn.
- `Alt+M` toggles mouse controls. Turn them off to select/copy terminal text.

Chat controls remain unchanged: Enter submits, Alt+Enter inserts a newline,
Esc interrupts an active request, and Ctrl+D or `/exit` exits. When the
diff viewer has focus, the first Esc returns focus to chat input.

Joy shows elapsed working time and local finish time beneath the result. The
Type `/keymap` to print shortcuts.

Cancellation is checked before each tool and during long operations. Cancelled
writes clean up temporary files; completed atomic changes remain committed.
Subprocesses are terminated by process group. A later agent turn receives no
stale output from a cancelled turn.

## Test

```sh
python -m unittest discover -s tests
```

Tests run without an API key or network access.
