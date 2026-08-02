from __future__ import annotations

import copy
from typing import Any

_DIRECT_CONCAT_KEYS = (
    "technical_assets",
    "business_rules",
    "concept_relationships",
    "functional_capabilities_inferred",
    "quality_warnings",
    "business_concepts",
    "value_domains",
    "external_references",
    "siglas_e_abreviaturas",
    "conflicts",
    "evidence_index",
)

# All top-level list fields of the Contexto Local Canonico (see
# prompts/output_schema.json), for callers that need to merge a whole batch
# response generically (e.g. folding a gap-fill retry into its parent batch).
ALL_LIST_KEYS = _DIRECT_CONCAT_KEYS + ("official_macroprocesses",)

# Categories where each batch assigns ids independently, so the same id
# string from two batches (e.g. both picking "BR-001" for their first rule)
# is a coincidental collision, not the same entity. official_macroprocesses
# is excluded on purpose: batches share the same organizational context, so
# a repeated id there is meant to be the same macroprocess (merged by exact
# id in structural_merge instead of renamed).
_ID_CATEGORIES_TO_UNIQUIFY = (
    "technical_assets",
    "business_concepts",
    "business_rules",
    "value_domains",
    "concept_relationships",
    "evidence_index",
    "external_references",
    "functional_capabilities_inferred",
)

# key -> list-valued fields to union when two entries of that category are merged.
_MERGE_LIST_FIELDS: dict[str, tuple[str, ...]] = {
    "business_concepts": (
        "original_names", "synonym_candidates", "related_capabilities",
        "official_macroprocesses", "technical_assets", "business_rules",
        "value_domains", "external_references", "evidence",
    ),
    "value_domains": ("values", "used_by", "evidence"),
    "official_macroprocesses": (
        "associated_concepts", "associated_capabilities", "associated_rules", "evidence",
    ),
    "external_references": ("local_concepts", "evidence"),
}


def structural_merge(partials: list[dict[str, Any]]) -> dict[str, Any]:
    """Concatenate list fields from all batch partials into one document.

    Safe because batches partition tables without overlap (core/batcher.py),
    so nothing here needs semantic dedup yet -- except official_macroprocesses,
    which different batches can legitimately re-emit with the same id (they
    all see the same organizational context), so those are pre-merged by
    exact id here per the original consolidation instructions.
    """
    document: dict[str, Any] = {key: [] for key in _DIRECT_CONCAT_KEYS}
    document["official_macroprocesses"] = []

    warnings: list[str] = []
    context_metadata = copy.deepcopy(partials[0].get("context_metadata", {})) if partials else {}
    used_ids_by_category: dict[str, set[str]] = {}

    for batch_index, raw_partial in enumerate(partials):
        partial = _uniquify_partial_ids(copy.deepcopy(raw_partial), batch_index, used_ids_by_category)
        for key in _DIRECT_CONCAT_KEYS:
            document[key].extend(partial.get(key, []) or [])
        document["official_macroprocesses"].extend(partial.get("official_macroprocesses", []) or [])
        for warning in (partial.get("context_metadata", {}) or {}).get("warnings", []) or []:
            if warning not in warnings:
                warnings.append(warning)

    document["official_macroprocesses"] = _merge_by_exact_id(
        document["official_macroprocesses"], _MERGE_LIST_FIELDS["official_macroprocesses"]
    )

    context_metadata["warnings"] = warnings
    document["context_metadata"] = context_metadata
    return document


def _uniquify_partial_ids(
    partial: dict[str, Any], batch_index: int, used_ids_by_category: dict[str, set[str]]
) -> dict[str, Any]:
    """Rename ids that collide with an id already used by an earlier batch.

    Each batch's own cross-references (e.g. business_rules[].concepts) only
    ever point at ids from that same batch, so renaming a partial's ids and
    remapping references within that same partial keeps it self-consistent.
    """
    for category in _ID_CATEGORIES_TO_UNIQUIFY:
        used_ids = used_ids_by_category.setdefault(category, set())
        local_remap: dict[str, str] = {}
        for item in partial.get(category, []) or []:
            item_id = item.get("id")
            if not item_id:
                continue
            if item_id in used_ids:
                new_id = _make_unique_id(item_id, batch_index, used_ids)
                local_remap[item_id] = new_id
                item["id"] = new_id
                used_ids.add(new_id)
            else:
                used_ids.add(item_id)
        if local_remap:
            partial = remap_ids(partial, local_remap)
    return partial


def _make_unique_id(item_id: str, batch_index: int, used_ids: set[str]) -> str:
    candidate = f"{item_id}-B{batch_index + 1}"
    suffix = 2
    while candidate in used_ids:
        candidate = f"{item_id}-B{batch_index + 1}-{suffix}"
        suffix += 1
    return candidate


