import base64
import json
import os
import time
from pathlib import Path
from typing import Iterator, Optional

import requests

# OAuth client embedded in gemini-cli source (public, "installed app" type)
CLIENT_ID = "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com"
CLIENT_SECRET = "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"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CLOUDCODE_BASE = "https://cloudcode-pa.googleapis.com/v1internal"
GENAI_BASE = "https://generativelanguage.googleapis.com/v1beta"
CONFIG_DIR = Path.home() / ".config" / "gemwrap"
CONFIG_PATH = CONFIG_DIR / "accounts.json"
TOKEN_CACHE_PATH = CONFIG_DIR / ".token_cache.json"
TOKEN_REFRESH_MARGIN_SEC = 300  # refresh 5 min before expiry


class GemwrapError(Exception):
    def __init__(self, message: str, status_code: int = 0, body: str = ""):
        self.status_code = status_code
        self.body = body
        super().__init__(message)


class AccountState:
    def __init__(self, config: dict):
        self.name: str = config["name"]
        self.backend: str = config["backend"]
        self.model: str = config.get("model", "gemini-3.1-pro-preview")
        self.enabled: bool = config.get("enabled", True)
        # cli_oauth
        self.creds_path: Optional[Path] = (
            Path(config["creds_path"]).expanduser() if config.get("creds_path") else None
        )
        self.refresh_token: Optional[str] = None
        self.access_token: Optional[str] = None
        self.token_expires_at: float = 0
        self.project_id: Optional[str] = None
        # api_key
        self.api_key: Optional[str] = config.get("api_key")
        # stats
        self.consecutive_errors: int = 0
        self.total_requests: int = 0

    def is_token_valid(self) -> bool:
        if self.backend != "cli_oauth":
            return True
        if not self.access_token:
            return False
        return time.time() < (self.token_expires_at - TOKEN_REFRESH_MARGIN_SEC)


