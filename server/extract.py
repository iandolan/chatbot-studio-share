"""Text extraction for uploaded training files (PDF, DOCX, TXT, MD)."""
import io
import re
import xml.etree.ElementTree as ET
import zipfile

MAX_FILE_BYTES = 20 * 1024 * 1024
ALLOWED_EXTENSIONS = ("pdf", "docx", "txt", "md", "markdown")


class ExtractError(Exception):
    pass


def extract_file(filename, data):
    """Returns (title, text). Raises ExtractError with a user-facing message."""
    if len(data) > MAX_FILE_BYTES:
        raise ExtractError("File is larger than 20 MB")
    name = (filename or "upload").strip()
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    title = re.sub(r"\.[^.]+$", "", name) or name

    if ext in ("txt", "md", "markdown"):
        text = data.decode("utf-8", errors="replace")
    elif ext == "pdf":
        text = _extract_pdf(data)
    elif ext == "docx":
        text = _extract_docx(data)
    elif ext == "doc":
        raise ExtractError("Legacy .doc isn't supported — save it as .docx or PDF first")
    else:
        raise ExtractError(f"Unsupported file type '.{ext}' — use PDF, DOCX, TXT or MD")

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 10:
        raise ExtractError("Couldn't find any extractable text in that file "
                           "(scanned PDFs need OCR first)")
    return title, text


def _extract_pdf(data):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ExtractError("PDF support needs the 'pypdf' package "
                           "(pip install -r requirements.txt)") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                raise ExtractError("This PDF is password-protected")
        pages = []
        for page in reader.pages:
            pages.append(page.extract_text() or "")
        return "\n\n".join(pages)
    except ExtractError:
        raise
    except Exception as exc:
        raise ExtractError(f"Couldn't read that PDF ({exc.__class__.__name__})") from exc


def _extract_docx(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml_bytes = zf.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ExtractError("That doesn't look like a valid .docx file") from exc
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ExtractError("Couldn't parse the document XML inside that .docx") from exc

    paragraphs = []
    for para in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
        runs = [
            node.text or ""
            for node in para.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")
        ]
        paragraphs.append("".join(runs))
    return "\n".join(paragraphs)
