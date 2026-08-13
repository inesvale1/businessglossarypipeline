from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree as ET

# .doc (legacy OLE/binary Word format) is deliberately not supported here --
# there's no reliable stdlib-only way to extract text from it, unlike
# .odt/.docx (both zip+XML containers) and .pdf (via pypdf). Files with this
# extension are reported as skipped by the caller instead of silently
# producing garbled text.
SUPPORTED_EXTENSIONS = (".odt", ".docx", ".pdf")


class DocumentTextError(RuntimeError):
    pass


def extract_text(relative_path: str, content: bytes) -> str:
    """Extract plain text from one document's raw bytes, dispatching on the
    file extension. Raises DocumentTextError for anything not in
    SUPPORTED_EXTENSIONS or that fails to parse.
    """
    ext = relative_path.lower().rsplit(".", 1)[-1] if "." in relative_path else ""
    try:
        if ext == "odt":
            return _extract_odt(content)
        if ext == "docx":
            return _extract_docx(content)
        if ext == "pdf":
            return _extract_pdf(content)
    except Exception as exc:
        raise DocumentTextError(f"Falha ao extrair texto de {relative_path!r}: {exc}") from exc
    raise DocumentTextError(f"Formato nao suportado: {relative_path!r}")


def _extract_odt(content: bytes) -> str:
    # ODT is a zip archive; the visible text lives in content.xml as
    # <text:p>/<text:h> elements. No odfpy dependency needed for plain text.
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        xml_bytes = archive.read("content.xml")
    root = ET.fromstring(xml_bytes)
    paragraphs: list[str] = []
    for element in root.iter():
        local_tag = element.tag.rsplit("}", 1)[-1]
        if local_tag in ("p", "h"):
            text = "".join(element.itertext()).strip()
            if text:
                paragraphs.append(text)
    return "\n".join(paragraphs)


def _extract_docx(content: bytes) -> str:
    import docx  # python-docx

    document = docx.Document(io.BytesIO(content))
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_pdf(content: bytes) -> str:
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(content))
    parts = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            parts.append(text)
    return "\n".join(parts)
