from __future__ import annotations

from datetime import datetime
from pathlib import Path


def write_timestamped_copy(path: Path, content: str) -> Path:
    """Write an extra dated copy of `path` alongside it (e.g.
    sources_context_cadastro.json -> sources_context_cadastro_20260815_1200.json).

    The fixed-name file at `path` stays authoritative -- it's what
    pipeline_bridge.py, generate_canonical_context.py and
    generate_sources_context.py --merge all read back by exact path. This
    timestamped sibling is a history trail only, never read by the pipeline.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamped_path = path.with_name(f"{path.stem}_{timestamp}{path.suffix}")
    timestamped_path.write_text(content, encoding="utf-8")
    return timestamped_path
