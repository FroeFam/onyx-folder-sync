"""One extractor per file type family, and the extension registry.

Each extractor satisfies the ``Extractor`` protocol. Heavy libraries are
imported inside ``extract`` so that importing this module stays cheap.
"""

from __future__ import annotations

import os
import re
import time
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

from onyx_folder_sync.interfaces import Extractor

# Test-only hook: when this environment variable is "1", the registry also holds
# the extensions in ``_TestHookExtractor``. The child process inherits the
# variable, so the hook works across the spawn boundary.
TEST_HOOK_ENV = "ONYX_FOLDER_SYNC_TEST_EXTRACTORS"


class PdfExtractor:
    extensions = frozenset({"pdf"})
    version = "pdf-1"

    def extract(self, path: Path) -> str:
        import pypdfium2 as pdfium  # type: ignore[import-untyped]

        pdf = pdfium.PdfDocument(str(path))
        try:
            parts: list[str] = []
            for index in range(len(pdf)):
                page = pdf[index]
                textpage = page.get_textpage()
                try:
                    parts.append(textpage.get_text_bounded())
                finally:
                    textpage.close()
                    page.close()
            return "\n".join(parts).strip()
        finally:
            pdf.close()


class MarkItDownExtractor:
    extensions = frozenset({"docx", "pptx", "xlsx", "html", "htm", "epub"})
    version = "markitdown-1"

    def __init__(self) -> None:
        self._converter: object | None = None

    def extract(self, path: Path) -> str:
        from markitdown import MarkItDown

        if self._converter is None:
            self._converter = MarkItDown()
        converter = self._converter
        assert isinstance(converter, MarkItDown)  # noqa: S101
        return str(converter.convert(str(path)).text_content).strip()


class _TagStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _strip_tags(html: str) -> str:
    stripper = _TagStripper()
    stripper.feed(html)
    stripper.close()
    return re.sub(r"\n\s*\n+", "\n\n", "".join(stripper.parts))


def _body_text(message: EmailMessage) -> str:
    plain = message.get_body(preferencelist=("plain",))
    if plain is not None:
        text = str(plain.get_content())
        if text.strip():
            return text
    html = message.get_body(preferencelist=("html",))
    if html is not None:
        return _strip_tags(str(html.get_content()))
    return ""


class EmlExtractor:
    """markitdown has no eml converter, so this uses the standard library."""

    extensions = frozenset({"eml"})
    version = "eml-1"

    def extract(self, path: Path) -> str:
        with path.open("rb") as handle:
            message = BytesParser(policy=policy.default).parse(handle)
        assert isinstance(message, EmailMessage)  # noqa: S101
        lines = [
            f"{label}: {message[header]}"
            for label, header in (
                ("Subject", "subject"),
                ("From", "from"),
                ("To", "to"),
                ("Date", "date"),
            )
            if message[header]
        ]
        return "\n".join([*lines, "", _body_text(message)]).strip()


def decode_text(data: bytes) -> str:
    """Decode bytes: UTF-8 first, then charset-normalizer."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    from charset_normalizer import from_bytes

    best = from_bytes(data).best()
    if best is None:
        return data.decode("utf-8", errors="replace")
    return str(best)


class TextExtractor:
    extensions = frozenset({"txt", "md", "csv", "tsv"})
    version = "text-1"

    def extract(self, path: Path) -> str:
        return decode_text(path.read_bytes()).strip()


class _TestHookExtractor:
    """Misbehaving extractors for process tests. Registered only through TEST_HOOK_ENV."""

    extensions = frozenset({"xsleep", "xcrash", "xalloc"})
    version = "xtest-1"

    def extract(self, path: Path) -> str:
        kind = path.suffix.lower().lstrip(".")
        if kind == "xsleep":
            time.sleep(120)
        elif kind == "xcrash":
            os._exit(3)
        elif kind == "xalloc":
            megabytes = int(path.read_text(encoding="ascii").strip())
            return str(len(bytearray(megabytes * 1024 * 1024)))
        return ""


def build_registry() -> dict[str, Extractor]:
    """Map each supported extension to its extractor."""
    extractors: list[Extractor] = [
        PdfExtractor(),
        MarkItDownExtractor(),
        EmlExtractor(),
        TextExtractor(),
    ]
    if os.environ.get(TEST_HOOK_ENV) == "1":
        extractors.append(_TestHookExtractor())
    return {ext: extractor for extractor in extractors for ext in extractor.extensions}


def version_for(extension: str) -> str | None:
    """Extractor version for an extension, None when no extractor handles it."""
    extractor = build_registry().get(extension.lower().lstrip("."))
    return None if extractor is None else extractor.version
