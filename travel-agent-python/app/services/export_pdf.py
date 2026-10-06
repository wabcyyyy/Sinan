"""印刷版行程单 PDF（用 reportlab 复刻 Java 的 Thymeleaf + Flying Saucer 印刷稿）。

**为什么不是"沿用那份 HTML 模板"**：Java 侧模板能跑是因为 Flying Saucer 只支持 CSS 2.1
子集，而这份稿子的骨架恰好落在那个子集里（表格边框合并、日块 `page-break-inside: avoid`、
圆角、`@page` 页边距）。Python 侧纯 wheel 可用的 xhtml2pdf 会**直接声明忽略**
`border-collapse` / `border-radius` / `page-break-inside`——表格会画成双线网格、日块会从中间
断页；支持这三条的 WeasyPrint 又要 Pango/Cairo 原生库，本机 Windows 装不上，等于把
"导出 PDF" 做成只能在 Linux 演示。所以这里按同一套视觉规则用 flowable 重写版式，
Java 模板随 Java 一起在 M7 退役。

印刷口径保持与站内一致：类型与来源都映射成用户能读的词，**不在印刷品上外露内部枚举**
（`attraction→景点`、`mysql.poi_knowledge→目的地知识库`）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Table, TableStyle

logger = logging.getLogger(__name__)

FONT_NAME = "SimHei"
INK = colors.HexColor("#14181f")
PAPER = colors.HexColor("#fffdf8")
MASTHEAD = colors.HexColor("#0b2927")
TEAL = colors.HexColor("#0f766e")
DEEP = colors.HexColor("#0c2e2c")
SAGE = colors.HexColor("#9dc3be")
TERRA = colors.HexColor("#bd6246")
PEACH = colors.HexColor("#f2c4a8")
LINE = colors.HexColor("#d8dfda")
CELL = colors.HexColor("#e2e6e4")
HEAD_BG = colors.HexColor("#eef2f0")
PAPER_BG = colors.HexColor("#f6f0e4")
MUTED = colors.HexColor("#5c6470")
BODY = colors.HexColor("#3d4451")
FAINT = colors.HexColor("#6f695f")

ITEM_COLUMNS = ("类型", "名称", "时间", "时长(分)", "费用(￥)", "开放/来源", "备注")
ITEM_COLUMN_WIDTHS = (34, 96, 62, 42, 46, 92, 92)

TYPE_LABELS = {"attraction": "景点", "food": "美食", "hotel": "酒店", "transport": "交通"}
SOURCE_LABELS = {
    "client-context": "行程设定",
    "mysql.poi_knowledge": "目的地知识库",
    "llm.open_day": "开放研究",
    "local-grounding": "本地知识库",
    # 存量数据（去高德前写入的行）仍按当时的事实标注
    "amap-grounding": "高德地图（存量）",
    "amap": "高德地图（存量）",
}


def register_font(path: Path) -> None:
    """注册嵌入字体；SimHei 无粗体字面，把粗体也映射到同一支，避免 reportlab 找不到变体。"""
    pdfmetrics.registerFont(TTFont(FONT_NAME, str(path)))
    pdfmetrics.registerFontFamily(FONT_NAME, normal=FONT_NAME, bold=FONT_NAME, italic=FONT_NAME, boldItalic=FONT_NAME)


def type_label(value: str | None) -> str:
    if value is None:
        return ""
    return TYPE_LABELS.get(value, value)


def source_label(value: str | None) -> str:
    """来源 → 用户化口径；未知来源统一说"AI 生成"，不印内部 key。"""
    if value is None:
        return "行程设定"
    return SOURCE_LABELS.get(value, "AI 生成")


def build_pdf(model: dict[str, Any], target: Path) -> int:
    """把行程模型排版成 PDF 文件，返回页数。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(target),
        pagesize=A4,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=str(model.get("title") or "行程单"),
    )
    story = [_masthead(model)]
    for day in model.get("dayList") or []:
        story.extend(_day_block(day))
    story.extend(_budget_appendix(model))
    story.append(_colophon())
    doc.build(story)
    return doc.page


def _style(name: str, size: int, color: colors.Color, **extra: Any) -> ParagraphStyle:
    return ParagraphStyle(name, fontName=FONT_NAME, fontSize=size, leading=size * 1.6, textColor=color, **extra)


