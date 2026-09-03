"""生產性能趨勢報告的 PDF 匯出。直接產生檔案,不透過瀏覽器列印對話框
(使用者要求 —— 按一顆鈕就要拿到檔案,不是被丟進一個還要自己操作的
系統對話框)。

版面跟 web/lib/trendreport.js 的 trendPrintReport() 是同一份設計:抬頭
(標題、牧場名稱、期間範圍、產生時間)+ 每個區段一張表、變化欄紅綠標色。
前端那份純 JS 版本留著是因為 node --test 看得到它,是「離開這個 app 的
瀏覽器主題之後,列印用的畫面」;這裡是另一個輸出端,直接生出檔案交給
使用者,不必再經過「另存 PDF」那一步。

只認 `trend.trend_report()` 的輸出格式,不碰 HTTP 也不碰資料庫 ——
跟 schedule.py/trend.py 同樣的分層方式。
"""
import io
from datetime import date
from pathlib import Path
from typing import Optional

try:  # reportlab 只有要匯出 PDF 才需要;沒裝也要能 import 這個模組,
    # 跟 db.py 對 psycopg 的處理同一個原則 —— 沒裝的話這條路徑關閉,
    # 其餘功能(含 PDF 之外的 CSV 匯出)照常運作。
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )
except ImportError:  # pragma: no cover - 取決於環境有沒有裝
    colors = None

# 字型檔的來源、授權、為什麼要自己裁切過,見 data/fonts/README.md。
# 用真正嵌入的字型檔,不用 reportlab 內建的「標準 14 種 CJK 字型」——
# 那些不嵌字型檔,只靠 PDF 閱讀器自己找字替換,實測完全對不上號。
_FONTS_DIR = Path(__file__).parent / "data" / "fonts"
FONT = "NotoSansTC"
FONT_BOLD = "NotoSansTC-Bold"
_fonts_registered = False

GOOD = colors.HexColor("#1a7f37") if colors else None
CRITICAL = colors.HexColor("#c0392b") if colors else None
NEUTRAL = colors.HexColor("#666666") if colors else None
HEADER_BG = colors.HexColor("#2a201b") if colors else None
GRID = colors.HexColor("#cccccc") if colors else None


def available() -> bool:
    """reportlab 有沒有裝好。伺服器啟動時不需要它就能跑,只有真的要匯出
    PDF 的那個端點才檢查(見 server.py 的 _trend_report_pdf)。
    """
    return colors is not None


def _ensure_fonts() -> None:
    global _fonts_registered
    if _fonts_registered:
        return
    pdfmetrics.registerFont(TTFont(FONT, str(_FONTS_DIR / "NotoSansTC-Regular.ttf")))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, str(_FONTS_DIR / "NotoSansTC-Bold.ttf")))
    _fonts_registered = True


def _change_text(change: Optional[dict], unit: str, digits: int):
    """跟 trendreport.js 的 changeText() 是同一段邏輯,只是這裡順便決定
    顏色 —— 兩邊的箭頭都跟著數字本身的正負,顏色才跟著 `improved`
    (這個方向算不算進步),兩者可能相反:死胎率上升該是紅色的 ▲。
    """
    if not change:
        return "—", NEUTRAL
    delta = change["delta"]
    arrow = "▲" if delta > 0 else "▼" if delta < 0 else "→"
    sign = "+" if delta > 0 else ""
    text = f"{arrow} {sign}{delta:.{digits}f}{unit}"
    pct = change.get("pct")
    if pct is not None:
        text += f" ({'+' if pct > 0 else ''}{pct:.0f}%)"
    improved = change.get("improved")
    color = NEUTRAL if improved is None else (GOOD if improved else CRITICAL)
    return text, color


def _value_text(v, digits: int, unit: str) -> str:
    # 沒有記錄印 —,不是 0 —— 兩者意思完全不同(憲法第三條)。
    return "—" if v is None else f"{v:.{digits}f}{unit}"


def _section_table(section: dict, periods: list, body_style) -> Table:
    header = ["指標"] + [p["label"] for p in periods] + ["變化"]
    rows = [header]
    change_colors = []
    for r in section["rows"]:
        cells = [Paragraph(r["label"], body_style)]
        cells += [_value_text(v, r["digits"], r["unit"]) for v in r["values"]]
        text, color = _change_text(r.get("change"), r["unit"], r["digits"])
        cells.append(text)
        change_colors.append(color)
        rows.append(cells)

    n_periods = len(periods)
    label_w = 42 * mm
    other_w = (257 * mm - label_w) / (n_periods + 1) if n_periods else 100 * mm
    col_widths = [label_w] + [other_w] * (n_periods + 1)

    t = Table(rows, colWidths=col_widths, repeatRows=1)
    style = [
        ("FONT", (0, 0), (-1, -1), FONT, 6.6),
        ("FONT", (0, 0), (-1, 0), FONT_BOLD, 7.2),
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, GRID),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    for i, color in enumerate(change_colors, start=1):
        style.append(("TEXTCOLOR", (-1, i), (-1, i), color))
        style.append(("FONT", (-1, i), (-1, i), FONT, 6.6))
    t.setStyle(TableStyle(style))
    return t


def build_pdf(report: dict, farm_name: str, generated_at: date) -> bytes:
    """`report` 是 trend.trend_report() 的輸出(periods + sections)。

    每個區段各自一頁(PageBreak)—— 12 期 x 5 區段的表格全部接在一起,
    印出來會在頁與頁之間從表格中間硬切一刀,讀起來比多翻幾頁更難受。

    橫向 A4:期數多的橫式報表比較裝得下。期數真的多到一頁塞不下時,
    reportlab 會自動把表格擠得很窄或裁掉超出邊界的部分 —— 那是紙本報表
    本來就有的極限,跟畫面上要橫向捲動才看得完全同一個限制,不是這裡
    能解的(所以 server.py 的呼叫端只讓「畫面上正在看的這段範圍」走
    這條路,不接受任意寬的期數)。
    """
    if not available():
        raise RuntimeError("reportlab 沒有安裝,無法產生 PDF")
    _ensure_fonts()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm,
        title="生產性能趨勢分析報告",
    )
    title_style = ParagraphStyle("title", fontName=FONT_BOLD, fontSize=15, leading=18)
    meta_style = ParagraphStyle("meta", fontName=FONT, fontSize=9, leading=12,
                                textColor=colors.HexColor("#555555"))
    h_style = ParagraphStyle("h3", fontName=FONT_BOLD, fontSize=10.5, leading=13,
                             spaceBefore=10, spaceAfter=4,
                             textColor=colors.HexColor("#1c1512"))
    body_style = ParagraphStyle("body", fontName=FONT, fontSize=6.6, leading=8)

    periods = report.get("periods") or []
    range_text = (f"{periods[0]['label']} ~ {periods[-1]['label']}(共 {len(periods)} 期)"
                 if periods else "沒有資料")
    meta = " ・ ".join(filter(None, [
        farm_name, range_text, f"產生於 {generated_at.isoformat()}",
    ]))

    story = [
        Paragraph("豬豬顧問 生產性能趨勢分析報告", title_style),
        Paragraph(meta, meta_style),
        Spacer(1, 6),
    ]

    sections = report.get("sections") or []
    if not sections:
        story.append(Paragraph("這段期間沒有記錄,算不出任何指標。", body_style))
    for i, section in enumerate(sections):
        if i:
            story.append(PageBreak())
        story.append(Paragraph(section["label"], h_style))
        story.append(_section_table(section, periods, body_style))

    doc.build(story)
    return buf.getvalue()
