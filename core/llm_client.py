from __future__ import annotations

import json
import os
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


def _build_proxy_http_client() -> Optional[httpx.Client]:
    """Build an httpx client that authenticates against a corporate proxy, if configured.

    The proxy itself is detected the same way urllib does (env vars first,
    falling back to the OS-level config on Windows/macOS), because this
    network's proxy is configured system-wide and is not exposed through
    HTTPS_PROXY/HTTP_PROXY.

    Credentials are embedded as Basic auth directly in the proxy URL: httpcore
    only ever sends Proxy-Authorization on the initial CONNECT when
    credentials are embedded that way. An `auth=` object authenticates the
    *request* that flows through an already-established tunnel, so it never
    gets a chance to run if the CONNECT itself is rejected with 407.
    """
    from urllib.request import getproxies

    proxy_info = getproxies()
    proxy_url = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or proxy_info.get("https")
        or proxy_info.get("http")
    )
    if not proxy_url:
        return None

    proxy_user = os.environ.get("PROXY_USER", "")
    proxy_pass = os.environ.get("PROXY_PASS", "")
    if proxy_user and not proxy_pass:
        proxy_pass = _read_keyring("catalogo-semantico-proxy", proxy_user)

    if proxy_user and proxy_pass:
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(proxy_url)
        if not parsed.username:
            netloc = f"{proxy_user}:{proxy_pass}@{parsed.hostname}"
            if parsed.port:
                netloc += f":{parsed.port}"
            proxy_url = urlunparse(parsed._replace(netloc=netloc))

    return httpx.Client(proxy=proxy_url)


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
        http_client = _build_proxy_http_client()
        try:
            self.last_error = ""
            self.last_finish_reason = ""
            endpoint = self._build_endpoint()
            owns_client = http_client is None
            client = http_client or httpx.Client()
            try:
                response = client.post(
                    endpoint,
                    json=payload,
                    headers=self._build_headers(),
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                body = response.json()
            finally:
                if owns_client:
                    client.close()
        except httpx.HTTPStatusError as exc:
            self.last_error = self._format_http_error(exc)
            return None
        except Exception as exc:
            self.last_error = str(exc)
            return None

        choices = body.get("choices", [])
        if not choices:
            self.last_error = "Empty choices returned by LLM API."
            return None
        self.last_finish_reason = choices[0].get("finish_reason", "")
        content = choices[0].get("message", {}).get("content", "")
        return str(content).strip() or None

    def _format_http_error(self, exc: httpx.HTTPStatusError) -> str:
        detail = ""
        try:
            parsed = json.loads(exc.response.text)
            if isinstance(parsed, dict):
                err = parsed.get("error", {})
                detail = str(err.get("message", "")).strip() if isinstance(err, dict) else ""
                if not detail:
                    detail = exc.response.text.strip()
        except Exception:
            detail = str(exc)
        summary = f"HTTP {exc.response.status_code} {exc.response.reason_phrase}".strip()
        return f"{summary}: {detail}".strip(": ")