_H_STYLES = {
    "brand": _style("brand", 9, SAGE, spaceAfter=6),
    "title": _style("title", 22, PAPER, spaceAfter=2),
    "city": _style("city", 13, PEACH, spaceAfter=8),
    "meta": _style("meta", 10, PAPER),
    "day_no": _style("day_no", 13, DEEP),
    "day_date": _style("day_date", 10, MUTED),
    "theme": _style("theme", 12, TERRA, spaceBefore=3),
    "note": _style("note", 11, BODY, spaceBefore=5),
    "tip": _style("tip", 10, MUTED, leftIndent=10, bulletIndent=2, spaceBefore=2),
    "guide": _style("guide", 10, FAINT, spaceBefore=2),
    "cell": _style("cell", 10.5, INK),
    "cell_muted": _style("cell_muted", 10, FAINT),
    "head": _style("head", 10, DEEP),
    "budget_title": _style("budget_title", 12, DEEP),
    "total": _style("total", 11, DEEP, alignment=2),
    "colophon": _style("colophon", 9.5, MUTED, alignment=1, spaceBefore=12),
}


def _masthead(model: dict[str, Any]) -> Table:
    inner: list[Any] = [
        Paragraph("TRAVEL FIELD HANDBOOK", _H_STYLES["brand"]),
        Paragraph(str(model.get("title") or "行程单"), _H_STYLES["title"]),
        Paragraph(str(model.get("city") or ""), _H_STYLES["city"]),
    ]
    rule = Table([[""]], colWidths=[64], rowHeights=[2], style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), TERRA)]))
    rule.hAlign = "LEFT"
    inner.append(rule)
    chips = [
        chunk
        for chunk in (
            _chip(model, f"{model['days']} 天 {model['persons']} 人", model.get("days")),
            _chip(model, f"{model.get('startDate')} ~ {model.get('endDate')}", model.get("startDate")),
            _chip(model, f"偏好：{model.get('preferences')}", model.get("preferences")),
            _chip(model, f"预算上限 ￥{model.get('budget')}", model.get("budget")),
        )
        if chunk
    ]
    if chips:
        inner.append(
            Table(
                [[chips]], style=TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 2)])
            )
        )
    block = Table(
        [[inner]],
        colWidths=[None],
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), MASTHEAD),
                ("LEFTPADDING", (0, 0), (-1, -1), 22),
                ("RIGHTPADDING", (0, 0), (-1, -1), 22),
                ("TOPPADDING", (0, 0), (-1, -1), 20),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 16),
            ]
        ),
    )
    return _spaced(block, 14)


def _chip(model: dict[str, Any], text: str, when: Any):
    return Paragraph(str(text), _H_STYLES["meta"]) if when else None


def _day_block(day: dict[str, Any]) -> list[Any]:
    head = f"第 {day.get('dayNo')} 天"
    if day.get("travelDate"):
        head += f"<font size=10 color='#5c6470'>（{day['travelDate']}）</font>"
    content: list[Any] = [Paragraph(head, _H_STYLES["day_no"])]
    if day.get("theme"):
        content.append(Paragraph(str(day["theme"]), _H_STYLES["theme"]))
    if day.get("note"):
        content.append(Paragraph(str(day["note"]), _H_STYLES["note"]))
    content.extend(Paragraph(f"• {tip}", _H_STYLES["tip"]) for tip in day.get("practicalNotes") or [])
    if day.get("backupPlan"):
        content.append(Paragraph(f"备选：{day['backupPlan']}", _H_STYLES["guide"]))
    card = Table(
        [[content]],
        colWidths=[None],
        style=TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 1, LINE),
                ("LINEBEFORE", (0, 0), (0, -1), 3, TEAL),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        ),
    )
    # 卡片只包日头：整块（含明细表）套进一个单元格会让 reportlab 无法跨页拆分，
    # 一天超过一页就直接 LayoutError。明细表独立成流、重复表头跨页，是印刷版
    # 更稳的等价做法（模板里的 page-break-inside 在内容超过一页时本来也必须断）。
    return [_spaced(KeepTogether(card), 6), _spaced(_item_table(day.get("items") or []), 12)]


