from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from pdfminer.high_level import extract_text as pdf_extract

SUPPORTED_SUFFIXES = {".pdf", ".docx"}

_P, _TBL, _TR, _TC, _T = qn("w:p"), qn("w:tbl"), qn("w:tr"), qn("w:tc"), qn("w:t")
_TAB, _BR, _CR = qn("w:tab"), qn("w:br"), qn("w:cr")
_TXBX = qn("w:txbxContent")
# Content controls (common in Word's own CV templates) wrap paragraphs and tables.
_SDT_CONTENT, _CUSTOM_XML = qn("w:sdtContent"), qn("w:customXml")
_WRAPPERS = {qn("w:sdt"), _CUSTOM_XML}
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"


class UnreadableCvError(Exception):
    pass


def _in_fallback(el, stop) -> bool:
    # Text boxes are stored twice (DrawingML + VML fallback); read only the primary copy.
    parent = el.getparent()
    while parent is not None and parent is not stop:
        if parent.tag == _MC_FALLBACK:
            return True
        parent = parent.getparent()
    return False


def _paragraph_text(p) -> str:
    """Text of one w:p, excluding text boxes nested inside it (those are emitted separately)."""
    chunks = []
    for el in p.iter(_T, _TAB, _BR, _CR):
        owner = el.getparent()
        while owner is not None and owner is not p and owner.tag != _TXBX:
            owner = owner.getparent()
        if owner is not p:
            continue
        chunks.append(el.text or "" if el.tag == _T else ("\t" if el.tag == _TAB else "\n"))
    return "".join(chunks).strip()


def _block_lines(container) -> list[str]:
    lines: list[str] = []
    for child in container.iterchildren():
        if child.tag == _P:
            text = _paragraph_text(child)
            if text:
                lines.append(text)
            for box in child.iter(_TXBX):
                if not _in_fallback(box, child):
                    lines.extend(_block_lines(box))
        elif child.tag == _TBL:
            for row in child.iter(_TR):
                if row.getparent() is not child:
                    continue  # nested tables are handled inside their cell
                cells = [" / ".join(_block_lines(tc)) for tc in row.iterchildren(_TC)]
                cells = [c for c in cells if c]
                if cells:
                    lines.append(" | ".join(cells))
        elif child.tag in _WRAPPERS:
            for content in child.iterchildren(_SDT_CONTENT):
                lines.extend(_block_lines(content))
            if child.tag == _CUSTOM_XML:
                lines.extend(_block_lines(child))
    return lines


def _docx_text(path: Path) -> str:
    doc = Document(path)
    seen: set[str] = set()
    header, footer = [], []
    for section in doc.sections:
        for part, bucket in ((section.header, header), (section.first_page_header, header),
                             (section.footer, footer), (section.first_page_footer, footer)):
            if part.is_linked_to_previous:
                continue
            for line in _block_lines(part._element):
                if line not in seen:
                    seen.add(line)
                    bucket.append(line)
    return "\n".join(header + _block_lines(doc.element.body) + footer)


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            text = (pdf_extract(str(path)) or "").replace("\x0c", "\n")
        elif suffix == ".docx":
            text = _docx_text(path)
        else:
            raise UnreadableCvError(f"unsupported type: {suffix}")
    except UnreadableCvError:
        raise
    except Exception as exc:
        raise UnreadableCvError(str(exc)) from exc
    if not text.strip():
        raise UnreadableCvError("empty CV text")
    return text
