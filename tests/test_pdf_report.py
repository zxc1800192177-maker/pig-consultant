"""生產性能趨勢報告的 PDF 匯出。

不重新驗證趨勢怎麼算(那是 test_trend.py 的事),只驗證「同一包資料
排進 PDF 有沒有排對」:沒有記錄印 —— 不是 0,一個區段一頁,抬頭帶得到
牧場名稱與期間範圍。用 pypdf 把文字抽出來斷言,不只是「沒有丟例外」——
版面壞掉、文字漏掉這些問題,不解開實際內容看不出來。
"""
from datetime import date

import pytest
from pypdf import PdfReader
import io

import pdf_report


def extract_text(pdf_bytes: bytes) -> list:
    """回傳每一頁的文字,列表索引對應頁碼。"""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return [page.extract_text() or "" for page in reader.pages]


PERIODS = [
    {"key": "2025-10", "label": "2025/10", "start": "2025-10-01", "end": "2025-10-31"},
    {"key": "2025-11", "label": "2025/11", "start": "2025-11-01", "end": "2025-11-30"},
]

REPORT = {
    "periods": PERIODS,
    "sections": [
        {"key": "farrowing", "label": "分娩", "rows": [
            {"key": "alive_per_litter", "label": "窩均活仔數", "unit": "隻", "digits": 1,
             "better": "high", "values": [11.0, 12.2],
             "change": {"delta": 1.2, "pct": 10.9, "improved": True}},
            {"key": "stillborn_pct", "label": "死胎率", "unit": "%", "digits": 1,
             "better": "low", "values": [7.0, None],
             "change": None},
        ]},
        {"key": "weaning", "label": "離乳", "rows": [
            {"key": "wean_score", "label": "離乳仔豬評分", "unit": "分", "digits": 1,
             "better": "high", "values": [None, 4.2], "change": None},
        ]},
    ],
}


def test_reportlab_is_available_in_this_environment():
    """這條測試存在的意義是:CI 或另一台機器上如果忘了裝 reportlab,
    這裡先報錯,而不是等到匯出 PDF 那一刻才發現。"""
    assert pdf_report.available()


