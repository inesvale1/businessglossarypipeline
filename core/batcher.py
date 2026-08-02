from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# sources_context categories that can plausibly relate to a batch of tables.
# All of them arrive as {"value": [...]} in the real sources_context_<schema>.json
# files, except regras_negocio, which is a plain list.
_SOURCE_CATEGORIES = ("regras_negocio", "enumeracoes", "dtos_entrada", "dtos_saida", "validadores", "tabelas_sql")

_MIN_MATCH_SCORE = 2
_MAX_MATCHES_PER_CATEGORY = 15


@dataclass
class Batch:
    index: int
    table_names: list[str]
    tables: list[dict[str, Any]]
    matched_sources: dict[str, list[Any]] = field(default_factory=dict)


def _tokenize(value: str) -> set[str]:
    return {token for token in re.split(r"[^A-Z0-9]+", str(value).upper()) if len(token) > 2}


def _extract_items(sources_context: dict[str, Any] | None, category: str) -> list[Any]:
    if not sources_context:
        return []
    raw = sources_context.get(category)
    if isinstance(raw, dict):
        raw = raw.get("value", [])
    return raw if isinstance(raw, list) else []


def build_batches(
    metadata_context: dict[str, Any],
    sources_context: dict[str, Any] | None,
    max_batch_bytes: int,
) -> list[Batch]:
    tables = metadata_context.get("tables", [])
    columns_by_table: dict[str, list[Any]] = defaultdict(list)
    for column in metadata_context.get("columns", []):
        columns_by_table[str(column.get("table_name", ""))].append(column)

    enriched: list[tuple[dict[str, Any], int]] = []
    for table_context in tables:
        entry = dict(table_context)
        entry["columns"] = columns_by_table.get(str(entry.get("table_name", "")), [])
        size = len(json.dumps(entry, ensure_ascii=False))
        enriched.append((entry, size))

    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_size = 0
    for entry, size in enriched:
        if current and current_size + size > max_batch_bytes:
            groups.append(current)
            current, current_size = [], 0
        current.append(entry)
        current_size += size
    if current:
        groups.append(current)

    batches: list[Batch] = []
    for index, group in enumerate(groups):
        table_names = [str(t.get("table_name", "")) for t in group]
        batches.append(
            Batch(
                index=index,
                table_names=table_names,
                tables=group,
                matched_sources=_match_sources(table_names, sources_context),
            )
        )
    return batches


def _match_sources(table_names: list[str], sources_context: dict[str, Any] | None) -> dict[str, list[Any]]:
    batch_tokens: set[str] = set()
    for name in table_names:
        batch_tokens |= _tokenize(name)

    matched: dict[str, list[Any]] = {}
    for category in _SOURCE_CATEGORIES:
        items = _extract_items(sources_context, category)
        if not items:
            matched[category] = []
            continue

        if category == "tabelas_sql":
            # Structured field: match the recorded table name directly against
            # the batch instead of a generic text-overlap score.
            upper_names = {name.upper() for name in table_names}
            matched[category] = [
                item for item in items
                if isinstance(item, dict) and str(item.get("tabela", "")).upper() in upper_names
            ][:_MAX_MATCHES_PER_CATEGORY]
            continue

        scored: list[tuple[int, Any]] = []
        for item in items:
            haystack = json.dumps(item, ensure_ascii=False).upper()
            score = sum(1 for token in batch_tokens if token in haystack)
            if score >= _MIN_MATCH_SCORE:
                scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        matched[category] = [item for _, item in scored[:_MAX_MATCHES_PER_CATEGORY]]

    return matched
