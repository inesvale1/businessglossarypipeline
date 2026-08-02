from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    package_parent = Path(__file__).resolve().parent.parent
    if str(package_parent) not in sys.path:
        sys.path.insert(0, str(package_parent))

from core.config_loader import load_config
from core.pipeline import run


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gera o Contexto Local Canonico (Prompt 1) para os esquemas configurados."
    )
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent.parent / "config" / "catalogo.config.json"),
        help="Caminho para o catalogo.config.json (default: config/catalogo.config.json)",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    if not config.schemas:
        print("Nenhum esquema configurado em 'schemas'. Nada a fazer.")
        return

    results = run(config)

    print()
    print("Resumo:")
    for result in results:
        line = f"  [{result.status.upper():7}] {result.schema_name} - {result.detail}"
        if result.output_path:
            line += f" -> {result.output_path}"
        print(line)

    failures = [r for r in results if r.status == "error"]
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