def _item_table(items: list[dict[str, Any]]) -> Table:
    rows: list[list[Any]] = [[Paragraph(label, _H_STYLES["head"]) for label in ITEM_COLUMNS]]
    styles: list[tuple[Any, ...]] = [
        ("BACKGROUND", (0, 0), (-1, 0), HEAD_BG),
        # 只画一层网格线 = CSS 的 border-collapse: collapse
        ("GRID", (0, 0), (-1, -1), 1, CELL),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    row = 1
    for item in items:
        rows.append(
            [
                Paragraph(str(item.get("typeLabel") or ""), _H_STYLES["cell"]),
                Paragraph(str(item.get("poiName") or ""), _H_STYLES["cell"]),
                Paragraph(_time_range(item), _H_STYLES["cell"]),
                Paragraph("" if item.get("durationMin") is None else str(item["durationMin"]), _H_STYLES["cell"]),
                Paragraph("" if item.get("cost") is None else str(item["cost"]), _H_STYLES["cell"]),
                Paragraph(_source_cell(item), _H_STYLES["cell"]),
                Paragraph(str(item.get("remark") or ""), _H_STYLES["cell"]),
            ]
        )
        row += 1
        why = item.get("whyThis")
        if why:
            # 叙事理由是整宽次级行（模板里是 colspan=7 且 border:none），不占第 8 列
            rows.append([Paragraph(str(why), _H_STYLES["cell_muted"])] + [""] * 6)
            styles.append(("SPAN", (0, row), (-1, row)))
            styles.append(("GRID", (0, row), (-1, row), 0, PAPER))
            row += 1
    table = Table(rows, colWidths=ITEM_COLUMN_WIDTHS, repeatRows=1, style=TableStyle(styles))
    table.hAlign = "LEFT"
    return table


def _time_range(item: dict[str, Any]) -> str:
    start, end = item.get("startTime"), item.get("endTime")
    if not start:
        return ""
    return f"{start} - {end}" if end else str(start)


def _source_cell(item: dict[str, Any]) -> str:
    parts = []
    if item.get("openTime"):
        parts.append(f"开放：{item['openTime']}")
    if item.get("source"):
        parts.append(f"来源：{item.get('srcLabel') or source_label(item['source'])}")
    return "<br/>".join(parts)


def _budget_appendix(model: dict[str, Any]) -> list[Any]:
    rows = [
        [
            Paragraph("分类", _H_STYLES["head"]),
            Paragraph("金额", _H_STYLES["head"]),
            Paragraph("项目数", _H_STYLES["head"]),
        ]
    ]
    for budget in model.get("budgetList") or []:
        rows.append(
            [
                Paragraph(str(budget.get("category") or ""), _H_STYLES["cell"]),
                Paragraph(str(budget.get("amount") or ""), _H_STYLES["cell"]),
                Paragraph("" if budget.get("itemCount") is None else str(budget["itemCount"]), _H_STYLES["cell"]),
            ]
        )
    table = Table(
        rows,
        colWidths=[ITEM_COLUMN_WIDTHS[1], ITEM_COLUMN_WIDTHS[2], ITEM_COLUMN_WIDTHS[3]],
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), HEAD_BG),
                ("GRID", (0, 0), (-1, -1), 1, CELL),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        ),
    )
    # 与日卡同理：标题条与表格分开成两个流，预算行很多时表格可以跨页而不是炸掉
    title_card = Table(
        [[Paragraph("预算附录（人民币估算）", _H_STYLES["budget_title"])]],
        colWidths=[None],
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PAPER_BG),
                ("BOX", (0, 0), (-1, -1), 1, LINE),
                ("LINEBEFORE", (0, 0), (0, -1), 3, TERRA),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        ),
    )
    return [title_card, table, Paragraph(f"合计：￥{model.get('totalAmount')}", _H_STYLES["total"])]


def _colophon() -> Paragraph:
    return Paragraph(
        "本刊由 Travel Assistant 排印生成 · 价格均为人民币估算，请以现场或官方渠道为准<br/>"
        "出发前请再次确认各点位营业时间与票价",
        _H_STYLES["colophon"],
    )


def _spaced(flowable: Any, space: int):
    """块间距靠外层表格的 padding 表达（reportlab 的 flowable 没有 margin 概念）。"""
    flowable.spaceAfter = space
    return flowable
