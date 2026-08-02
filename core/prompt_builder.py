from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .batcher import Batch
from .inputs_loader import SchemaInputs

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

_SYSTEM_PROMPT = (
    "Voce retorna exclusivamente JSON valido, sem markdown, sem texto antes ou "
    "depois do JSON. Se nao puder cumprir integralmente a tarefa, ainda assim "
    "retorne o JSON mais completo e coerente possivel, seguindo a estrutura pedida."
)


def _load_template(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


def _output_schema_json() -> str:
    schema = json.loads((_PROMPTS_DIR / "output_schema.json").read_text(encoding="utf-8"))
    return json.dumps(schema, ensure_ascii=False, indent=2)


def build_batch_prompt(inputs: SchemaInputs, batch: Batch, batch_total: int) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) for one batch's Prompt 1 call."""
    template = _load_template("prompt1_batch.txt")

    metadata_context_subset = {
        "schema_name": inputs.metadata_context.get("schema_name"),
        "owner": inputs.metadata_context.get("owner"),
        "db_instance_name": inputs.metadata_context.get("db_instance_name"),
        "tables": batch.tables,
    }

    replacements = {
        "__SYSTEM_NAME_UPPER__": inputs.system_name.upper(),
        "__SCHEMA_NAME_UPPER__": inputs.schema_name.upper(),
        "__BATCH_INDEX__": str(batch.index + 1),
        "__BATCH_TOTAL__": str(batch_total),
        "__METADATA_CONTEXT_JSON__": json.dumps(metadata_context_subset, ensure_ascii=False, indent=2),
        "__SOURCES_CONTEXT_JSON__": json.dumps(batch.matched_sources, ensure_ascii=False, indent=2),
        "__ORGANIZATIONAL_CONTEXT_JSON__": json.dumps(inputs.organizational_context, ensure_ascii=False, indent=2),
        "__OUTPUT_SCHEMA_JSON__": _output_schema_json(),
    }

    user_prompt = template
    for placeholder, value in replacements.items():
        user_prompt = user_prompt.replace(placeholder, value)

    return _SYSTEM_PROMPT, user_prompt


def build_merge_decision_prompt(
    inputs: SchemaInputs,
    compact_view: dict[str, Any],
) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) asking the LLM which items to merge.

    Unlike the old full-document consolidation, this call's output is a small,
    fixed-size list of merge decisions -- the actual merging happens in Python
    (core/consolidator.py), so this never risks truncation regardless of how
    large the schema's generated content is.
    """
    template = _load_template("prompt2_merge_decisions.txt")

    replacements = {
        "__SYSTEM_NAME_UPPER__": inputs.system_name.upper(),
        "__SCHEMA_NAME_UPPER__": inputs.schema_name.upper(),
        "__COMPACT_VIEW_JSON__": json.dumps(compact_view, ensure_ascii=False, indent=2),
    }

    user_prompt = template
    for placeholder, value in replacements.items():
        user_prompt = user_prompt.replace(placeholder, value)

    return _SYSTEM_PROMPT, user_prompt
