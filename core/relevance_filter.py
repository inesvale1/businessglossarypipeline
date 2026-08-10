from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

# High-confidence noise: never useful for enumeracoes/regras_negocio/dtos/
# validadores/tabelas_sql, regardless of extension. Matched against the
# lowercased path with a leading "/" (so "*/test/*" also matches a path that
# starts with "test/").
DEFAULT_EXCLUDE_PATTERNS: tuple[str, ...] = (
    "*/test/*", "*/tests/*", "*/src/test/*", "*/__tests__/*",
    "*/target/*", "*/build/*", "*/out/*", "*/dist/*",
    "*/.git/*", "*/.mvn/*", "*/.idea/*", "*/.vscode/*", "*/.settings/*",
    "*/generated*/*", "*/generated-sources/*", "*generated*",
    "*/node_modules/*",
    "*/resources/static/*", "*/resources/templates/*", "*/resources/public/*",
)

DEFAULT_ALLOWED_EXTENSIONS: tuple[str, ...] = (".java", ".sql")

# Opt-in (strict_include=True): a file must ALSO contain one of these
# substrings in its path to be kept. Narrows a very large repo further, at
# the real risk of missing business logic hiding in a class whose name
# doesn't hint at it (e.g. "DocumentosAlteradosRouter.java", which is a
# genuine regra_negocio source but wouldn't obviously match "rule"/"regra").
# Off by default for that reason -- prefer the exclude-only pass unless the
# repo is too large for that to be practical.
DEFAULT_INCLUDE_KEYWORDS: tuple[str, ...] = (
    "enum", "dto", "entrada", "saida", "saída", "request", "response",
    "valid", "rule", "regra", "router", "service", "manager",
    "repository", "model", "entity", "controller", "resource", "rest",
)


@dataclass(frozen=True)
class RelevanceFilterConfig:
    allowed_extensions: tuple[str, ...] = DEFAULT_ALLOWED_EXTENSIONS
    exclude_patterns: tuple[str, ...] = DEFAULT_EXCLUDE_PATTERNS
    strict_include: bool = False
    include_keywords: tuple[str, ...] = DEFAULT_INCLUDE_KEYWORDS


@dataclass(frozen=True)
class FilterResult:
    kept: list[tuple[str, int]] = field(default_factory=list)
    discarded: list[tuple[str, int, str]] = field(default_factory=list)  # (path, size, reason)


def filter_relevant_paths(files: list[tuple[str, int]], config: RelevanceFilterConfig | None = None) -> FilterResult:
    """Split `files` (path, size_bytes) -- as returned by
    git_source.list_tracked_files_with_size, i.e. names only, no content read
    yet -- into kept vs. discarded, using only the path text.

    This is the cheap pass: it runs before any file content is fetched, so a
    large monorepo doesn't cost bandwidth or LLM tokens on files that are
    obviously noise (tests, build output, static assets) or, in strict mode,
    files whose name doesn't hint at business logic at all.
    """
    cfg = config or RelevanceFilterConfig()
    result = FilterResult()

    for raw_path, size in files:
        lower = "/" + raw_path.replace("\\", "/").lower()

        if not lower.endswith(cfg.allowed_extensions):
            result.discarded.append((raw_path, size, "extensao"))
            continue

        excluded_by = next((p for p in cfg.exclude_patterns if fnmatch.fnmatch(lower, p.lower())), None)
        if excluded_by:
            result.discarded.append((raw_path, size, f"excluido por {excluded_by!r}"))
            continue

        if cfg.strict_include and not any(keyword in lower for keyword in cfg.include_keywords):
            result.discarded.append((raw_path, size, "sem palavra-chave (strict_include)"))
            continue

        result.kept.append((raw_path, size))

    return result


def group_into_batches(files: list[tuple[str, int]], max_batch_bytes: int) -> list[list[str]]:
    """Greedily group (path, size) pairs into batches whose total size stays
    under `max_batch_bytes`, so each LLM call gets a coherent, budget-sized
    slice instead of the whole (filtered) file list at once. A single file
    larger than the budget gets its own oversized batch rather than being
    dropped.
    """
    batches: list[list[str]] = []
    current: list[str] = []
    current_size = 0

    for path, size in files:
        if current and current_size + size > max_batch_bytes:
            batches.append(current)
            current = []
            current_size = 0
        current.append(path)
        current_size += size

    if current:
        batches.append(current)

    return batches
