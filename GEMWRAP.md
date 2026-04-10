# gemwrap

Personal Gemini API wrapper that uses OAuth tokens from gemini-cli to make direct API calls. Bypasses the standard Gemini API key rate limits by hitting Google's Code Assist endpoint (`cloudcode-pa.googleapis.com`) — a separate quota pool with ~1,000 free requests/day per account.

## Installation

```bash
pip install -e .
```

Requires gemini-cli to be authenticated first (`gemini` command, sign in via browser).

## How It Works

1. Reads the OAuth refresh token from gemini-cli's credential file (`~/.gemini/oauth_creds.json`)
2. Refreshes the access token via Google's OAuth endpoint
3. Discovers the project ID via `loadCodeAssist`
4. Makes direct API calls to `cloudcode-pa.googleapis.com/v1internal`

Token refresh and project discovery are cached in `~/.config/gemwrap/.token_cache.json` to avoid extra HTTP calls on every invocation.

## CLI Usage

```bash
# Basic prompt
gemwrap "Explain Python decorators in 3 sentences"

# Pipe from stdin
echo "Summarize this" | gemwrap

# Streaming output
gemwrap --stream "Tell me a story"

# System instruction
gemwrap -s "You are a pirate" "What is recursion?"

# Model selection
gemwrap -m gemini-2.5-pro "Explain quantum computing"
gemwrap -m gemini-2.5-flash "Quick summary of X"
gemwrap -m gemini-3-flash-preview "Hello"
gemwrap -m gemini-3.1-pro-preview "Complex reasoning task"

# Account selection
gemwrap -a pro "Hello"          # use pro account
gemwrap -a free "Hello"         # use free account
gemwrap "Hello"                 # round-robin rotation

# YouTube video analysis (real video understanding, not just transcript)
gemwrap --youtube "https://youtu.be/VIDEO_ID" "Summarize this video"
gemwrap --youtube "https://youtu.be/VIDEO_ID" "Describe what you SEE in the video"
gemwrap --stream --youtube "https://youtu.be/VIDEO_ID" "Key points?"

# Check remaining quota (live from Google)
gemwrap --quota
gemwrap --quota -a pro

# List configured accounts
gemwrap --list-accounts

# Verbose mode (shows auth steps, HTTP status codes)
gemwrap -v "Hello"
```

### All CLI Flags

| Flag | Short | Description |
|------|-------|-------------|
| `--model` | `-m` | Model name (gemini-2.5-flash, gemini-2.5-pro, gemini-3-flash-preview, gemini-3.1-pro-preview, etc.) |
| `--account` | `-a` | Account name from config |
| `--backend` | `-b` | Force backend: `cli_oauth` or `api_key` |
| `--temperature` | `-t` | Temperature (default: 0.7) |
| `--max-tokens` | | Max output tokens (default: 8192) |
| `--system` | `-s` | System instruction |
| `--youtube` | | YouTube URL for video analysis |
| `--image` | | Path to image file (png, jpg, gif, webp) |
| `--stream` | | Stream response chunks to stdout |
| `--verbose` | `-v` | Debug output |
| `--quota` | | Show remaining quota from Google |
| `--list-accounts` | | List configured accounts |
| `--init-config` | | Create default config file |

## Python Library Usage

```python
from gemwrap import GeminiClient

client = GeminiClient()

# Basic generation
text = client.generate("Explain decorators")

# With options
text = client.generate(
    "Explain quantum computing",
    model="gemini-2.5-pro",
    system="Be concise, use bullet points",
    temperature=0.5,
    max_tokens=4096,
    account="pro",
)

# Streaming
for chunk in client.stream("Tell me a story"):
    print(chunk, end="", flush=True)

# Multi-turn conversation
history = [
    {"role": "user", "parts": [{"text": "What is Python?"}]},
    {"role": "model", "parts": [{"text": "Python is a programming language."}]},
]
text = client.generate("Tell me more about its type system", history=history)

# YouTube video analysis
summary = client.generate(
    "Summarize this video and identify the speaker",
    youtube="https://youtu.be/VIDEO_ID",
)

# Check quota
quotas = client.quota()          # all accounts
quotas = client.quota("pro")     # specific account
# Returns: {"pro": [{"model": "gemini-2.5-flash", "remaining_pct": 98.3, ...}, ...]}

# List accounts
client.list_accounts()
```

## Configuration

