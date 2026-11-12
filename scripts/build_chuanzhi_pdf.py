"""Build the Chinese technical PDF and page images using isolated authoring tools."""
from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]


def build(source: Path, output: Path) -> dict:
    pdfmetrics.registerFont(TTFont("CJK", "C:/Windows/Fonts/simhei.ttf"))
    pdfmetrics.registerFont(TTFont("Mono", "C:/Windows/Fonts/consola.ttf"))
    pdfmetrics.registerFontFamily("CJK", normal="CJK", bold="CJK", italic="CJK", boldItalic="CJK")
    ink = colors.HexColor("#18354c")
    teal = colors.HexColor("#087f8c")
    styles = {
        "body": ParagraphStyle("body", fontName="CJK", fontSize=10.4, leading=18,
                               textColor=ink, spaceAfter=11, wordWrap="CJK"),
        "h1": ParagraphStyle("h1", fontName="CJK", fontSize=22, leading=31,
                             textColor=ink, spaceAfter=22, wordWrap="CJK"),
        "h2": ParagraphStyle("h2", fontName="CJK", fontSize=15, leading=23,
                             textColor=teal, spaceAfter=18, wordWrap="CJK"),
        "cell": ParagraphStyle("cell", fontName="CJK", fontSize=9, leading=14,
                               textColor=ink, wordWrap="CJK"),
        "code": ParagraphStyle("code", fontName="CJK", fontSize=8.1, leading=13,
                               backColor=colors.HexColor("#edf4f7"), borderPadding=9,
                               spaceAfter=12, wordWrap="CJK"),
        "cover": ParagraphStyle("cover", fontName="CJK", fontSize=48, leading=64,
                                textColor=ink, spaceAfter=23, alignment=TA_CENTER),
    }

    def paragraph(text: str, kind: str = "body") -> Paragraph:
        safe = html.escape(text).replace("`", "")
        safe = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", safe)
        return Paragraph(safe, styles[kind])

    story = []
    pages = source.read_text(encoding="utf-8").split("<!-- page -->")
    width = A4[0] - 104
    for page_index, page in enumerate(pages):
        if page_index:
            story.append(PageBreak())
        else:
            story.append(Spacer(1, 75))
        lines = page.strip().splitlines()
        index = 0
        while index < len(lines):
            line = lines[index].strip()
            index += 1
            if not line:
                continue
            if line.startswith("```"):
                code = []
                while index < len(lines) and not lines[index].strip().startswith("```"):
                    code.append(lines[index])
                    index += 1
                index += 1
                story.append(Paragraph("<br/>".join(html.escape(s) for s in code), styles["code"]))
            elif line.startswith("|"):
                rows = [line]
                while index < len(lines) and lines[index].strip().startswith("|"):
                    rows.append(lines[index].strip())
                    index += 1
                values = [[s.strip() for s in r.strip("|").split("|")] for r in rows]
                values = [r for r in values if not all(re.fullmatch(r"[:\- ]+", c) for c in r)]
                count = len(values[0])
                widths = [width / count] * count
                if count == 3:
                    widths = [width * .23, width * .36, width * .41]
                table = Table([[paragraph(c, "cell") for c in r] for r in values],
                              colWidths=widths, repeatRows=1, hAlign="LEFT")
                table.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dceef1")),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f7f9")]),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 9),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
                    ("LINEBELOW", (0, 0), (-1, 0), .6, teal),
                ]))
                story.extend([table, Spacer(1, 16)])
            elif line.startswith("# "):
                story.append(paragraph(line[2:], "cover" if not page_index else "h1"))
                if page_index:
                    story.extend([HRFlowable(width="100%", color=teal, thickness=2), Spacer(1, 20)])
            elif line.startswith("## "):
                story.append(paragraph(line[3:], "h2"))
            elif line.startswith("!["):
                match = re.match(r"!\[(.*?)\]\((.*?)\)", line)
                if match:
                    path = ROOT / match.group(2)
                    if path.exists():
                        from PIL import Image as PILImage
                        with PILImage.open(path) as picture:
                            ratio = min(width / picture.width, 360 / picture.height)
                            picture_width, picture_height = picture.width * ratio, picture.height * ratio
                        story.extend([Image(str(path), width=picture_width, height=picture_height),
                                      paragraph(match.group(1), "cell"), Spacer(1, 10)])
            else:
                story.append(paragraph(line))

    output.parent.mkdir(parents=True, exist_ok=True)

    def decorate(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(teal)
        canvas.rect(0, A4[1] - 9, A4[0], 9, stroke=0, fill=1)
        canvas.setFont("CJK", 8)
        canvas.setFillColor(colors.HexColor("#637789"))
        canvas.drawString(52, 29, "颐邻 / Vibe Coding / 唐克波 · B组 · 2132417")
        canvas.drawRightString(A4[0] - 52, 29, f"{doc.page:02d}")
        canvas.restoreState()

    document = SimpleDocTemplate(str(output), pagesize=A4, leftMargin=52, rightMargin=52,
                                 topMargin=50, bottomMargin=54,
                                 title="颐邻：社区智慧养老平台技术文档", author="唐克波")
    document.build(story, onFirstPage=decorate, onLaterPages=decorate)
    import pymupdf
    from PIL import Image as PILImage
    from PIL import ImageDraw

    qa = ROOT / "reports" / "chuanzhi-pdf"
    qa.mkdir(parents=True, exist_ok=True)
    reader = pymupdf.open(output)
    if len(reader) > 30:
        raise RuntimeError("Technical PDF exceeds the competition page limit")
    thumbnails = []
    details = []
    for number, page in enumerate(reader):
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.25, 1.25))
        path = qa / f"page-{number + 1:02}.png"
        pixmap.save(path)
        thumb = PILImage.open(path).convert("RGB")
        thumb.thumbnail((238, 337))
        thumbnails.append(thumb)
        details.append({"page": number + 1, "text_characters": len(page.get_text()), "image": str(path)})
    for start in range(0, len(thumbnails), 8):
        group = thumbnails[start:start + 8]
        sheet = PILImage.new("RGB", (4 * 258, 2 * 365), "#dce5ec")
        draw = ImageDraw.Draw(sheet)
        for offset, thumb in enumerate(group):
            x, y = (offset % 4) * 258 + 10, (offset // 4) * 365 + 8
            sheet.paste(thumb, (x, y))
            draw.text((x, y + 340), f"Page {start + offset + 1}", fill="#18354c")
        sheet.save(qa / f"contact-{start // 8 + 1}.png")
    result = {"pdf": str(output), "page_count": len(reader), "source": str(source), "pages": details,
              "renderer": "PyMuPDF (Poppler unavailable)", "visual_review": "pending"}
    (qa / "qa.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT / "docs/chuanzhi/技术文档.md")
    parser.add_argument("--output", type=Path, default=ROOT / "output/pdf/2132417-唐克波-技术文档.pdf")
    args = parser.parse_args()
    result = build(args.source, args.output)
    print(json.dumps({k: v for k, v in result.items() if k != "pages"}, ensure_ascii=False))