class TestBuildPdf:
    def test_produces_a_real_pdf(self):
        out = pdf_report.build_pdf(REPORT, "測試牧場", date(2026, 9, 3))
        assert out[:4] == b"%PDF"
        assert len(out) > 1000

    def test_one_page_per_section(self):
        out = pdf_report.build_pdf(REPORT, "測試牧場", date(2026, 9, 3))
        assert len(PdfReader(io.BytesIO(out)).pages) == len(REPORT["sections"])

    def test_header_carries_farm_name_and_range(self):
        out = pdf_report.build_pdf(REPORT, "合億畜牧場", date(2026, 9, 3))
        page1 = extract_text(out)[0]
        assert "合億畜牧場" in page1
        assert "2025/10" in page1 and "2025/11" in page1
        assert "2026-09-03" in page1
        assert "豬豬顧問" in page1

    def test_section_labels_and_metric_labels_appear(self):
        pages = extract_text(pdf_report.build_pdf(REPORT, "測試牧場", date(2026, 9, 3)))
        assert "分娩" in pages[0]
        assert "窩均活仔數" in pages[0]
        assert "離乳" in pages[1]
        assert "離乳仔豬評分" in pages[1]

    def test_missing_values_print_a_dash_not_zero(self):
        """0 是「這期真的沒有」,— 是「沒有記錄」—— 兩者意思完全不同
        (憲法第三條)。死胎率第二期是 None,PDF 裡不該看到 0.0%。
        """
        pages = extract_text(pdf_report.build_pdf(REPORT, "測試牧場", date(2026, 9, 3)))
        assert "0.0%" not in pages[0]
        assert "—" in pages[0]

    def test_values_are_formatted_with_their_unit(self):
        pages = extract_text(pdf_report.build_pdf(REPORT, "測試牧場", date(2026, 9, 3)))
        assert "11.0隻" in pages[0]
        assert "12.2隻" in pages[0]

    def test_no_sections_still_produces_a_readable_pdf(self):
        empty = {"periods": PERIODS, "sections": []}
        out = pdf_report.build_pdf(empty, "測試牧場", date(2026, 9, 3))
        assert out[:4] == b"%PDF"
        pages = extract_text(out)
        assert len(pages) == 1
        assert "沒有記錄" in pages[0]

    def test_no_periods_does_not_crash(self):
        """理論上 server.py 早就擋掉零期的請求,但這個函式本身不該假設
        呼叫端一定會先檢查 —— 空期間至少要能印出「沒有資料」而不是崩潰。
        """
        out = pdf_report.build_pdf({"periods": [], "sections": []},
                                   "測試牧場", date(2026, 9, 3))
        assert out[:4] == b"%PDF"

    def test_missing_farm_name_does_not_leave_a_stray_separator(self):
        # 沒有牧場名稱時,期間範圍要直接開頭,不能留下一個空的「・」——
        # 那看起來像資料漏了一截,而不是「這裡本來就沒有牧場名稱」。
        out = pdf_report.build_pdf(REPORT, "", date(2026, 9, 3))
        page1 = extract_text(out)[0]
        assert "2025/10" in page1
        assert "・ ・" not in page1
        assert not page1.split("報告")[1].lstrip().startswith("・")

    def test_change_color_direction_can_differ_from_the_arrow(self):
        """死胎率上升該是紅色的 ▲,不是綠色 —— 箭頭跟著數字正負,
        顏色跟著 improved,兩者不是同一件事。這裡至少確認兩種顏色
        (good/critical)在同一份文件裡都用得到,不會被實作成同一色。
        """
        report = {
            "periods": PERIODS,
            "sections": [{"key": "x", "label": "測試", "rows": [
                {"key": "a", "label": "改善", "unit": "%", "digits": 1, "better": "high",
                 "values": [1.0, 2.0], "change": {"delta": 1.0, "pct": 100.0, "improved": True}},
                {"key": "b", "label": "惡化", "unit": "%", "digits": 1, "better": "high",
                 "values": [2.0, 1.0], "change": {"delta": -1.0, "pct": -50.0, "improved": False}},
            ]}],
        }
        # 顏色套用不會影響抽出來的文字內容,這裡改確認兩種方向的文字都
        # 正確出現(▲/▼ 各自跟著自己的正負號)——上色本身在
        # _change_text() 已經有回傳值層級的單元測試涵蓋。
        text = extract_text(pdf_report.build_pdf(report, "測試牧場", date(2026, 9, 3)))[0]
        assert "▲" in text and "+1.0%" in text
        assert "▼" in text and "-1.0%" in text


class TestChangeText:
    """_change_text 是內部函式,但邏輯值得直接測 —— 跟
    trendreport.js 的 changeText() 是同一份規則,兩邊都要對。
    """

    def test_arrow_follows_the_sign_not_improved(self):
        worse = pdf_report._change_text(
            {"delta": 2.4, "pct": 34.3, "improved": False}, "%", 1)
        assert worse[0].startswith("▲")
        assert worse[1] == pdf_report.CRITICAL

        better = pdf_report._change_text(
            {"delta": -2.4, "pct": -34.3, "improved": True}, "%", 1)
        assert better[0].startswith("▼")
        assert better[1] == pdf_report.GOOD

    def test_no_change_is_a_dash_in_neutral_color(self):
        text, color = pdf_report._change_text(None, "隻", 1)
        assert text == "—"
        assert color == pdf_report.NEUTRAL

    def test_none_improved_is_neutral_color(self):
        _, color = pdf_report._change_text(
            {"delta": 5, "pct": 10, "improved": None}, "筆", 0)
        assert color == pdf_report.NEUTRAL
