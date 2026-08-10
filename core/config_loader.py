from __future__ import annotations

import json
from dataclasses import dataclass, field
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
class SourceRepoConfig:
    git_url: str
    ref: str = "main"
    username: str | None = None
    token_keyring_service: str | None = None
    token_keyring_username: str | None = None
    # Optional path prefixes (e.g. "receita-core/src/main/java/.../enums") to
    # scope the file listing to before the relevance filter runs. Leave empty
    # to scan the whole repository -- the listing itself is cheap (no file
    # content is downloaded for it), so this is about narrowing scope /
    # matching a known module layout, not about avoiding bandwidth.
    subpackages: list[str] = field(default_factory=list)
    # See core/relevance_filter.py RelevanceFilterConfig.strict_include.
    strict_include: bool = False


@dataclass(frozen=True)
class CatalogoConfig:
    schema_root: Path
    organizational_context_path: Path
    schemas: list[str]
    batching: BatchingConfig
    llm: LLMConfig
    checkouts_root: Path
    sources_repos: dict[str, SourceRepoConfig]


def load_config(path: str | Path) -> CatalogoConfig:
    config_path = Path(path).resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    # config/catalogo.config.json -> <projeto>/ -> Implementation/
    workspace_root = config_path.parent.parent.parent
    project_root = config_path.parent.parent

    def resolve_path(value: str) -> Path:
        candidate = Path(value)
        return candidate if candidate.is_absolute() else workspace_root / candidate

    llm_data = data.get("llm", {})
    batching_data = data.get("batching", {})
    sources_repos_data = data.get("sources_repos", {}) or {}

    checkouts_root_raw = str(data.get("checkouts_root", ".checkouts"))
    checkouts_root = Path(checkouts_root_raw)
    if not checkouts_root.is_absolute():
        checkouts_root = project_root / checkouts_root

    sources_repos = {
        schema_name: SourceRepoConfig(
            git_url=str(repo_data["git_url"]),
            ref=str(repo_data.get("ref", "main")),
            username=repo_data.get("username"),
            token_keyring_service=repo_data.get("token_keyring_service"),
            token_keyring_username=repo_data.get("token_keyring_username"),
            subpackages=list(repo_data.get("subpackages", [])),
            strict_include=bool(repo_data.get("strict_include", False)),
        )
        for schema_name, repo_data in sources_repos_data.items()
    }

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
        checkouts_root=checkouts_root,
        sources_repos=sources_repos,
    )
