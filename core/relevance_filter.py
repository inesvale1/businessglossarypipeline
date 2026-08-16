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

# .ts covers TypeScript/Angular front-end repos (e.g. cadastro-web): client-side
# validators there are a genuine business-rule signal (CPF/CNPJ/format/length
# checks mirroring the backend), not noise -- unlike .html/.css, which stay
# excluded.
DEFAULT_ALLOWED_EXTENSIONS: tuple[str, ...] = (".java", ".sql", ".ts")

# For sources_repos.<schema> entries with content_type="docs" (business
# vision/requirements/use-case documents, not source code) -- see
# core/document_text.py for which of these actually get parsed (.doc, the
# legacy binary Word format, is excluded on purpose: no reliable stdlib-only
# text extraction for it; .md/.mdx cover docs-as-code repos, e.g. Astro/
# Starlight sites).
DOCUMENT_EXTENSIONS: tuple[str, ...] = (".odt", ".docx", ".pdf", ".md", ".mdx")

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

# Any path *directory* segment containing one of these substrings (case
# insensitive) is dropped, regardless of position -- broader than
# DEFAULT_EXCLUDE_PATTERNS' exact-segment "*/test/*"/"*/tests/*" (which would
# miss e.g. "testeIntegracao/", "ContestacaoTeste/"). Deliberately substring,
# not exact-match, per explicit request to cut LLM token spend even at the
# cost of an occasional false positive (e.g. a real "Attestation" folder).
DEFAULT_EXCLUDE_DIRNAME_SUBSTRINGS: tuple[str, ...] = ("test",)

# Files whose most recent commit is older than this are dropped as stale --
# reduces both git-listing noise and LLM token spend on code that's unlikely
# to reflect the system's current business rules. None disables the check
# (kept when no commit-date info is available for a path).
DEFAULT_MAX_FILE_AGE_DAYS: int | None = 365


@dataclass(frozen=True)
class RelevanceFilterConfig:
    allowed_extensions: tuple[str, ...] = DEFAULT_ALLOWED_EXTENSIONS
    exclude_patterns: tuple[str, ...] = DEFAULT_EXCLUDE_PATTERNS
    strict_include: bool = False
    include_keywords: tuple[str, ...] = DEFAULT_INCLUDE_KEYWORDS
    exclude_dirname_substrings: tuple[str, ...] = DEFAULT_EXCLUDE_DIRNAME_SUBSTRINGS
    max_file_age_days: int | None = DEFAULT_MAX_FILE_AGE_DAYS


@dataclass(frozen=True)
class FilterResult:
    kept: list[tuple[str, int]] = field(default_factory=list)
    discarded: list[tuple[str, int, str]] = field(default_factory=list)  # (path, size, reason)


def filter_relevant_paths(
    files: list[tuple[str, int]],
    config: RelevanceFilterConfig | None = None,
    recently_touched: dict[str, str] | None = None,
) -> FilterResult:
    """Split `files` (path, size_bytes) -- as returned by
    git_source.list_tracked_files_with_size, i.e. names only, no content read
    yet -- into kept vs. discarded, using only the path text (and, if
    `recently_touched` is given, whether each path was touched recently).

    This is the cheap pass: it runs before any file content is fetched, so a
    large monorepo doesn't cost bandwidth or LLM tokens on files that are
    obviously noise (tests, build output, static assets, stale/unmaintained
    code) or, in strict mode, files whose name doesn't hint at business logic
    at all.

    `recently_touched`: path -> ISO-8601 date of that path's most recent
    commit *within the configured age window*, as returned by
    git_source.list_recently_touched_paths(..., since_days=cfg.max_file_age_days).
    A path absent from this dict is treated as stale (git reported no commit
    for it within the window) and dropped -- unless `recently_touched` itself
    is None, meaning the caller didn't run the age lookup at all (e.g.
    max_file_age_days is disabled), in which case every path is kept
    regardless of age.
    """
    cfg = config or RelevanceFilterConfig()
    result = FilterResult()
    age_filter_active = cfg.max_file_age_days is not None and recently_touched is not None

    for raw_path, size in files:
        normalized = raw_path.replace("\\", "/")
        lower = "/" + normalized.lower()

        if not lower.endswith(cfg.allowed_extensions):
            result.discarded.append((raw_path, size, "extensao"))
            continue

        dir_segments = normalized.lower().split("/")[:-1]
        matched_substring = next(
            (s for s in cfg.exclude_dirname_substrings if any(s in segment for segment in dir_segments)),
            None,
        )
        if matched_substring:
            result.discarded.append((raw_path, size, f"pasta contem {matched_substring!r}"))
            continue

        excluded_by = next((p for p in cfg.exclude_patterns if fnmatch.fnmatch(lower, p.lower())), None)
        if excluded_by:
            result.discarded.append((raw_path, size, f"excluido por {excluded_by!r}"))
            continue

        if age_filter_active and raw_path not in recently_touched and normalized not in recently_touched:
            result.discarded.append(
                (raw_path, size, f"desatualizado (sem commit nos ultimos {cfg.max_file_age_days} dias)")
            )
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
