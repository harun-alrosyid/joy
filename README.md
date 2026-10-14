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
- **Ctrl+C** — cancel input or the pending reply.
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

## Test

```sh
python -m unittest discover -s tests
```

Tests run without an API key or network access.
