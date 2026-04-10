import argparse
import json
import sys
from datetime import datetime, timezone

from gemwrap.client import GeminiClient, GemwrapError


def _fmt_resets(iso_str: str) -> str:
    """Convert '2026-02-24T12:00:27Z' to 'resets in 22h 30m'."""
    try:
        reset = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = reset - datetime.now(timezone.utc)
        total_sec = max(int(delta.total_seconds()), 0)
        hours, remainder = divmod(total_sec, 3600)
        minutes = remainder // 60
        if hours > 0:
            return f"resets in {hours}h {minutes}m"
        return f"resets in {minutes}m"
    except (ValueError, TypeError):
        return iso_str


def main():
    parser = argparse.ArgumentParser(
        prog="gemwrap",
        description="Gemini API wrapper with multi-account rotation",
    )
    parser.add_argument("prompt", nargs="?", default=None, help="Prompt text (or pipe via stdin)")
    parser.add_argument("-m", "--model", default=None, help="Model (e.g. gemini-3.1-pro-preview, gemini-3-flash-preview, gemini-2.5-flash, gemini-2.5-pro)")
    parser.add_argument("-a", "--account", default=None, help="Account name from config")
    parser.add_argument("-b", "--backend", default=None, choices=["cli_oauth", "api_key"], help="Force backend")
    parser.add_argument("-t", "--temperature", type=float, default=0.7)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("-s", "--system", default=None, help="System instruction")
    parser.add_argument("--youtube", default=None, help="YouTube URL to analyze")
    parser.add_argument("--stream", action="store_true", help="Stream response to stdout")
    parser.add_argument("--image", default=None, help="Path to image file to include")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug output")
    parser.add_argument("--quota", action="store_true", help="Show remaining quota for all accounts")
    parser.add_argument("--list-accounts", action="store_true", help="List configured accounts")
    parser.add_argument("--init-config", action="store_true", help="Create default config file")

    args = parser.parse_args()

    if args.init_config:
        from gemwrap.client import CONFIG_PATH
        GeminiClient._bootstrap_config()
        print(f"Config created at {CONFIG_PATH}")
        return

    try:
        client = GeminiClient(
            account=args.account,
            model=args.model,
            backend=args.backend,
            verbose=args.verbose,
        )
    except GemwrapError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.list_accounts:
        for a in client.list_accounts():
            status = "enabled" if a["enabled"] else "disabled"
            print(f"  {a['name']:<12s}  {a['backend']:<12s}  {a['model']:<24s}  [{status}]  ({a['requests']} reqs)")
        return

    if args.quota:
        try:
            quotas = client.quota(account=args.account)
        except GemwrapError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        for acct_name, buckets in quotas.items():
            print(f"\n  {acct_name}:")
            print(f"  {'Model':<28s}  {'Reqs':>6s}  {'Usage remaining':>20s}")
            print(f"  {'─' * 28}  {'─' * 6}  {'─' * 30}")
            for b in buckets:
                if "error" in b:
                    print(f"  {b['error']}")
                    continue
                pct = b["remaining_pct"]
                amt = b.get("remaining_amount") or "-"
                reset_str = _fmt_resets(b["resets_at"])
                print(f"  {b['model']:<28s}  {amt:>6s}  {pct:5.1f}% {reset_str}")
        print()
        return

    prompt = args.prompt
    if prompt is None:
        if not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
        else:
            parser.error("No prompt provided. Pass as argument or pipe via stdin.")

    try:
        if args.stream:
            for chunk in client.stream(
                prompt,
                system=args.system,
                youtube=args.youtube,
                image=args.image,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
            ):
                sys.stdout.write(chunk)
                sys.stdout.flush()
            sys.stdout.write("\n")
        else:
            result = client.generate(
                prompt,
                system=args.system,
                youtube=args.youtube,
                image=args.image,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
            )
            print(result)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except GemwrapError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
