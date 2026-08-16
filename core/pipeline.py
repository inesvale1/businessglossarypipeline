from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .batcher import Batch, build_batches
from .config_loader import CatalogoConfig
from .consolidator import (
    ALL_LIST_KEYS,
    apply_merge_decisions,
    build_compact_view,
    dedupe_evidence,
    find_dangling_references,
    find_missing_tables,
    structural_merge,
)
from .inputs_loader import SchemaInputs, load_organizational_context, load_schema_inputs
from .llm_client import LLMClient
from .output_writer import write_timestamped_copy
from .prompt_builder import build_batch_prompt, build_merge_decision_prompt

_ID_LIST_KEYS = (
    "official_macroprocesses",
    "functional_capabilities_inferred",
    "business_concepts",
    "business_rules",
    "value_domains",
    "concept_relationships",
    "external_references",
    "technical_assets",
    "evidence_index",
    "conflicts",
)


@dataclass
class SchemaResult:
    schema_name: str
    status: str  # "ok" | "skipped" | "error"
    detail: str = ""
    output_path: Path | None = None


def run(config: CatalogoConfig) -> list[SchemaResult]:
    llm = LLMClient.from_config(config.llm)
    organizational_context = load_organizational_context(config.organizational_context_path)

    return [run_schema(config, llm, schema_name, organizational_context) for schema_name in config.schemas]


def run_schema(
    config: CatalogoConfig,
    llm: LLMClient,
    schema_name: str,
    organizational_context: dict[str, Any],
) -> SchemaResult:
    if not llm.enabled:
        return SchemaResult(schema_name, "skipped", f"LLM disabled ({llm.disabled_reason})")

    try:
        inputs = load_schema_inputs(config.schema_root, schema_name, organizational_context)
    except FileNotFoundError as exc:
        return SchemaResult(schema_name, "error", str(exc))

    batches = build_batches(inputs.metadata_context, inputs.sources_context, config.batching.max_batch_bytes)
    if not batches:
        return SchemaResult(schema_name, "error", "Nenhuma tabela encontrada em metadata_context.")

    partial_results: list[dict[str, Any]] = []
    for batch in batches:
        system_prompt, user_prompt = build_batch_prompt(inputs, batch, len(batches))
        result = llm.complete_json(system_prompt, user_prompt)
        if result is None:
            return SchemaResult(
                schema_name, "error",
                f"Lote {batch.index + 1}/{len(batches)} falhou: {llm.last_error}",
            )
        result = _fill_missing_tables(llm, inputs, batch, result, len(batches))
        partial_results.append(result)

    if len(partial_results) == 1:
        final_document = partial_results[0]
    else:
        final_document = structural_merge(partial_results)
        final_document = dedupe_evidence(final_document)
        compact_view = build_compact_view(final_document)
        system_prompt, user_prompt = build_merge_decision_prompt(inputs, compact_view)
        decisions = llm.complete_json(system_prompt, user_prompt)
        if decisions is None:
            return SchemaResult(schema_name, "error", f"Decisao de fusao falhou: {llm.last_error}")
        final_document = apply_merge_decisions(final_document, decisions)

    all_table_names = [name for batch in batches for name in batch.table_names]
    missing_tables = find_missing_tables(all_table_names, final_document)
    table_warnings = (
        [f"Tabelas sem technical_asset correspondente no documento final: {missing_tables}"]
        if missing_tables
        else []
    )

    for warning in _find_duplicate_ids(final_document) + find_dangling_references(final_document) + table_warnings:
        final_document.setdefault("context_metadata", {}).setdefault("warnings", []).append(warning)

    output_path = config.schema_root / schema_name / "outputs" / f"local_canonical_context_{schema_name}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(final_document, ensure_ascii=False, indent=2)
    output_path.write_text(content, encoding="utf-8")
    write_timestamped_copy(output_path, content)

    return SchemaResult(schema_name, "ok", f"{len(batches)} lote(s)", output_path)


def _fill_missing_tables(
    llm: LLMClient,
    inputs: SchemaInputs,
    batch: Batch,
    result: dict[str, Any],
    batch_total: int,
) -> dict[str, Any]:
    """Retry, once, for tables a batch was given but silently left out.

    prompt1_batch.txt now asks for one technical_asset per table, but LLM
    instruction-following isn't guaranteed. Rather than only reporting the
    gap (find_missing_tables, called on the final document), make one
    focused follow-up call scoped to just the missing tables -- a much
    smaller ask, which the model is far more likely to complete fully -- and
    fold whatever it returns into this batch's own result before it is
    merged with the rest.
    """
    missing = find_missing_tables(batch.table_names, result)
    if not missing:
        return result

    missing_upper = {name.strip().upper() for name in missing}
    gap_batch = Batch(
        index=batch.index,
        table_names=[str(t.get("table_name", "")) for t in batch.tables if str(t.get("table_name", "")).strip().upper() in missing_upper],
        tables=[t for t in batch.tables if str(t.get("table_name", "")).strip().upper() in missing_upper],
        matched_sources=batch.matched_sources,
    )

    system_prompt, user_prompt = build_batch_prompt(inputs, gap_batch, batch_total)
    gap_result = llm.complete_json(system_prompt, user_prompt)
    if gap_result is None:
        return result

    for key in ALL_LIST_KEYS:
        result.setdefault(key, [])
        result[key].extend(gap_result.get(key, []) or [])

    return result


def _find_duplicate_ids(document: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for key in _ID_LIST_KEYS:
        items = document.get(key, [])
        if not isinstance(items, list):
            continue
        ids = [item.get("id") for item in items if isinstance(item, dict) and item.get("id")]
        duplicates = sorted({item_id for item_id in ids if ids.count(item_id) > 1})
        if duplicates:
            warnings.append(f"IDs duplicados em {key}: {duplicates}")
    return warnings
