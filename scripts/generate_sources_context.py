"""Gera (ou funde em) sources_context_<esquema>.json a partir do codigo-fonte de
um sistema, usando o template em prompts/prompt0_sources_context.txt.

Dois modos:
- --source <pasta>: le uma pasta local ja clonada (um pacote/subpacote por vez,
  use --merge para acumular varias chamadas no mesmo arquivo).
- --schema <nome> sem --source: usa sources_repos.<schema> do
  config/catalogo.config.json. Primeiro lista TODOS os arquivos do repositorio
  remoto sem baixar conteudo nenhum (clone "blobless"), aplica o filtro de
  relevancia por nome/caminho (core/relevance_filter.py), agrupa o que sobrou
  em lotes por tamanho real, e só entao busca o conteudo -- um arquivo de cada
  vez -- apenas dos arquivos que passaram no filtro. Use --dry-run para ver o
  que seria incluido/descartado sem gastar nenhuma chamada de LLM.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    package_parent = Path(__file__).resolve().parent.parent
    if str(package_parent) not in sys.path:
        sys.path.insert(0, str(package_parent))

from core.business_docs_builder import build_prompt as build_docs_prompt
from core.business_docs_builder import merge_business_docs
from core.config_loader import CatalogoConfig, SourceRepoConfig, load_config
from core.document_text import DocumentTextError, extract_text
from core.git_source import (
    GitRepoSettings,
    GitSourceError,
    list_recently_touched_paths,
    list_tracked_files_with_size,
    read_blob,
    read_blob_bytes,
    sync_blobless_tree,
)
from core.llm_client import LLMClient
from core.output_writer import write_timestamped_copy
from core.relevance_filter import DOCUMENT_EXTENSIONS, RelevanceFilterConfig, filter_relevant_paths, group_into_batches
from core.sources_context_builder import SourceFile, build_prompt, collect_source_files, merge_sources_context

# Hard ceiling on extracted document text sent in one LLM call. Source code
# batching (group_into_batches) caps by max_batch_bytes because *many small
# files* get grouped together; a business document is processed one-at-a-time
# instead (splitting a single vision/requirements doc mid-way would produce
# incoherent summaries), so it needs its own, more generous cap to avoid an
# unbounded prompt on some outlier huge PDF.
MAX_DOCUMENT_CHARS = 120_000


def _run_one_batch(
    source_files: list[SourceFile],
    label: str,
    system_name: str,
    package_root: str,
    template: str,
    output_schema: dict[str, Any],
    llm: LLMClient,
) -> dict[str, Any] | None:
    if not source_files:
        print(f"  [aviso] lote vazio ({label}), pulando")
        return None

    system_prompt, user_prompt = build_prompt(template, system_name, package_root, source_files, output_schema)
    print(f"  {label}: {len(source_files)} arquivo(s) -- chamando o LLM...")
    result = llm.complete_json(system_prompt, user_prompt)
    if result is None:
        print(f"  [erro] falha na chamada ao LLM: {llm.last_error}")
        return None
    return result


def _sync_and_list(
    config: CatalogoConfig, schema: str, repo: SourceRepoConfig, settings: GitRepoSettings,
) -> tuple[Path, list[tuple[str, int]], dict[str, str] | None] | None:
    repo_slug = repo.git_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    tree_dir = config.checkouts_root / f"{schema}-{repo_slug}-tree"
    print(f"Listando arquivos de {repo.git_url}@{repo.ref} (sem baixar conteudo)...")
    max_age_days = config.relevance_filter.max_file_age_days
    try:
        sync_blobless_tree(settings, tree_dir)
        all_files = list_tracked_files_with_size(tree_dir, repo.ref, settings)
        recently_touched = (
            list_recently_touched_paths(tree_dir, repo.ref, settings, since_days=max_age_days)
            if max_age_days is not None
            else None
        )
    except GitSourceError as exc:
        print(f"[erro] {exc}")
        return None

    if repo.subpackages:
        prefixes = tuple(p.rstrip("/") + "/" for p in repo.subpackages)
        all_files = [(p, s) for p, s in all_files if p.startswith(prefixes)]
    return tree_dir, all_files, recently_touched


def _relevance_filter_config(config: CatalogoConfig, repo: SourceRepoConfig, allowed_extensions=None) -> RelevanceFilterConfig:
    kwargs = dict(
        strict_include=repo.strict_include,
        exclude_dirname_substrings=config.relevance_filter.exclude_dirname_substrings,
        max_file_age_days=config.relevance_filter.max_file_age_days,
    )
    if allowed_extensions is not None:
        kwargs["allowed_extensions"] = allowed_extensions
    return RelevanceFilterConfig(**kwargs)


def _print_filter_summary(all_files: list, filter_result) -> None:
    total_kept_bytes = sum(size for _, size in filter_result.kept)
    print(f"Total no repositorio (escopo aplicado): {len(all_files)} arquivo(s)")
    print(f"Relevantes: {len(filter_result.kept)} arquivo(s), {total_kept_bytes} bytes")
    print(f"Descartados: {len(filter_result.discarded)} arquivo(s)")


def _print_dry_run_listing(filter_result) -> None:
    print("\n--- MANTIDOS ---")
    for path, size in filter_result.kept:
        print(f"  {size:>8}  {path}")
    print("\n--- DESCARTADOS (motivo) ---")
    for path, size, reason in filter_result.discarded:
        print(f"  {size:>8}  {path}  <- {reason}")


def _run_source_repos(
    config: CatalogoConfig, schema: str, repos: list[SourceRepoConfig], system_name: str, package_root: str,
    template: str, output_schema: dict[str, Any], llm: LLMClient, existing: dict[str, Any] | None, dry_run: bool,
) -> dict[str, Any] | None:
    merged = existing
    for repo_index, repo in enumerate(repos, start=1):
        print(f"\n=== Repositorio (codigo-fonte) {repo_index}/{len(repos)}: {repo.git_url}@{repo.ref} ===")
        settings = GitRepoSettings(
            git_url=repo.git_url, ref=repo.ref, username=repo.username,
            token_keyring_service=repo.token_keyring_service, token_keyring_username=repo.token_keyring_username,
        )
        synced = _sync_and_list(config, schema, repo, settings)
        if synced is None:
            continue
        tree_dir, all_files, recently_touched = synced

        filter_result = filter_relevant_paths(all_files, _relevance_filter_config(config, repo), recently_touched)
        _print_filter_summary(all_files, filter_result)
        if dry_run:
            _print_dry_run_listing(filter_result)
            continue

        batches = group_into_batches(filter_result.kept, config.batching.max_batch_bytes)
        print(f"Agrupado em {len(batches)} lote(s) de ate ~{config.batching.max_batch_bytes} bytes")

        for index, batch_paths in enumerate(batches, start=1):
            source_files = [SourceFile(relative_path=p, content=read_blob(tree_dir, repo.ref, p, settings)) for p in batch_paths]
            result = _run_one_batch(source_files, f"lote {index}/{len(batches)}", system_name, package_root, template, output_schema, llm)
            if result is None:
                continue
            merged = merge_sources_context(merged, result)

    if dry_run:
        return None
    if merged is not None:
        # merge_sources_context() lets each batch's LLM response overwrite
        # "fonte", so after 100+ batches it's left with whatever one random
        # file the last batch happened to mention. Overwrite it here with an
        # actual, complete provenance record of every repo this run pulled from.
        merged["fonte"] = "Repositorios git (git.sefaz.ce.gov.br): " + "; ".join(f"{r.git_url}@{r.ref}" for r in repos)
    return merged


def _run_docs_repos(
    config: CatalogoConfig, schema: str, repos: list[SourceRepoConfig], system_name: str,
    docs_template: str, docs_output_schema: dict[str, Any], llm: LLMClient,
    existing: dict[str, Any] | None, dry_run: bool,
) -> dict[str, Any] | None:
    merged = existing
    for repo_index, repo in enumerate(repos, start=1):
        print(f"\n=== Repositorio (documentos) {repo_index}/{len(repos)}: {repo.git_url}@{repo.ref} ===")
        settings = GitRepoSettings(
            git_url=repo.git_url, ref=repo.ref, username=repo.username,
            token_keyring_service=repo.token_keyring_service, token_keyring_username=repo.token_keyring_username,
        )
        synced = _sync_and_list(config, schema, repo, settings)
        if synced is None:
            continue
        tree_dir, all_files, recently_touched = synced

        filter_result = filter_relevant_paths(
            all_files,
            _relevance_filter_config(config, repo, allowed_extensions=DOCUMENT_EXTENSIONS),
            recently_touched,
        )
        _print_filter_summary(all_files, filter_result)
        if dry_run:
            _print_dry_run_listing(filter_result)
            continue

        for path, size in filter_result.kept:
            try:
                raw_bytes = read_blob_bytes(tree_dir, repo.ref, path, settings)
                text = extract_text(path, raw_bytes)
            except (GitSourceError, DocumentTextError) as exc:
                print(f"  [aviso] pulando {path}: {exc}")
                continue
            if not text.strip():
                print(f"  [aviso] {path}: texto extraido vazio, pulando")
                continue
            if len(text) > MAX_DOCUMENT_CHARS:
                print(f"  [aviso] {path}: texto truncado de {len(text)} para {MAX_DOCUMENT_CHARS} caracteres")
                text = text[:MAX_DOCUMENT_CHARS]

            print(f"  {path} ({size} bytes) -- chamando o LLM...")
            system_prompt, user_prompt = build_docs_prompt(docs_template, system_name, path, text, docs_output_schema)
            result = llm.complete_json(system_prompt, user_prompt)
            if result is None:
                print(f"  [erro] falha na chamada ao LLM: {llm.last_error}")
                continue
            for item in result.get("documentos_negocio", []) or []:
                if isinstance(item, dict):
                    item["arquivo"] = path
            merged = merge_business_docs(merged, result)

    if dry_run:
        return None
    if merged is not None:
        merged["fonte"] = "Repositorios git (git.sefaz.ce.gov.br): " + "; ".join(f"{r.git_url}@{r.ref}" for r in repos)
    return merged


def _run_git_mode(
    config: CatalogoConfig,
    schema: str,
    system_name: str,
    package_root: str,
    template: str,
    output_schema: dict[str, Any],
    docs_template: str,
    docs_output_schema: dict[str, Any],
    llm: LLMClient,
    existing: dict[str, Any] | None,
    existing_docs: dict[str, Any] | None,
    dry_run: bool,
    only: str = "all",
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    repos = config.sources_repos.get(schema)
    if not repos:
        print(
            f"Nenhum --source informado e sources_repos.{schema!r} nao esta configurado. "
            "Adicione um bloco sources_repos.<schema> (git_url, ref, token_keyring_service, "
            "token_keyring_username) ou passe --source apontando para uma pasta ja clonada."
        )
        return None, None

    source_repos = [r for r in repos if r.content_type == "source"] if only in ("all", "source") else []
    docs_repos = [r for r in repos if r.content_type == "docs"] if only in ("all", "docs") else []

    merged_source = _run_source_repos(
        config, schema, source_repos, system_name, package_root, template, output_schema, llm, existing, dry_run
    ) if source_repos else existing

    merged_docs = _run_docs_repos(
        config, schema, docs_repos, system_name, docs_template, docs_output_schema, llm, existing_docs, dry_run
    ) if docs_repos else existing_docs

    if dry_run:
        print("\n[dry-run] nenhuma chamada ao LLM foi feita.")
        return None, None

    return merged_source, merged_docs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent.parent / "config" / "catalogo.config.json"),
        help="Caminho para o catalogo.config.json (reaproveita llm/batching/sources_repos de la)",
    )
    parser.add_argument("--schema", required=True, help="Nome do esquema, ex: receita2")
    parser.add_argument("--system-name", default=None, help="Nome do sistema (default: nome do esquema)")
    parser.add_argument("--package-root", default="", help="Pacote raiz Java, ex: br.gov.ce.sefaz.receita")
    parser.add_argument(
        "--source",
        dest="source_dir",
        default=None,
        help="Pasta local ja clonada a ler. Se omitido, usa sources_repos.<schema> do config (lista+filtra+baixa automaticamente).",
    )
    parser.add_argument(
        "--prompt",
        default=str(Path(__file__).resolve().parent.parent / "prompts" / "prompt0_sources_context.txt"),
        help="Template do prompt de codigo-fonte (edite prompts/prompt0_sources_context.txt para ajustar as instrucoes)",
    )
    parser.add_argument(
        "--docs-prompt",
        default=str(Path(__file__).resolve().parent.parent / "prompts" / "prompt0_business_docs.txt"),
        help="Template do prompt para repos com content_type=docs (edite prompts/prompt0_business_docs.txt)",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="Funde com o sources_context_<esquema>.json ja existente em vez de sobrescrever",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="(modo git) Mostra quais arquivos seriam incluidos/descartados e como seriam agrupados, sem chamar o LLM.",
    )
    parser.add_argument(
        "--only",
        choices=["all", "source", "docs"],
        default="all",
        help="(modo git) Restringe a repos com content_type=source ou content_type=docs. "
        "Util pra rodar so os documentos novos sem reprocessar codigo-fonte ja extraido (ou vice-versa).",
    )
    args = parser.parse_args()

    config: CatalogoConfig = load_config(args.config)
    llm = LLMClient.from_config(config.llm)
    if not llm.enabled and not args.dry_run:
        print(f"LLM desabilitado ({llm.disabled_reason}). Configure config/catalogo.config.json ou grave a senha com scripts/store_keyring_secret.py.")
        sys.exit(1)

    output_schema = json.loads((Path(__file__).resolve().parent.parent / "prompts" / "sources_context_output_schema.json").read_text(encoding="utf-8"))
    template = Path(args.prompt).read_text(encoding="utf-8")
    docs_output_schema = json.loads((Path(__file__).resolve().parent.parent / "prompts" / "business_docs_output_schema.json").read_text(encoding="utf-8"))
    docs_template = Path(args.docs_prompt).read_text(encoding="utf-8")
    system_name = args.system_name or args.schema

    output_path = config.schema_root / args.schema / "inputs" / f"sources_context_{args.schema}.json"
    existing: dict[str, Any] | None = None
    if args.merge and output_path.exists():
        existing = json.loads(output_path.read_text(encoding="utf-8-sig"))

    docs_output_path = config.schema_root / args.schema / "inputs" / f"business_docs_context_{args.schema}.json"
    existing_docs: dict[str, Any] | None = None
    if args.merge and docs_output_path.exists():
        existing_docs = json.loads(docs_output_path.read_text(encoding="utf-8-sig"))

    if args.source_dir:
        source_files = collect_source_files(Path(args.source_dir), max_total_bytes=config.batching.max_batch_bytes)
        result = _run_one_batch(source_files, str(args.source_dir), system_name, args.package_root, template, output_schema, llm)
        if result is None:
            sys.exit(1)
        merged = merge_sources_context(existing, result)
        merged_docs = None
    else:
        merged, merged_docs = _run_git_mode(
            config, args.schema, system_name, args.package_root, template, output_schema,
            docs_template, docs_output_schema, llm, existing, existing_docs, args.dry_run, args.only,
        )
        if args.dry_run:
            return
        if merged is None and merged_docs is None:
            sys.exit(1)

    if merged is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(merged, ensure_ascii=False, indent=2)
        output_path.write_text(content, encoding="utf-8")
        write_timestamped_copy(output_path, content)
        action = "Fundido em" if existing is not None else "Gravado"
        print(f"{action}: {output_path}")
        print("Resumo:", merged.get("resumo"))

    if merged_docs is not None:
        docs_output_path.parent.mkdir(parents=True, exist_ok=True)
        content_docs = json.dumps(merged_docs, ensure_ascii=False, indent=2)
        docs_output_path.write_text(content_docs, encoding="utf-8")
        write_timestamped_copy(docs_output_path, content_docs)
        action = "Fundido em" if existing_docs is not None else "Gravado"
        print(f"{action}: {docs_output_path}")
        print("Resumo:", merged_docs.get("resumo"))


if __name__ == "__main__":
    main()
