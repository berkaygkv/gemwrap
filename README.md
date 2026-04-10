# gemwrap

Personal Gemini API wrapper with multi-account rotation. Uses OAuth tokens from [gemini-cli](https://github.com/google-gemini/gemini-cli) to hit Google's Code Assist endpoint — a separate quota pool with ~1,000 free requests/day per account.

## Install

```bash
pip install -e .
```

Requires gemini-cli authenticated first (`gemini` → sign in via browser).

## CLI

```bash
gemwrap "Explain Python decorators"
gemwrap --stream "Tell me a story"
gemwrap -m gemini-3.1-pro-preview "Complex task"
gemwrap -s "You are a pirate" "What is recursion?"
gemwrap --youtube "https://youtu.be/VIDEO_ID" "Summarize this video"
gemwrap --image photo.png "Describe this image"
echo "Summarize this" | gemwrap
gemwrap --quota
gemwrap --list-accounts
```

## Python

```python
from gemwrap import GeminiClient

client = GeminiClient()
text = client.generate("Hello")

for chunk in client.stream("Tell me a story"):
    print(chunk, end="", flush=True)
```

## Multi-Account Rotation

Configure accounts in `~/.config/gemwrap/accounts.json` (auto-created on first run). Supports `round_robin`, `failover`, and `sticky` rotation across multiple Google accounts.

Two backends: `cli_oauth` (free, ~1k req/day) and `api_key` (standard Gemini API).

See [GEMWRAP.md](GEMWRAP.md) for full documentation.
