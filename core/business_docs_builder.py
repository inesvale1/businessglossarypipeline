from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def build_prompt(
    template: str,
    system_name: str,
    document_path: str,
    document_text: str,
    output_schema: dict[str, Any],
) -> tuple[str, str]:
    """Fill a prompts/prompt0_business_docs.txt-style template for ONE
    document (a whole vision/requirements/use-case doc, unlike the source-code
    path which batches many small files into one call) and split it into
    (system_prompt, user_prompt).
    """
    user_prompt = (
        template
        .replace("__SYSTEM_NAME__", system_name)
        .replace("__DOCUMENT_PATH__", document_path)
        .replace("__DOCUMENT_TEXT__", document_text)
        .replace("__OUTPUT_SCHEMA_JSON__", json.dumps(output_schema, ensure_ascii=False, indent=2))
    )
    system_prompt = (
        "Voce e especialista em Engenharia de Requisitos, Governanca de Dados e dominios "
        "fazendarios. Responda somente com JSON valido, sem texto antes ou depois."
    )
    return system_prompt, user_prompt


def merge_business_docs(existing: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """Fold one document's extraction result into the schema's accumulated
    business_docs_context, deduplicating by `arquivo` (each call covers
    exactly one document, so a rerun over the same file should replace, not
    duplicate, its entry).
    """
    if existing is None:
        merged: dict[str, Any] = {
            "sistema": new.get("sistema", ""),
            "fonte": new.get("fonte", ""),
            "documentos_negocio": [],
        }
    else:
        merged = dict(existing)
        merged["sistema"] = merged.get("sistema") or new.get("sistema", "")
        merged["fonte"] = new.get("fonte") or merged.get("fonte", "")
        merged.setdefault("documentos_negocio", [])

    current = [item for item in merged.get("documentos_negocio", []) if isinstance(item, dict)]
    by_arquivo = {item.get("arquivo"): index for index, item in enumerate(current)}

    for item in new.get("documentos_negocio", []) or []:
        if not isinstance(item, dict):
            continue
        arquivo = item.get("arquivo")
        if arquivo in by_arquivo:
            current[by_arquivo[arquivo]] = item
        else:
            by_arquivo[arquivo] = len(current)
            current.append(item)

    merged["documentos_negocio"] = current
    merged["gerado_em"] = datetime.now(timezone.utc).isoformat()
    merged["resumo"] = {"total_documentos": len(current)}
    return merged