def _merge_by_exact_id(items: list[dict[str, Any]], list_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        item_id = item.get("id")
        if not item_id:
            continue
        if item_id not in merged:
            merged[item_id] = copy.deepcopy(item)
            order.append(item_id)
            continue
        _union_list_fields(merged[item_id], item, list_fields)
    return [merged[item_id] for item_id in order]


def _union_list_fields(keep: dict[str, Any], other: dict[str, Any], list_fields: tuple[str, ...]) -> None:
    for field_name in list_fields:
        keep_values = keep.get(field_name)
        other_values = other.get(field_name)
        if not isinstance(keep_values, list) or not isinstance(other_values, list):
            continue
        for value in other_values:
            if value not in keep_values:
                keep_values.append(value)


def dedupe_evidence(document: dict[str, Any]) -> dict[str, Any]:
    """Structurally dedupe evidence_index entries with identical content."""
    evidence_index = document.get("evidence_index", [])
    seen: dict[tuple[Any, ...], str] = {}
    remap: dict[str, str] = {}
    deduped: list[dict[str, Any]] = []

    for entry in evidence_index:
        entry_id = entry.get("id")
        content_key = (
            entry.get("source_type"),
            entry.get("source_file"),
            entry.get("technical_location"),
            entry.get("summary"),
        )
        if content_key in seen:
            if entry_id:
                remap[entry_id] = seen[content_key]
            continue
        if entry_id:
            seen[content_key] = entry_id
        deduped.append(entry)

    document["evidence_index"] = deduped
    if remap:
        document = remap_ids(document, remap)
    return document


def remap_ids(document: dict[str, Any], remap: dict[str, str]) -> dict[str, Any]:
    """Rewrite reference fields that point at a remapped id.

    Never touches a literal `"id"` key: that field always declares an item's
    own identity, set directly by whoever assigns it, and must never be
    overwritten by this generic string substitution. This matters when the
    same original id string is shared by more than one item within a single
    partial (e.g. a batch's own output and its gap-fill retry both minting
    "FC-X" independently): only the *last* rename for that shared string
    survives in `remap` (the caller's dict is keyed by old id), so if `walk`
    rewrote "id" fields too, it could clobber an unrelated item's already
    -- correctly assigned id with that last target, producing a duplicate.
    Excluding "id" makes every renamed item's own id immune to this, no
    matter how many other items once shared its original string.
    """
    if not remap:
        return document

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: (value if key == "id" else walk(value)) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(value) for value in node]
        if isinstance(node, str):
            return remap.get(node, node)
        return node

    return walk(document)


_COMPACT_FIELDS = {
    "business_concepts": ("id", "preferred_name", "original_names", "synonym_candidates", "type"),
    "value_domains": ("id", "name", "description", "values"),
    "official_macroprocesses": ("id", "name"),
    "external_references": ("id", "name", "external_system", "external_domain"),
}


