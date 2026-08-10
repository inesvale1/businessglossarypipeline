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

from core.config_loader import CatalogoConfig, load_config
from core.git_source import GitRepoSettings, GitSourceError, list_tracked_files_with_size, read_blob, sync_blobless_tree
from core.llm_client import LLMClient
from core.relevance_filter import RelevanceFilterConfig, filter_relevant_paths, group_into_batches
from core.sources_context_builder import SourceFile, build_prompt, collect_source_files, merge_sources_context


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


def _run_git_mode(
    config: CatalogoConfig,
    schema: str,
    system_name: str,
    package_root: str,
    template: str,
    output_schema: dict[str, Any],
    llm: LLMClient,
    existing: dict[str, Any] | None,
    dry_run: bool,
) -> dict[str, Any] | None:
    repo = config.sources_repos.get(schema)
    if repo is None:
        print(
            f"Nenhum --source informado e sources_repos.{schema!r} nao esta configurado. "
            "Adicione um bloco sources_repos.<schema> (git_url, ref, token_keyring_service, "
            "token_keyring_username) ou passe --source apontando para uma pasta ja clonada."
        )
        return None

    settings = GitRepoSettings(
        git_url=repo.git_url, ref=repo.ref, username=repo.username,
        token_keyring_service=repo.token_keyring_service, token_keyring_username=repo.token_keyring_username,
    )
    tree_dir = config.checkouts_root / f"{schema}-tree"

    print(f"Listando arquivos de {repo.git_url}@{repo.ref} (sem baixar conteudo)...")
    try:
        sync_blobless_tree(settings, tree_dir)
        all_files = list_tracked_files_with_size(tree_dir, repo.ref)
    except GitSourceError as exc:
        print(f"[erro] {exc}")
        return None

    if repo.subpackages:
        prefixes = tuple(p.rstrip("/") + "/" for p in repo.subpackages)
        all_files = [(p, s) for p, s in all_files if p.startswith(prefixes)]

    filter_result = filter_relevant_paths(all_files, RelevanceFilterConfig(strict_include=repo.strict_include))
    total_kept_bytes = sum(size for _, size in filter_result.kept)
    print(f"Total no repositorio (escopo aplicado): {len(all_files)} arquivo(s)")
    print(f"Relevantes: {len(filter_result.kept)} arquivo(s), {total_kept_bytes} bytes")
    print(f"Descartados: {len(filter_result.discarded)} arquivo(s)")
    if dry_run:
        print("\n--- MANTIDOS ---")
        for path, size in filter_result.kept:
            print(f"  {size:>8}  {path}")
        print("\n--- DESCARTADOS (motivo) ---")
        for path, size, reason in filter_result.discarded:
            print(f"  {size:>8}  {path}  <- {reason}")
        print("\n[dry-run] nenhuma chamada ao LLM foi feita.")
        return None

    batches = group_into_batches(filter_result.kept, config.batching.max_batch_bytes)
    print(f"Agrupado em {len(batches)} lote(s) de ate ~{config.batching.max_batch_bytes} bytes")

    merged = existing
    for index, batch_paths in enumerate(batches, start=1):
        source_files = [SourceFile(relative_path=p, content=read_blob(tree_dir, repo.ref, p)) for p in batch_paths]
        result = _run_one_batch(source_files, f"lote {index}/{len(batches)}", system_name, package_root, template, output_schema, llm)
        if result is None:
            continue
        merged = merge_sources_context(merged, result)

    return merged


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
        help="Template do prompt (edite prompts/prompt0_sources_context.txt para ajustar as instrucoes)",
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
    args = parser.parse_args()

    config: CatalogoConfig = load_config(args.config)
    llm = LLMClient.from_config(config.llm)
    if not llm.enabled and not args.dry_run:
        print(f"LLM desabilitado ({llm.disabled_reason}). Configure config/catalogo.config.json ou grave a senha com scripts/store_keyring_secret.py.")
        sys.exit(1)

    output_schema = json.loads((Path(__file__).resolve().parent.parent / "prompts" / "sources_context_output_schema.json").read_text(encoding="utf-8"))
    template = Path(args.prompt).read_text(encoding="utf-8")
    system_name = args.system_name or args.schema

    output_path = config.schema_root / args.schema / "inputs" / f"sources_context_{args.schema}.json"
    existing: dict[str, Any] | None = None
    if args.merge and output_path.exists():
        existing = json.loads(output_path.read_text(encoding="utf-8-sig"))

    if args.source_dir:
        source_files = collect_source_files(Path(args.source_dir), max_total_bytes=config.batching.max_batch_bytes)
        result = _run_one_batch(source_files, str(args.source_dir), system_name, args.package_root, template, output_schema, llm)
        if result is None:
            sys.exit(1)
        merged = merge_sources_context(existing, result)
    else:
        merged = _run_git_mode(config, args.schema, system_name, args.package_root, template, output_schema, llm, existing, args.dry_run)
        if args.dry_run:
            return
        if merged is None:
            sys.exit(1)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")

    action = "Fundido em" if existing is not None else "Gravado"
    print(f"{action}: {output_path}")
    print("Resumo:", merged.get("resumo"))


if __name__ == "__main__":
    main()
