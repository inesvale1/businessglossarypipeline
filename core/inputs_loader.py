from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SchemaInputs:
    schema_name: str
    system_name: str
    metadata_context: dict[str, Any]
    sources_context: dict[str, Any] | None
    organizational_context: dict[str, Any]


def _read_json(path: Path, encoding: str = "utf-8-sig") -> dict[str, Any]:
    with path.open("r", encoding=encoding) as handle:
        return json.load(handle)


def load_organizational_context(path: Path) -> dict[str, Any]:
    return _read_json(path)


def load_schema_inputs(
    schema_root: Path,
    schema_name: str,
    organizational_context: dict[str, Any],
) -> SchemaInputs:
    inputs_dir = schema_root / schema_name / "inputs"

    metadata_context_path = inputs_dir / f"metadata_context_{schema_name}.json"
    if not metadata_context_path.exists():
        raise FileNotFoundError(
            f"metadata_context_{schema_name}.json not found under {inputs_dir}. "
            "Run the dataquality metadata-context build for this schema first."
        )
    metadata_context = _read_json(metadata_context_path)

    sources_context_path = inputs_dir / f"sources_context_{schema_name}.json"
    sources_context = _read_json(sources_context_path) if sources_context_path.exists() else None

    system_name = ""
    if sources_context:
        system_name = str(sources_context.get("sistema", "")).strip()
    if not system_name:
        system_name = schema_name

    return SchemaInputs(
        schema_name=schema_name,
        system_name=system_name,
        metadata_context=metadata_context,
        sources_context=sources_context,
        organizational_context=organizational_context,
    )