class GeminiClient:
    def __init__(
        self,
        config_path: Optional[str] = None,
        account: Optional[str] = None,
        model: Optional[str] = None,
        backend: Optional[str] = None,
        verbose: bool = False,
    ):
        self._verbose = verbose
        self._model_override = model
        self._account_pin = account
        self._backend_filter = backend
        self._config = self._load_config(config_path)
        self._accounts: dict[str, AccountState] = {}
        self._rr_index = 0

        for acct_cfg in self._config.get("accounts", []):
            acct = AccountState(acct_cfg)
            if self._backend_filter and acct.backend != self._backend_filter:
                continue
            self._accounts[acct.name] = acct

        self._rotation = self._config.get("rotation", "round_robin")
        self._load_token_cache()

    # ── Public API ──────────────────────────────────────────────

    def generate(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        history: Optional[list] = None,
        model: Optional[str] = None,
        youtube: Optional[str] = None,
        image: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 8192,
        account: Optional[str] = None,
    ) -> str:
        acct = self._pick_account(account)
        try:
            return self._do_generate(acct, prompt, system=system, history=history,
                                     model=model, youtube=youtube, image=image,
                                     temperature=temperature, max_tokens=max_tokens)
        except GemwrapError as e:
            acct.consecutive_errors += 1
            if self._rotation == "failover" and account is None and e.status_code in (429, 500, 502, 503):
                self._log(f"Account '{acct.name}' failed ({e.status_code}), trying next...")
                next_acct = self._pick_account()
                return self._do_generate(next_acct, prompt, system=system, history=history,
                                         model=model, youtube=youtube, image=image,
                                         temperature=temperature, max_tokens=max_tokens)
            raise

    def stream(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        history: Optional[list] = None,
        model: Optional[str] = None,
        youtube: Optional[str] = None,
        image: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 8192,
        account: Optional[str] = None,
    ) -> Iterator[str]:
        acct = self._pick_account(account)
        self._ensure_auth(acct)
        use_model = model or self._model_override or acct.model
        body = self._build_body(acct, prompt, system=system, history=history,
                                youtube=youtube, image=image,
                                model=use_model, temperature=temperature, max_tokens=max_tokens)
        yield from self._call_stream(acct, body, use_model)
        acct.consecutive_errors = 0
        acct.total_requests += 1

    def list_accounts(self) -> list[dict]:
        return [
            {"name": a.name, "backend": a.backend, "model": a.model,
             "enabled": a.enabled, "requests": a.total_requests}
            for a in self._accounts.values()
        ]

    def quota(self, account: Optional[str] = None) -> dict[str, list[dict]]:
        """Fetch remaining quota from Google for each (or a specific) account.
        Returns {account_name: [{model, remaining_pct, resets_at}, ...]}"""
        targets = (
            [self._accounts[account]] if account
            else [a for a in self._accounts.values() if a.enabled and a.backend == "cli_oauth"]
        )
        result = {}
        for acct in targets:
            if acct.backend != "cli_oauth":
                continue
            self._ensure_auth(acct)
            resp = requests.post(
                f"{CLOUDCODE_BASE}:retrieveUserQuota",
                headers={
                    "Authorization": f"Bearer {acct.access_token}",
                    "Content-Type": "application/json",
                },
                json={"project": acct.project_id},
                timeout=15,
            )
            if resp.status_code != 200:
                result[acct.name] = [{"error": f"HTTP {resp.status_code}"}]
                continue
            buckets = resp.json().get("buckets", [])
            result[acct.name] = [
                {
                    "model": b["modelId"],
                    "remaining_pct": round(b.get("remainingFraction", 0) * 100, 1),
                    "remaining_amount": b.get("remainingAmount"),
                    "resets_at": b.get("resetTime", ""),
                }
                for b in buckets
                if not b["modelId"].endswith("_vertex")
            ]
        return result

    # ── Internal: generate helper ───────────────────────────────

    def _do_generate(self, acct: AccountState, prompt: str, **kwargs) -> str:
        self._ensure_auth(acct)
        use_model = kwargs.pop("model", None) or self._model_override or acct.model
        body = self._build_body(acct, prompt, model=use_model, **kwargs)
        resp_json = self._call_generate(acct, body, use_model)
        acct.consecutive_errors = 0
        acct.total_requests += 1
        return self._extract_text(resp_json)

    # ── Internal: auth ──────────────────────────────────────────

    def _ensure_auth(self, acct: AccountState) -> None:
        if acct.backend == "api_key":
            if not acct.api_key:
                raise GemwrapError(f"Account '{acct.name}' has no api_key configured")
            return

        if acct.refresh_token is None:
            if not acct.creds_path or not acct.creds_path.exists():
                raise GemwrapError(
                    f"Credentials file not found: {acct.creds_path}\n"
                    "Is gemini-cli authenticated? Run 'gemini' and sign in first."
                )
            raw = json.loads(acct.creds_path.read_text())
            acct.refresh_token = raw.get("refresh_token")
            if not acct.refresh_token:
                raise GemwrapError(f"No refresh_token in {acct.creds_path}")
            # use stored access token if still valid
            stored_expiry = raw.get("expiry_date", 0) / 1000
            if stored_expiry > time.time() + TOKEN_REFRESH_MARGIN_SEC and not acct.access_token:
                acct.access_token = raw.get("access_token")
                acct.token_expires_at = stored_expiry

        if not acct.is_token_valid():
            self._refresh_access_token(acct)

        if acct.project_id is None:
            self._discover_project(acct)

    def _refresh_access_token(self, acct: AccountState) -> None:
        self._log(f"Refreshing token for '{acct.name}'...")
        resp = requests.post(
            TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "refresh_token": acct.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            raise GemwrapError(
                f"Token refresh failed for '{acct.name}': {resp.text}\n"
                "Try re-authenticating: run 'gemini' and sign in again.",
                status_code=resp.status_code, body=resp.text,
            )
        data = resp.json()
        acct.access_token = data["access_token"]
        acct.token_expires_at = time.time() + data.get("expires_in", 3600)
        self._save_token_cache()
        self._log(f"Token refreshed, expires in {data.get('expires_in', '?')}s")

    def _discover_project(self, acct: AccountState) -> None:
        self._log(f"Discovering project for '{acct.name}'...")
        resp = requests.post(
            f"{CLOUDCODE_BASE}:loadCodeAssist",
            headers={
                "Authorization": f"Bearer {acct.access_token}",
                "Content-Type": "application/json",
            },
            json={},
            timeout=15,
        )
        if resp.status_code != 200:
            raise GemwrapError(
                f"Project discovery failed for '{acct.name}': {resp.text}",
                status_code=resp.status_code, body=resp.text,
            )
        data = resp.json()
        acct.project_id = data.get("cloudaicompanionProject") or data.get("project") or data.get("projectId", "")
        if not acct.project_id:
            for key, val in data.items():
                if isinstance(val, str) and "project" in key.lower():
                    acct.project_id = val
                    break
        self._save_token_cache()
        self._log(f"Project: {acct.project_id}")

    # ── Internal: request building ──────────────────────────────

    def _build_body(
        self, acct: AccountState, prompt: str, *,
        system: Optional[str] = None, history: Optional[list] = None,
        youtube: Optional[str] = None, image: Optional[str] = None,
        model: str = "gemini-3.1-pro-preview", temperature: float = 0.7,
        max_tokens: int = 8192,
    ) -> dict:
        contents = list(history) if history else []
        user_parts = []
        if youtube:
            user_parts.append({"fileData": {"fileUri": youtube, "mimeType": "video/*"}})
        if image:
            img_path = Path(image)
            suffix = img_path.suffix.lower()
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "gif": "image/gif", "webp": "image/webp"}.get(suffix.lstrip("."), "image/png")
            img_data = base64.b64encode(img_path.read_bytes()).decode("ascii")
            user_parts.append({"inlineData": {"mimeType": mime, "data": img_data}})
        user_parts.append({"text": prompt})
        contents.append({"role": "user", "parts": user_parts})

        gen_config = {"maxOutputTokens": max_tokens, "temperature": temperature}

        if acct.backend == "cli_oauth":
            request_inner = {"contents": contents, "generationConfig": gen_config}
            if system:
                request_inner["systemInstruction"] = {"parts": [{"text": system}]}
            return {"model": model, "project": acct.project_id, "request": request_inner}
        else:
            body = {"contents": contents, "generationConfig": gen_config}
            if system:
                body["systemInstruction"] = {"parts": [{"text": system}]}
            body["_model"] = model  # carried for URL construction, stripped before send
            return body

    # ── Internal: API calls ─────────────────────────────────────

    def _call_generate(self, acct: AccountState, body: dict, model: str) -> dict:
        if acct.backend == "cli_oauth":
            url = f"{CLOUDCODE_BASE}:generateContent"
            headers = {
                "Authorization": f"Bearer {acct.access_token}",
                "Content-Type": "application/json",
            }
            resp = requests.post(url, headers=headers, json=body, timeout=600)
        else:
            send_body = {k: v for k, v in body.items() if k != "_model"}
            url = f"{GENAI_BASE}/models/{model}:generateContent?key={acct.api_key}"
            headers = {"Content-Type": "application/json"}
            resp = requests.post(url, headers=headers, json=send_body, timeout=600)

        self._log(f"generateContent → {resp.status_code}")
        if resp.status_code != 200:
            raise GemwrapError(
                f"generateContent failed ({resp.status_code}): {resp.text[:500]}",
                status_code=resp.status_code, body=resp.text,
            )
        return resp.json()

    def _call_stream(self, acct: AccountState, body: dict, model: str) -> Iterator[str]:
        if acct.backend == "cli_oauth":
            url = f"{CLOUDCODE_BASE}:streamGenerateContent?alt=sse"
            headers = {
                "Authorization": f"Bearer {acct.access_token}",
                "Content-Type": "application/json",
            }
            resp = requests.post(url, headers=headers, json=body, timeout=600, stream=True)
        else:
            send_body = {k: v for k, v in body.items() if k != "_model"}
            url = f"{GENAI_BASE}/models/{model}:streamGenerateContent?alt=sse&key={acct.api_key}"
            headers = {"Content-Type": "application/json"}
            resp = requests.post(url, headers=headers, json=send_body, timeout=600, stream=True)

        self._log(f"streamGenerateContent → {resp.status_code}")
        if resp.status_code != 200:
            raise GemwrapError(
                f"streamGenerateContent failed ({resp.status_code}): {resp.text[:500]}",
                status_code=resp.status_code, body=resp.text,
            )

        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            text = self._extract_text(chunk)
            if text:
                yield text

    def _extract_text(self, data: dict) -> str:
        candidates = data.get("candidates") or data.get("response", {}).get("candidates", [])
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)

    # ── Internal: account rotation ──────────────────────────────

    def _pick_account(self, requested: Optional[str] = None) -> AccountState:
        if requested or self._account_pin:
            name = requested or self._account_pin
            if name not in self._accounts:
                raise GemwrapError(
                    f"Account '{name}' not found. Available: {list(self._accounts.keys())}"
                )
            return self._accounts[name]

        enabled = [a for a in self._accounts.values() if a.enabled]
        if not enabled:
            raise GemwrapError("No enabled accounts. Check ~/.config/gemwrap/accounts.json")

        if self._rotation == "sticky":
            default = self._config.get("default_account")
            if default and default in self._accounts:
                return self._accounts[default]
            return enabled[0]

        if self._rotation == "failover":
            for a in enabled:
                if a.consecutive_errors < 3:
                    return a
            for a in enabled:
                a.consecutive_errors = 0
            return enabled[0]

        # round_robin
        acct = enabled[self._rr_index % len(enabled)]
        self._rr_index += 1
        return acct

    # ── Internal: config ────────────────────────────────────────

    def _load_config(self, config_path: Optional[str] = None) -> dict:
        path = Path(config_path) if config_path else CONFIG_PATH
        if path.exists():
            return json.loads(path.read_text())
        return self._bootstrap_config()

    @staticmethod
    def _bootstrap_config() -> dict:
        default = {
            "accounts": [
                {
                    "name": "free",
                    "backend": "cli_oauth",
                    "creds_path": "~/.gemini/oauth_creds.json",
                    "model": "gemini-3.1-pro-preview",
                    "enabled": True,
                }
            ],
            "rotation": "round_robin",
            "default_account": None,
        }
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(default, indent=2))
        os.chmod(CONFIG_PATH, 0o600)
        return default

    # ── Internal: token cache ───────────────────────────────────

    def _save_token_cache(self) -> None:
        cache = {}
        for name, acct in self._accounts.items():
            if acct.backend != "cli_oauth":
                continue
            cache[name] = {
                "access_token": acct.access_token,
                "token_expires_at": acct.token_expires_at,
                "project_id": acct.project_id,
            }
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE_PATH.write_text(json.dumps(cache, indent=2))
        os.chmod(TOKEN_CACHE_PATH, 0o600)

    def _load_token_cache(self) -> None:
        if not TOKEN_CACHE_PATH.exists():
            return
        try:
            cache = json.loads(TOKEN_CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return
        for name, data in cache.items():
            if name not in self._accounts:
                continue
            acct = self._accounts[name]
            if acct.backend != "cli_oauth":
                continue
            expires = data.get("token_expires_at", 0)
            if expires > time.time() + TOKEN_REFRESH_MARGIN_SEC:
                acct.access_token = data.get("access_token")
                acct.token_expires_at = expires
            acct.project_id = data.get("project_id")

    # ── Internal: logging ───────────────────────────────────────

    def _log(self, msg: str) -> None:
        if self._verbose:
            print(f"[gemwrap] {msg}")
