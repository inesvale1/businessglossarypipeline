from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from .config_loader import LLMConfig


def _read_keyring(service: str, username: str) -> str:
    if not service or not username:
        return ""
    try:
        import keyring
        return keyring.get_password(service, username) or ""
    except Exception:
        return ""


def _detect_proxy_url() -> Optional[str]:
    """Detect the corporate proxy the same way urllib does: env vars first,
    falling back to the OS-level config on Windows/macOS, since this
    network's proxy is configured system-wide and is not exposed through
    HTTPS_PROXY/HTTP_PROXY."""
    from urllib.request import getproxies

    proxy_info = getproxies()
    return (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or proxy_info.get("https")
        or proxy_info.get("http")
        or None
    )


@dataclass
class LLMClient:
    enabled: bool
    api_key: str
    model: str
    base_url: str
    api_type: str
    api_version: str
    timeout_seconds: int
    temperature: float
    max_output_tokens: int
    disabled_reason: str = ""
    last_error: str = ""
    last_finish_reason: str = ""

    @classmethod
    def from_config(cls, config: LLMConfig) -> "LLMClient":
        api_key = os.environ.get(config.api_key_env, "") if config.api_key_env else ""
        if not api_key:
            api_key = _read_keyring(config.api_key_keyring_service, config.api_key_keyring_username)

        enabled = bool(config.enabled and config.model and (api_key or not config.require_api_key))
        if not config.enabled:
            disabled_reason = "LLM_DISABLED"
        elif config.require_api_key and not api_key:
            disabled_reason = "LLM_MISSING_API_KEY"
        elif not config.model:
            disabled_reason = "LLM_MISSING_MODEL"
        else:
            disabled_reason = ""

        return cls(
            enabled=enabled,
            api_key=api_key,
            model=config.model,
            base_url=config.base_url,
            api_type=config.api_type,
            api_version=config.api_version,
            timeout_seconds=config.timeout_seconds,
            temperature=config.temperature,
            max_output_tokens=config.max_output_tokens,
            disabled_reason=disabled_reason,
        )

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any] | None:
        """Send one chat-completion request and parse the response as JSON.

        Returns None (with `last_error` set) on any transport, HTTP, or JSON
        parsing failure, so callers can decide how to react (skip batch, retry,
        abort schema) without this client encoding that policy itself.
        """
        if not self.enabled:
            self.last_error = self.disabled_reason
            return None

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
        }

        raw = self._post_json(payload)
        if raw is None:
            return None

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            import re
            match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
            if self.last_finish_reason == "length":
                self.last_error = (
                    "LLM response was not valid JSON (resposta truncada: o modelo atingiu "
                    f"max_output_tokens={self.max_output_tokens} antes de terminar o JSON)."
                )
            else:
                self.last_error = "LLM response was not valid JSON."
            return None

    def _build_endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if self.api_type.lower() == "azure":
            url = f"{base}/openai/deployments/{self.model}/chat/completions"
            if self.api_version:
                url += f"?api-version={self.api_version}"
            return url
        return base + "/chat/completions"

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            if self.api_type.lower() == "azure":
                headers["api-key"] = self.api_key
            else:
                headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _post_json(self, payload: dict[str, Any]) -> str | None:
        self.last_error = ""
        self.last_finish_reason = ""
        endpoint = self._build_endpoint()

        proxy_url = _detect_proxy_url()
        try:
            if proxy_url:
                status_code, response_text = self._post_via_curl(endpoint, payload, proxy_url)
            else:
                status_code, response_text = self._post_via_httpx(endpoint, payload)
        except Exception as exc:
            self.last_error = str(exc)
            return None

        if status_code < 200 or status_code >= 300:
            self.last_error = self._format_http_error(status_code, response_text)
            return None

        body = json.loads(response_text)
        choices = body.get("choices", [])
        if not choices:
            self.last_error = "Empty choices returned by LLM API."
            return None
        self.last_finish_reason = choices[0].get("finish_reason", "")
        content = choices[0].get("message", {}).get("content", "")
        return str(content).strip() or None

    def _post_via_httpx(self, endpoint: str, payload: dict[str, Any]) -> tuple[int, str]:
        with httpx.Client() as client:
            response = client.post(
                endpoint,
                json=payload,
                headers=self._build_headers(),
                timeout=self.timeout_seconds,
            )
        return response.status_code, response.text

    def _post_via_curl(self, endpoint: str, payload: dict[str, Any], proxy_url: str) -> tuple[int, str]:
        """Send the request through curl instead of httpx.

        This network's proxy (Skyhigh Secure Web Gateway) enforces NTLM proxy
        authentication and rejects Basic auth outright, even with correct
        credentials (confirmed via a raw CONNECT probe). httpx/httpcore has
        no NTLM support. curl on Windows is built against SSPI, so
        `--proxy-ntlm --proxy-user :` transparently authenticates using the
        current Windows-domain login (single sign-on) -- no password needed.
        `--ssl-revoke-best-effort` avoids hard failures when the corporate
        gateway also blocks OCSP/CRL revocation checks.
        """
        headers = self._build_headers()
        header_args = []
        for key, value in headers.items():
            header_args += ["-H", f"{key}: {value}"]

        # Default to SSPI single sign-on (no password sent at all). Stored
        # credentials are opt-in only (PROXY_USE_EXPLICIT_CREDS=1), because a
        # wrong stored password sent on every batch call risks tripping an
        # AD account lockout -- SSO reuses the already-validated Windows
        # logon instead of guessing a password.
        proxy_user_arg = ":"
        if os.environ.get("PROXY_USE_EXPLICIT_CREDS") == "1":
            proxy_user = os.environ.get("PROXY_USER", "")
            proxy_pass = os.environ.get("PROXY_PASS", "") or _read_keyring("catalogo-semantico-proxy", proxy_user)
            if proxy_user and proxy_pass:
                proxy_user_arg = f"{proxy_user}:{proxy_pass}"

        with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as body_file:
            body_path = body_file.name

        try:
            cmd = [
                "curl.exe", "-sS",
                "--proxy", proxy_url,
                "--proxy-ntlm", "--proxy-user", proxy_user_arg,
                "--ssl-revoke-best-effort",
                "-X", "POST", endpoint,
                *header_args,
                "--data-binary", "@-",
                "-o", body_path,
                "-w", "%{http_code}",
                "--max-time", str(self.timeout_seconds),
            ]
            result = subprocess.run(
                cmd,
                input=json.dumps(payload).encode("utf-8"),
                capture_output=True,
                timeout=self.timeout_seconds + 10,
            )
            with open(body_path, "r", encoding="utf-8") as handle:
                body_text = handle.read()
        finally:
            os.unlink(body_path)

        if result.returncode != 0:
            raise RuntimeError(f"curl falhou (exit {result.returncode}): {result.stderr.decode(errors='replace').strip()}")

        status_code = int(result.stdout.decode().strip() or "0")
        return status_code, body_text

    def _format_http_error(self, status_code: int, response_text: str) -> str:
        detail = ""
        try:
            parsed = json.loads(response_text)
            if isinstance(parsed, dict):
                err = parsed.get("error", {})
                detail = str(err.get("message", "")).strip() if isinstance(err, dict) else ""
                if not detail:
                    detail = response_text.strip()
        except Exception:
            detail = response_text.strip()
        summary = f"HTTP {status_code}"
        return f"{summary}: {detail}".strip(": ")
