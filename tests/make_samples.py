"""Generates the files under tests/samples. Run: uv run python tests/make_samples.py

The committed samples are the output of this script. Every sample holds the phrase
"quokka marmalade <ext>" so that tests can assert on it.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

SAMPLES = Path(__file__).parent / "samples"
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def phrase(ext: str) -> str:
    return f"quokka marmalade {ext}"


def _zip(path: Path, members: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)


def make_pdf(path: Path, text: str | None) -> None:
    """A minimal valid one-page PDF; text None gives a page without a text object."""
    stream = "" if text is None else f"BT /F1 18 Tf 72 700 Td ({text}) Tj ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = "%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    path.write_bytes(out.encode("ascii"))


def make_docx(path: Path) -> None:
    ns = "http://schemas.openxmlformats.org/"
    _zip(
        path,
        {
            "[Content_Types].xml": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                f'<Types xmlns="{ns}package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
                'package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/word/document.xml" ContentType="application/vnd.'
                'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                "</Types>"
            ),
            "_rels/.rels": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                f'<Relationships xmlns="{ns}package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/officeDocument" '
                'Target="word/document.xml"/></Relationships>'
            ),
            "word/document.xml": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                f'<w:document xmlns:w="{ns}wordprocessingml/2006/main"><w:body>'
                f"<w:p><w:r><w:t>{phrase('docx')}</w:t></w:r></w:p>"
                "</w:body></w:document>"
            ),
        },
    )


def make_pptx(path: Path) -> None:
    from pptx import Presentation

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = phrase("pptx")
    deck.save(str(path))


def make_xlsx(path: Path) -> None:
    from openpyxl import Workbook  # type: ignore[import-untyped]

    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.append(["name", "note"])
    sheet.append(["row1", phrase("xlsx")])
    book.save(str(path))


def make_epub(path: Path) -> None:
    ns = "http://www.idpf.org/2007/opf"
    _zip(
        path,
        {
            "mimetype": "application/epub+zip",
            "META-INF/container.xml": (
                '<?xml version="1.0"?>'
                '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:'
                'container"><rootfiles><rootfile full-path="content.opf" '
                'media-type="application/oebps-package+xml"/></rootfiles></container>'
            ),
            "content.opf": (
                '<?xml version="1.0"?>'
                f'<package xmlns="{ns}" version="2.0" unique-identifier="id">'
                '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                '<dc:title>Sample</dc:title><dc:identifier id="id">sample-1</dc:identifier>'
                "<dc:language>en</dc:language></metadata>"
                '<manifest><item id="c1" href="chapter1.xhtml" '
                'media-type="application/xhtml+xml"/></manifest>'
                '<spine><itemref idref="c1"/></spine></package>'
            ),
            "chapter1.xhtml": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Sample</title></head>'
                f"<body><p>{phrase('epub')}</p></body></html>"
            ),
        },
    )


def main() -> None:
    SAMPLES.mkdir(exist_ok=True)
    make_pdf(SAMPLES / "sample.pdf", phrase("pdf"))
    make_pdf(SAMPLES / "empty.pdf", None)
    make_docx(SAMPLES / "sample.docx")
    make_pptx(SAMPLES / "sample.pptx")
    make_xlsx(SAMPLES / "sample.xlsx")
    make_epub(SAMPLES / "sample.epub")
    html = f"<html><head><title>t</title></head><body><p>{phrase('html')}</p></body></html>\n"
    (SAMPLES / "sample.html").write_text(html, encoding="ascii")
    (SAMPLES / "sample.htm").write_text(
        html.replace(phrase("html"), phrase("htm")), encoding="ascii"
    )
    (SAMPLES / "sample.eml").write_bytes(
        (
            "From: Alice <alice@example.com>\nTo: Bob <bob@example.com>\n"
            "Subject: Hello there\nDate: Thu, 01 Jan 2026 10:00:00 +0000\n"
            "Content-Type: text/plain; charset=utf-8\n\n" + phrase("eml") + "\n"
        ).encode("ascii")
    )
    (SAMPLES / "sample.txt").write_text(phrase("txt") + "\n", encoding="ascii")
    (SAMPLES / "sample.md").write_text("# Title\n\n" + phrase("md") + "\n", encoding="ascii")
    (SAMPLES / "sample.csv").write_text("a,b\n1," + phrase("csv") + "\n", encoding="ascii")
    (SAMPLES / "sample.tsv").write_text("a\tb\n1\t" + phrase("tsv") + "\n", encoding="ascii")
    junk = b"PK\x03\x04" + bytes(range(256)) * 4
    for ext in ("pdf", "docx", "pptx", "xlsx", "epub"):
        (SAMPLES / f"corrupt.{ext}").write_bytes(junk if ext != "pdf" else b"%PDF-1.4\n" + junk)


if __name__ == "__main__":
    main()