Config file: `~/.config/gemwrap/accounts.json` (auto-created on first run)

```json
{
  "accounts": [
    {
      "name": "pro",
      "backend": "cli_oauth",
      "creds_path": "~/.gemini/oauth_creds.json",
      "model": "gemini-3.1-pro-preview",
      "enabled": true
    },
    {
      "name": "free",
      "backend": "cli_oauth",
      "creds_path": "~/.gemini-pro/.gemini/oauth_creds.json",
      "model": "gemini-2.5-flash",
      "enabled": true
    },
    {
      "name": "apikey",
      "backend": "api_key",
      "api_key": "AIza...",
      "model": "gemini-2.5-flash",
      "enabled": false
    }
  ],
  "rotation": "round_robin",
  "default_account": null
}
```

### Account Fields

| Field | Description |
|-------|-------------|
| `name` | Account identifier used with `-a` flag |
| `backend` | `cli_oauth` (cloudcode-pa, free) or `api_key` (generativelanguage, paid/limited) |
| `creds_path` | Path to gemini-cli's `oauth_creds.json` (supports `~`) |
| `model` | Default model for this account |
| `enabled` | Set `false` to skip in rotation without deleting |
| `api_key` | Gemini API key (only for `api_key` backend) |

### Rotation Modes

| Mode | Behavior |
|------|----------|
| `round_robin` | Alternates between enabled accounts each call (default) |
| `failover` | Uses first account, switches to next on 429/5xx errors |
| `sticky` | Always uses `default_account`, no rotation |

## Adding a Second Account

```bash
# Authenticate with a different Google account
mkdir -p ~/.gemini-pro
GEMINI_CLI_HOME=~/.gemini-pro gemini
# Sign in with your other Google account, then Ctrl+C

# Credentials will be at ~/.gemini-pro/.gemini/oauth_creds.json
# Add it to ~/.config/gemwrap/accounts.json
```

## Quota & Rate Limits

### CLI OAuth (cloudcode-pa) — per account

| Tier | Requests/Day | Requests/Min |
|------|-------------|-------------|
| Free (Google account) | 1,000 | 60 |
| Google AI Pro | 1,500 | 120 |
| Google AI Ultra | 2,000 | 120 |

### API Key (generativelanguage) — separate pool, much lower

| Model | Free RPD | Free RPM |
|-------|---------|---------|
| gemini-2.5-flash | ~20 | 5 |
| gemini-2.5-pro | 0 (blocked) | 0 |

The CLI OAuth path is vastly superior for free usage. With 2 accounts you get ~2,000 requests/day.

## Constraints & Caveats

- **Internal API**: Uses `cloudcode-pa.googleapis.com/v1internal` which is not a public stable API. Google can change it without notice. In practice, it has been stable across gemini-cli releases.
- **Auth dependency**: Requires gemini-cli to have been authenticated at least once. If tokens stop working, re-run `gemini` and sign in again.
- **Token expiry**: Access tokens last ~1 hour. gemwrap auto-refreshes using the stored refresh token. Refresh tokens are long-lived but can be revoked by Google.
- **YouTube videos**: Must be public or unlisted. One video per request. Max 8 hours of video per day. Uses real multimodal video+audio understanding, not transcript.
- **Image upload**: Supports inline base64 images (png, jpg, gif, webp) via `--image` flag or `image=` parameter. PDFs and other file types are not supported.
- **Rate limits are per-user**: Shared between gemwrap and gemini-cli itself (same OAuth token = same quota pool). If you use gemini-cli interactively, it eats into the same daily quota.

## File Structure

```
gemwrap/
  pyproject.toml              # packaging + "gemwrap" CLI entry point
  gemwrap/
    __init__.py               # re-exports GeminiClient
    client.py                 # core: auth, accounts, API calls, streaming, rotation
    cli.py                    # argparse CLI entry point

~/.config/gemwrap/
  accounts.json               # account configuration
  .token_cache.json           # cached access tokens + project IDs
```

## Error Handling

All errors raise `GemwrapError` with `.status_code` and `.body` attributes:

```python
from gemwrap import GeminiClient
from gemwrap.client import GemwrapError

client = GeminiClient()
try:
    text = client.generate("Hello")
except GemwrapError as e:
    if e.status_code == 429:
        print("Quota exceeded, try another account")
    else:
        print(f"Error {e.status_code}: {e}")
```

In `failover` rotation mode, 429/5xx errors automatically retry with the next account.
