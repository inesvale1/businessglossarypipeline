from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT_EXTENSIONS = (".java",)

# top-level list key -> fields that form its natural dedupe key when merging
# results from multiple subpackage runs into one sources_context_<esquema>.json
_LIST_KEYS_DEDUPE_FIELDS: dict[str, tuple[str, ...]] = {
    "enumeracoes": ("pacote", "classe", "constante"),
    "regras_negocio": ("classe", "metodo", "descricao"),
    "dtos_entrada": ("classe",),
    "dtos_saida": ("classe",),
    "validadores": ("classe", "campo_validado"),
    "tabelas_sql": ("tabela",),
}


@dataclass
class SourceFile:
    relative_path: str
    content: str


def collect_source_files(
    source_dir: Path,
    extensions: tuple[str, ...] = _DEFAULT_EXTENSIONS,
    max_total_bytes: int = 400_000,
) -> list[SourceFile]:
    """Walk `source_dir` collecting text files with the given extensions, stopping
    once `max_total_bytes` of content has been gathered.

    Point --source-dir at a single subpackage (not the whole repo) when the
    codebase is large for one LLM call -- the same "one specialized reader per
    subpackage" split already used manually for this project -- and use
    generate_sources_context.py --merge to combine the runs into one
    sources_context_<esquema>.json.
    """
    files: list[SourceFile] = []
    total_bytes = 0
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        size = len(text.encode("utf-8"))
        if total_bytes + size > max_total_bytes and files:
            break
        files.append(SourceFile(relative_path=str(path.relative_to(source_dir)), content=text))
        total_bytes += size
    return files


_FENCE_LANGUAGE_BY_EXTENSION = {".java": "java", ".sql": "sql", ".ts": "typescript"}


def _fence_language(relative_path: str) -> str:
    suffix = Path(relative_path).suffix.lower()
    return _FENCE_LANGUAGE_BY_EXTENSION.get(suffix, "")


def build_prompt(
    template: str,
    system_name: str,
    package_root: str,
    source_files: list[SourceFile],
    output_schema: dict[str, Any],
) -> tuple[str, str]:
    """Fill a prompts/prompt0_sources_context.txt-style template and split it into
    (system_prompt, user_prompt), the shape core/llm_client.py expects.
    """
    listing = "\n\n".join(
        f"### Arquivo: {f.relative_path}\n```{_fence_language(f.relative_path)}\n{f.content}\n```"
        for f in source_files
    )
    user_prompt = (
        template
        .replace("__SYSTEM_NAME__", system_name)
        .replace("__PACKAGE_ROOT__", package_root)
        .replace("__SOURCE_FILES__", listing)
        .replace("__OUTPUT_SCHEMA_JSON__", json.dumps(output_schema, ensure_ascii=False, indent=2))
    )
    system_prompt = (
        "Você é especialista em Engenharia de Software, Governança de Dados e domínios "
        "fazendários. Responda somente com JSON válido, sem texto antes ou depois."
    )
    return system_prompt, user_prompt


def merge_sources_context(existing: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """Fold one subpackage's extraction result into the schema's accumulated
    sources_context, deduplicating list items by their natural key. Mirrors the
    "read one subpackage at a time, then merge" workflow already used manually.
    """
    if existing is None:
        merged: dict[str, Any] = {
            "sistema": new.get("sistema", ""),
            "pacote_raiz": new.get("pacote_raiz", ""),
            "fonte": new.get("fonte", ""),
        }
        for key in _LIST_KEYS_DEDUPE_FIELDS:
            merged[key] = []
    else:
        merged = dict(existing)
        merged["sistema"] = merged.get("sistema") or new.get("sistema", "")
        merged["pacote_raiz"] = merged.get("pacote_raiz") or new.get("pacote_raiz", "")
        merged["fonte"] = new.get("fonte") or merged.get("fonte", "")

    for key, dedupe_fields in _LIST_KEYS_DEDUPE_FIELDS.items():
        current = list(merged.get(key, []))
        seen = {
            tuple(str(item.get(field, "")) for field in dedupe_fields)
            for item in current
            if isinstance(item, dict)
        }
        for item in new.get(key, []) or []:
            if not isinstance(item, dict):
                continue
            dedupe_key = tuple(str(item.get(field, "")) for field in dedupe_fields)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            current.append(item)
        merged[key] = current

    merged["gerado_em"] = datetime.now(timezone.utc).isoformat()
    merged["resumo"] = {
        "classificados": {f"total_{key}": len(merged.get(key, [])) for key in _LIST_KEYS_DEDUPE_FIELDS}
    }
    return merged
