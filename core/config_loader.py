from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LLMConfig:
    enabled: bool = True
    api_type: str = "azure"
    api_key_env: str = ""
    api_key_keyring_service: str = ""
    api_key_keyring_username: str = ""
    require_api_key: bool = True
    model: str = "gpt-4o-mini"
    base_url: str = ""
    api_version: str = ""
    timeout_seconds: int = 180
    temperature: float = 0.1
    max_output_tokens: int = 8000


@dataclass(frozen=True)
class BatchingConfig:
    max_batch_bytes: int = 60000


@dataclass(frozen=True)
class CatalogoConfig:
    schema_root: Path
    organizational_context_path: Path
    schemas: list[str]
    batching: BatchingConfig
    llm: LLMConfig


def load_config(path: str | Path) -> CatalogoConfig:
    config_path = Path(path).resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    # config/catalogo.config.json -> <projeto>/ -> Implementation/
    workspace_root = config_path.parent.parent.parent

    def resolve_path(value: str) -> Path:
        candidate = Path(value)
        return candidate if candidate.is_absolute() else workspace_root / candidate

    llm_data = data.get("llm", {})
    batching_data = data.get("batching", {})

    return CatalogoConfig(
        schema_root=resolve_path(data["schema_root"]),
        organizational_context_path=resolve_path(data["organizational_context_path"]),
        schemas=list(data.get("schemas", [])),
        batching=BatchingConfig(
            max_batch_bytes=int(batching_data.get("max_batch_bytes", 60000)),
        ),
        llm=LLMConfig(
            enabled=bool(llm_data.get("enabled", True)),
            api_type=str(llm_data.get("api_type", "azure")),
            api_key_env=str(llm_data.get("api_key_env", "")),
            api_key_keyring_service=str(llm_data.get("api_key_keyring_service", "")),
            api_key_keyring_username=str(llm_data.get("api_key_keyring_username", "")),
            require_api_key=bool(llm_data.get("require_api_key", True)),
            model=str(llm_data.get("model", "gpt-4o-mini")),
            base_url=str(llm_data.get("base_url", "")),
            api_version=str(llm_data.get("api_version", "")),
            timeout_seconds=int(llm_data.get("timeout_seconds", 180)),
            temperature=float(llm_data.get("temperature", 0.1)),
            max_output_tokens=int(llm_data.get("max_output_tokens", 8000)),
        ),
    )
