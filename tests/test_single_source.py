"""單一事實來源(DRY)守衛。

同一條規則寫在兩個地方時,改了一邊漏另一邊不會有任何錯誤提示,
只會安靜地產生不一致的行為。這裡的測試讓「重複」變成會失敗的建置條件。

背景:曾經 data/benchmark_2025.json 有一份 bands,core/grading.py 也有一份,
而且程式碼從來沒讀過 JSON 那份——有人改資料檔以為會改變評級規則,
結果什麼都不會發生。
"""

import ast
import pathlib

import pytest

from core import grading
from core.benchmark import BENCHMARK, bands

PROJECT_ROOT = pathlib.Path(__file__).parent.parent


class TestBandsSingleSource:
    """分級切點以資料檔為準,程式碼不得自己再寫一份。"""

    def test_data_file_defines_bands(self):
        assert "bands" in BENCHMARK, "資料檔必須定義 bands(單一事實來源)"

    def test_grading_uses_data_file_bands(self):
        assert grading.BANDS == bands()

    def test_bands_match_official_report(self):
        """A級<10%, B級10~25%, C級25~50%, D級50~75%, E級75~90%, F級>90%"""
        assert bands() == [(10, "A"), (25, "B"), (50, "C"), (75, "D"), (90, "E")]

    def test_grading_source_has_no_literal_band_list(self):
        """掃描原始碼:grading.py 不得再出現寫死的切點清單。

        光比對數值相等不夠——兩邊都寫死成一樣的值也會通過。
        這裡直接檢查程式碼裡沒有第二份定義。
        """
        source = (PROJECT_ROOT / "core" / "grading.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            # 找形如 [(10, "A"), (25, "B"), ...] 的字面值
            pairs = [
                el for el in node.elts
                if isinstance(el, ast.Tuple) and len(el.elts) == 2
                and isinstance(el.elts[0], ast.Constant)
                and isinstance(el.elts[1], ast.Constant)
                and isinstance(el.elts[1].value, str)
                and len(el.elts[1].value) == 1
            ]
            assert len(pairs) < 3, (
                "grading.py 出現寫死的分級切點清單。"
                "切點應只定義在 data/benchmark_2025.json,由程式讀取。"
            )


class TestWeaknessRuleSingleSource:
    """弱項判斷規則只存在後端。前端不得自行再定義一份。"""

    FORMAT_JS = PROJECT_ROOT / "web" / "lib" / "format.js"

    def test_frontend_does_not_define_weak_grade_set(self):
        source = self.FORMAT_JS.read_text(encoding="utf-8")
        assert '"D", "E", "F"' not in source, (
            "前端自行定義了弱項級距清單。這條規則屬於後端,"
            "應由 API 回傳 isWeak 欄位告知前端。"
        )

    def test_frontend_exports_no_is_weak(self):
        source = self.FORMAT_JS.read_text(encoding="utf-8")
        assert "export function isWeak" not in source, (
            "前端不該有自己的弱項判斷函式,應改用後端回傳的 isWeak 欄位。"
        )


class TestUserFacingTextSingleSource:
    """給使用者看的文字只寫一次。措辭一改就要全站同步,不該散落多處。"""

    APP_JS = PROJECT_ROOT / "web" / "app.js"
    SERVER_PY = PROJECT_ROOT / "server.py"

    def test_ai_unavailable_note_defined_once(self):
        from core.labels import ai_unavailable_note

        note = ai_unavailable_note()
        assert note

        for path in (self.APP_JS, self.SERVER_PY):
            source = path.read_text(encoding="utf-8")
            assert note not in source, (
                f"{path.name} 內嵌了 AI 不可用的提示文字。"
                "該文字唯一來源是 core/labels.py,呼叫它而非複製一份。"
            )

    def test_health_endpoint_supplies_the_note(self):
        """前端要拿得到這段文字,才不需要自己寫一份。"""
        from ai.transport import FakeTransport
        from server import Application

        app = Application(transport=FakeTransport(chunks=["ok"]))
        _, body = app.handle_get("/api/health")
        assert body["aiUnavailableNote"]