def build_compact_view(document: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, fields in _COMPACT_FIELDS.items():
        compact[key] = [
            {field_name: item.get(field_name) for field_name in fields}
            for item in document.get(key, [])
        ]
    compact["siglas_e_abreviaturas"] = [
        {"sigla": item.get("sigla"), "forma_extensa": item.get("forma_extensa")}
        for item in document.get("siglas_e_abreviaturas", [])
    ]
    return compact


_MERGE_DECISION_CATEGORIES = (
    ("business_concepts_merges", "business_concepts", "id"),
    ("value_domains_merges", "value_domains", "id"),
    ("official_macroprocesses_merges", "official_macroprocesses", "id"),
    ("external_references_merges", "external_references", "id"),
)


def apply_merge_decisions(document: dict[str, Any], decisions: dict[str, Any]) -> dict[str, Any]:
    id_remap: dict[str, str] = {}

    for decision_key, doc_key, id_field in _MERGE_DECISION_CATEGORIES:
        groups = decisions.get(decision_key) or []
        items = document.get(doc_key, [])
        by_id = {item.get(id_field): item for item in items if item.get(id_field)}
        list_fields = _MERGE_LIST_FIELDS.get(doc_key, ())

        for group in groups:
            keep_id = group.get("keep_id")
            merge_ids = group.get("merge_ids") or []
            keep_item = by_id.get(keep_id)
            if keep_item is None:
                continue
            for merge_id in merge_ids:
                if merge_id == keep_id:
                    continue
                merged_item = by_id.get(merge_id)
                if merged_item is None:
                    continue
                _union_list_fields(keep_item, merged_item, list_fields)
                id_remap[merge_id] = keep_id

        document[doc_key] = [item for item in items if item.get(id_field) not in id_remap]

    _apply_siglas_merges(document, decisions.get("siglas_merges") or [])

    if id_remap:
        document = remap_ids(document, id_remap)

    for new_conflict in decisions.get("new_conflicts") or []:
        document.setdefault("conflicts", []).append(new_conflict)

    return document


def find_missing_tables(expected_table_names: list[str], document: dict[str, Any]) -> list[str]:
    """Report tables that went into a batch but have no technical_asset in the output.

    Each batch is supposed to emit exactly one technical_asset per table it was
    given (prompt1_batch.txt), but nothing enforces that -- the LLM can simply
    skip a table. Since structural_merge only concatenates whatever batches
    returned, that kind of silent data loss needs to be caught explicitly.
    """
    produced = {
        str(item.get("name", "")).strip().upper()
        for item in document.get("technical_assets", [])
        if isinstance(item, dict)
    }
    missing = sorted({name for name in expected_table_names if name.strip().upper() not in produced})
    return missing


def _apply_siglas_merges(document: dict[str, Any], groups: list[dict[str, Any]]) -> None:
    siglas = document.get("siglas_e_abreviaturas", [])
    by_sigla = {item.get("sigla"): item for item in siglas if item.get("sigla")}
    dropped: set[str] = set()

    for group in groups:
        keep_sigla = group.get("keep_sigla")
        merge_siglas = group.get("merge_siglas") or []
        keep_item = by_sigla.get(keep_sigla)
        if keep_item is None:
            continue
        for merge_sigla in merge_siglas:
            if merge_sigla == keep_sigla:
                continue
            merged_item = by_sigla.get(merge_sigla)
            if merged_item is None:
                continue
            if not keep_item.get("forma_extensa") and merged_item.get("forma_extensa"):
                keep_item["forma_extensa"] = merged_item["forma_extensa"]
            for value in merged_item.get("fonte", []) or []:
                keep_item.setdefault("fonte", [])
                if value not in keep_item["fonte"]:
                    keep_item["fonte"].append(value)
            dropped.add(merge_sigla)

    document["siglas_e_abreviaturas"] = [item for item in siglas if item.get("sigla") not in dropped]


# (source_list, source_field, target_list) -- explicit, since field names like
# "official_macroprocesses" mean different things depending on where they appear
# (declaring an id vs. referencing one), so generic name-matching is unsafe.
_CROSS_REFERENCES = (
    ("business_concepts", "related_capabilities", "functional_capabilities_inferred"),
    ("business_concepts", "official_macroprocesses", "official_macroprocesses"),
    ("business_concepts", "technical_assets", "technical_assets"),
    ("business_concepts", "business_rules", "business_rules"),
    ("business_concepts", "value_domains", "value_domains"),
    ("business_concepts", "external_references", "external_references"),
    ("business_concepts", "evidence", "evidence_index"),
    ("official_macroprocesses", "associated_concepts", "business_concepts"),
    ("official_macroprocesses", "associated_capabilities", "functional_capabilities_inferred"),
    ("official_macroprocesses", "associated_rules", "business_rules"),
    ("official_macroprocesses", "evidence", "evidence_index"),
    ("functional_capabilities_inferred", "concepts", "business_concepts"),
    ("functional_capabilities_inferred", "technical_assets", "technical_assets"),
    ("functional_capabilities_inferred", "official_macroprocesses", "official_macroprocesses"),
    ("functional_capabilities_inferred", "evidence", "evidence_index"),
    ("business_rules", "concepts", "business_concepts"),
    ("business_rules", "evidence", "evidence_index"),
    ("external_references", "local_concepts", "business_concepts"),
    ("external_references", "evidence", "evidence_index"),
    ("technical_assets", "business_concepts", "business_concepts"),
    ("value_domains", "evidence", "evidence_index"),
)


def find_dangling_references(document: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    valid_ids_by_list = {
        key: {item.get("id") for item in document.get(key, []) if isinstance(item, dict) and item.get("id")}
        for key in ALL_LIST_KEYS
    }

    for source_list, source_field, target_list in _CROSS_REFERENCES:
        valid_ids = valid_ids_by_list.get(target_list, set())
        dangling: set[str] = set()
        for item in document.get(source_list, []):
            if not isinstance(item, dict):
                continue
            for candidate in item.get(source_field, []) or []:
                if isinstance(candidate, str) and candidate and candidate not in valid_ids:
                    dangling.add(candidate)
        if dangling:
            warnings.append(f"{source_list}.{source_field} aponta para id(s) inexistente(s) em {target_list}: {sorted(dangling)}")

    business_concept_ids = valid_ids_by_list.get("business_concepts", set())
    dangling_relationships: set[str] = set()
    for relationship in document.get("concept_relationships", []):
        if not isinstance(relationship, dict):
            continue
        source_id = relationship.get("source_concept_id")
        if isinstance(source_id, str) and source_id and source_id not in business_concept_ids:
            dangling_relationships.add(source_id)
        if not relationship.get("external_reference"):
            target_id = relationship.get("target_concept_id")
            if isinstance(target_id, str) and target_id and target_id not in business_concept_ids:
                dangling_relationships.add(target_id)
    if dangling_relationships:
        warnings.append(f"concept_relationships aponta para id(s) inexistente(s) em business_concepts: {sorted(dangling_relationships)}")

    return warnings
