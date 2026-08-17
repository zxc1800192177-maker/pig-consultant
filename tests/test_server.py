"""HTTP 層測試。

伺服器只做路由、輸入檢查、限流,商業邏輯都在 core/ 與 ai/。
測試用假傳輸層,不消耗訂閱額度。
"""

import json
import re

import pytest

import config
from ai.transport import AnthropicApiTransport, FakeTransport, NotLoggedIn, QuotaExceeded
from server import CLEAR_SESSION_KEY, SET_SESSION_KEY, WEB_DIR, Application

# monkeypatch.setattr 已在每個測試結束後自動還原 config 的修改,不需額外處理。


@pytest.fixture
def app():
    return Application(transport=FakeTransport(chunks=["建議內容"]))


def _post(app, path, payload):
    return app.handle_post(path, json.dumps(payload).encode("utf-8"), client="test")


class TestHealth:
    def test_reports_ai_availability(self, app):
        status, body = app.handle_get("/api/health")
        assert status == 200
        assert "aiAvailable" in body

    def test_grading_always_available(self, app):
        """規格 6.5:生產健檢不依賴 AI,健康檢查要能表達這件事。"""
        broken = Application(transport=FakeTransport(error=QuotaExceeded("用盡")))
        _, body = broken.handle_get("/api/health")
        assert body["gradingAvailable"] is True


class TestMetricsEndpoint:
    """前端要靠這個端點動態產生輸入欄位,不該把指標清單寫死在畫面裡。"""

    def test_returns_only_gradable_metrics(self, app):
        status, body = app.handle_get("/api/metrics")
        assert status == 200
        assert len(body["metrics"]) == 18

    def test_each_metric_has_field_info(self, app):
        _, body = app.handle_get("/api/metrics")
        for metric in body["metrics"]:
            assert metric["key"] and metric["name"]
            assert "unit" in metric
            assert metric["definition"]

    def test_scale_metrics_not_offered_for_input(self, app):
        """規模型指標不評級,不該出現在輸入表單造成誤會。"""
        _, body = app.handle_get("/api/metrics")
        keys = {m["key"] for m in body["metrics"]}
        assert "total_services" not in keys


class TestIsWeakComesFromBackend:
    """弱項判斷規則只存在後端(DRY)。

    前端原本自己維護一份「D 級以下算弱項」的清單,跟 core/diagnosis.py 重複。
    兩份規則改一邊漏一邊不會報錯,只會讓畫面標示與實際排序不一致。
    改由 API 直接告訴前端每一項是不是弱項。
    """

    VALUES = {
        "psy": 20.63,            # D 級且低於平均 -> 是弱項
        "wean_to_service": 7.05,  # D 級但優於平均 -> 不是弱項
        "farrowing_index": 2.42,  # B 級 -> 不是弱項
    }

    def test_grades_carry_is_weak_flag(self, app):
        _, body = _post(app, "/api/grade", {"values": self.VALUES})
        for key, grade in body["grades"].items():
            assert "isWeak" in grade, f"{key} 缺少 isWeak 欄位"

    def test_is_weak_matches_the_ranking(self, app):
        """isWeak 為 true 的項目,必須恰好等於出現在改善清單裡的項目。

        這是最重要的一條:兩者若不一致,畫面標示會跟排序自相矛盾。
        """
        _, body = _post(app, "/api/grade", {"values": self.VALUES})
        flagged = {k for k, g in body["grades"].items() if g["isWeak"]}
        ranked = {w["key"] for w in body["weaknesses"]}
        assert flagged == ranked

    def test_below_median_but_above_mean_is_not_weak(self, app):
        """離乳到第一次配種間隔 7.05 天雖為 D 級,但優於全國平均 7.38,不算弱項。"""
        _, body = _post(app, "/api/grade", {"values": self.VALUES})
        assert body["grades"]["wean_to_service"]["isWeak"] is False

    def test_good_grade_is_not_weak(self, app):
        _, body = _post(app, "/api/grade", {"values": self.VALUES})
        assert body["grades"]["farrowing_index"]["isWeak"] is False

    def test_genuinely_behind_metric_is_weak(self, app):
        _, body = _post(app, "/api/grade", {"values": self.VALUES})
        assert body["grades"]["psy"]["isWeak"] is True


class TestGradeToAdviseRoundTrip:
    WEAKNESSES = [
        {"key": "psy", "name": "PSY", "grade": "F", "shortfallSd": 1.0,
         "improvement": "", "downstreamNames": []},
    ]

    @staticmethod
    def _events(app, payload, token=None):
        return list(app.advise_events(payload, client="test", token=token))

    @staticmethod
    def _text(events):
        return "".join(e.get("text", "") for e in events if e.get("type") == "delta")

    """實際踩過的 bug:/api/grade 回給瀏覽器的是駝峰式鍵名(shortfallSd),
    瀏覽器原封不動把它送回 /api/advise,但 ai/prompts.py 期待底線式
    (shortfall_sd),兩邊對不上導致 KeyError,伺服器 502,前端又沒有錯誤處理,
    卡在「顧問分析中…」不動。

    之前的單元測試都是手工塞 snake_case 資料呼叫 Consultant.advise(),
    從沒真正走過「/api/grade 的輸出 -> 直接餵給 /api/advise」這條完整路徑,
    所以這個命名不一致的問題一路通過 501 個測試才在真實環境爆出來。
    """

    def test_grade_output_can_feed_advise_directly(self, app):
        """這是最貼近瀏覽器實際行為的測試:不手工構造資料,
        而是先呼叫 /api/grade,把它回傳的 weaknesses 原封不動送進 /api/advise。
        """
        example = {
            "psy": 20.63, "weaning_age": 21.97, "preweaning_mortality": 20.21,
        }
        _, grade_body = _post(app, "/api/grade", {"values": example})
        assert grade_body["weaknesses"], "前置條件:至少要有一項弱項才測得到"

        status, advise_body = _post(app, "/api/advise", {
            "weaknesses": grade_body["weaknesses"],
        })

        assert status == 200, f"應成功,實際回應:{advise_body}"
        assert "advice" in advise_body

    def test_rejects_without_weaknesses(self, app):
        status, body = _post(app, "/api/advise-chat", {"question": "先做哪個比較好"})
        assert status == 400
        assert "健檢" in body["error"]

    def test_rejects_empty_question(self, app):
        status, _ = _post(app, "/api/advise-chat", {
            "weaknesses": self.WEAKNESSES, "question": "   ",
        })
        assert status == 400

    def test_rejects_overlong_question(self, app):
        status, _ = _post(app, "/api/advise-chat", {
            "weaknesses": self.WEAKNESSES,
            "question": "問" * (config.MAX_QUESTION_CHARS + 1),
        })
        assert status == 400

    def test_succeeds_with_weaknesses_and_question(self, app):
        status, body = _post(app, "/api/advise-chat", {
            "weaknesses": self.WEAKNESSES, "question": "先做哪個比較好",
        })
        assert status == 200
        assert body["answer"]

    def test_threads_reference_factors_into_the_prompt(self):
        transport = FakeTransport(chunks=["建議內容"])
        app = Application(transport=transport)
        _post(app, "/api/advise-chat", {
            "weaknesses": self.WEAKNESSES, "question": "先做哪個比較好",
            "referenceFactors": [{"name": "豬舍類型", "value": "開放式豬舍"}],
        })
        assert "豬舍類型" in transport.last_prompt
        assert "開放式豬舍" in transport.last_prompt

    def test_threads_history_into_the_prompt(self):
        transport = FakeTransport(chunks=["建議內容"])
        app = Application(transport=transport)
        _post(app, "/api/advise-chat", {
            "weaknesses": self.WEAKNESSES, "question": "那第二個呢",
            "history": [
                {"role": "user", "content": "先做哪個比較好"},
                {"role": "assistant", "content": "先處理離乳前死亡率"},
            ],
        })
        assert "先處理離乳前死亡率" in transport.last_prompt

    def test_error_event_carries_status(self):
        app = Application(transport=FakeTransport(error=QuotaExceeded("用盡")))
        events = self._events(app, {
            "weaknesses": self.WEAKNESSES, "question": "先做哪個比較好",
        })
        error = next(e for e in events if e["type"] == "error")
        assert error["status"] == 503
        assert error["reason"] == "quota"

    def test_deltas_then_done(self, app):
        events = self._events(app, {
            "weaknesses": self.WEAKNESSES, "question": "先做哪個比較好",
        })
        assert events[-1]["type"] == "done"
        assert any(e["type"] == "delta" for e in events)

    def test_blocked_without_login_when_accounts_enabled(self):
        app = _account_app()
        status, body = _post(app, "/api/advise-chat", {
            "weaknesses": self.WEAKNESSES, "question": "先做哪個比較好",
        })
        assert status == 401
        assert body.get("reason") == "login_required"

    def test_works_after_guest_login(self):
        app = _account_app()
        token = _post(app, "/api/auth/guest", {})[1][SET_SESSION_KEY]
        status, body = _post_as(app, "/api/advise-chat", {
            "weaknesses": self.WEAKNESSES, "question": "先做哪個比較好",
        }, token)
        assert status == 200
        assert body["answer"]

    def test_second_immediate_request_is_throttled(self, app):
        _post(app, "/api/advise-chat", {
            "weaknesses": self.WEAKNESSES, "question": "第一問",
        })
        status, body = _post(app, "/api/advise-chat", {
            "weaknesses": self.WEAKNESSES, "question": "第二問",
        })
        assert status == 429
        assert "秒" in body["error"]


class TestExampleEndpoint:
    """demo 用的範例資料(範例牧場,已取得授權)。"""

    def test_returns_full_farm_values(self, app):
        status, body = app.handle_get("/api/example")
        assert status == 200
        assert len(body["values"]) == 18
        assert body["values"]["psy"] == 20.63

    def test_example_grades_match_official_report(self, app):
        """範例跑完的結果必須與官方報告一致,這是 demo 的說服點。"""
        _, example = app.handle_get("/api/example")
        _, body = _post(app, "/api/grade", {"values": example["values"]})
        assert body["grades"]["psy"]["grade"] == "D"
        assert body["grades"]["preweaning_mortality"]["grade"] == "E"
        assert body["grades"]["weaning_age"]["grade"] == "F"


class TestAuthBarVisibilityBug:
    """實際回報過的 bug:登出後右上角的使用者名稱沒有消失。

    根因是 .authbar 這個 class 設了 display: flex,跟 [hidden] 屬性
    預設的 display: none 特異度相同 —— 作者的規則會贏。用
    `bar.hidden = true` 設定隱藏屬性,DOM 上的屬性有加上去,但畫面上
    毫無效果,舊的 innerHTML(登出前的使用者名稱)留在原地。

    修法是跟其餘所有隱藏邏輯一樣改用 .is-hidden(帶 !important,才真的
    蓋得過 display: flex)。這裡鎖住修法本身,不讓它被無意中改回去。
    """

    def test_authbar_code_uses_is_hidden_not_hidden_property(self):
        js = (WEB_DIR / "app.js").read_text("utf-8")
        section = js.split("function renderAuthBar()")[1].split("\nfunction ")[0]
        # 找的是「賦值」(bar.hidden = ...),不是任何提到這個字的地方 ——
        # 函式裡的說明註解本身就會提到 bar.hidden 這個寫法(解釋不要用它)。
        assert not re.search(r"bar\.hidden\s*=", section), (
            "authBar 改回用 .hidden 屬性設定可見度 —— .authbar 的 "
            "display: flex 特異度跟 [hidden] 相同,作者規則會贏,"
            "畫面不會真的隱藏(這正是登出後使用者名稱不消失的成因)"
        )

    def test_authbar_initial_html_state_uses_is_hidden(self):
        html = (WEB_DIR / "index.html").read_text("utf-8")
        tag = re.search(r'<div[^>]*id="authBar"[^>]*>', html)
        assert tag, "找不到 #authBar"
        assert "is-hidden" in tag.group(0), (
            "#authBar 初始狀態應該用 is-hidden class,不是 hidden 屬性"
        )
        assert " hidden" not in tag.group(0), (
            "#authBar 不該用 hidden 屬性 —— .authbar 的 display:flex 會蓋掉它"
        )

    def test_authbar_css_still_conflicts_with_hidden_attribute(self):
        """如果這條測試哪天失敗(代表 .authbar 不再設固定 display 了),
        以上兩條測試就可以拿掉 —— 但那之前,前兩條測試存在的理由都還在。
        """
        css = (WEB_DIR / "style.css").read_text("utf-8")
        rule = css.split(".authbar {")[1].split("}")[0]
        assert "display:" in rule.replace(" ", "")


class TestPwaAssets:
    """manifest / service worker 的檔案沒有動態產生,不會被一般測試碰到,
    改版時很容易漏改而沒人發現(圖示改名、家目錄挪動)。這裡鎖住兩件事:
    manifest 裡引用的每個檔案都真的存在,以及 service worker 的預快取清單
    不會誤吞 /api/* —— 那會讓串流回應被快取攔截。
    """

    def test_manifest_is_valid_json_with_required_fields(self):
        manifest = json.loads((WEB_DIR / "manifest.webmanifest").read_text("utf-8"))
        assert manifest["start_url"] == "/"
        assert manifest["display"] == "standalone"
        assert len(manifest["icons"]) >= 2

    def test_manifest_icons_exist_on_disk(self):
        manifest = json.loads((WEB_DIR / "manifest.webmanifest").read_text("utf-8"))
        for icon in manifest["icons"]:
            assert (WEB_DIR / icon["src"]).is_file(), f"manifest 引用但不存在:{icon['src']}"

    def test_apple_touch_icon_exists(self):
        assert (WEB_DIR / "icons" / "apple-touch-icon.png").is_file()

    @staticmethod
    def _sw_url_list(name):
        sw = (WEB_DIR / "sw.js").read_text("utf-8")
        block = sw.split(f"{name} = [")[1].split("]")[0]
        return re.findall(r'"(/[^"]*)"', block)

    def test_service_worker_cache_lists_have_no_api_paths(self):
        """一旦 /api/* 混進快取清單,SSE 串流會被攔截而整個斷掉。"""
        for name in ("CODE_URLS", "ASSET_URLS"):
            urls = self._sw_url_list(name)
            assert urls, f"沒解析到 {name},測試本身可能失效"
            assert not any(u.startswith("/api/") for u in urls)

    def test_service_worker_cached_files_exist_on_disk(self):
        for name in ("CODE_URLS", "ASSET_URLS"):
            for url in self._sw_url_list(name):
                path = WEB_DIR / "index.html" if url == "/" else WEB_DIR / url.lstrip("/")
                assert path.is_file(), f"sw.js 快取但不存在:{url}"

    def test_all_code_files_are_network_first(self):
        """HTML/CSS/JS 必須走網路優先。

        走快取優先時真實發生過兩件事:部署後第一次載入必定是舊版;
        以及各檔案獨立更新造成「舊 HTML + 新 JS」,新 JS 找不到元素而
        讓整頁按鈕失效。這條測試確保不會有人把程式碼檔案挪回素材清單。
        """
        code_urls = set(self._sw_url_list("CODE_URLS"))
        for path in WEB_DIR.rglob("*"):
            if path.suffix not in (".js", ".css", ".html"):
                continue
            if path.name == "sw.js":      # service worker 由瀏覽器自己管理更新
                continue
            url = "/" + path.relative_to(WEB_DIR).as_posix()
            assert url in code_urls or (url == "/index.html" and "/" in code_urls), (
                f"{url} 是程式碼卻不在 CODE_URLS,會走快取優先而可能與其他檔案版本錯配"
            )

    def test_code_and_asset_lists_do_not_overlap(self):
        """同一個路徑落在兩份清單會讓行為取決於程式碼順序,不該存在。"""
        overlap = set(self._sw_url_list("CODE_URLS")) & set(self._sw_url_list("ASSET_URLS"))
        assert not overlap, f"重複出現在兩份清單:{overlap}"

    def test_index_html_links_manifest_and_service_worker_registration(self):
        html = (WEB_DIR / "index.html").read_text("utf-8")
        assert 'rel="manifest"' in html
        js = (WEB_DIR / "app.js").read_text("utf-8")
        assert "serviceWorker" in js and "register(" in js

    def test_app_bootstraps_itself(self):
        """app.js 必須真的呼叫 init()。

        剝掉 v1 程式碼時把這行一起刪掉過:沒有任何錯誤訊息,console 全乾淨,
        只是每個標籤停在「載入中…」,看起來像後端沒回應。查了很久才發現
        前端根本沒開始跑。
        """
        js = (WEB_DIR / "app.js").read_text("utf-8")
        assert re.search(r"^init\(\);", js, re.MULTILINE), (
            "app.js 定義了 init() 卻沒有呼叫,整個前端不會啟動"
        )

    def test_no_references_to_removed_v1_elements(self):
        """app.js 不可再碰 v1 已刪掉的元素。

        `$("askBtn").disabled = true` 就這樣留了下來 —— askBtn 是 v1 疾病
        諮詢的按鈕,index.html 早就沒有了。它藏在「AI 不可用」分支裡,
        平常跑不到,一旦額度用盡就拋 TypeError 讓 init() 中斷,連純計算的
        生產健檢都一起打不開,正好是憲法第二條要防的情形。
        """
        js = (WEB_DIR / "app.js").read_text("utf-8")
        html = (WEB_DIR / "index.html").read_text("utf-8")
        # 改善建議卡與匯入確認鈕是 app.js 自己用模板長出來的,index.html 裡
        # 當然找不到 —— 兩邊都算數,只揪出哪邊都沒有的。
        missing = sorted({
            el_id for el_id in re.findall(r'\$\("([A-Za-z][\w-]*)"\)', js)
            if f'id="{el_id}"' not in html and f'id="{el_id}"' not in js
        })
        assert not missing, f"app.js 取用了不存在的元素:{missing}"


class TestStaticCacheHeaders:
    """靜態檔的快取標頭。

    這裡開真的 socket 而不是呼叫 Application —— 要驗的正是 HTTP 層,
    Application 根本看不到這些標頭。

    實際踩到的 bug:改完 app.js、重啟伺服器、重新整理,分頁跑的仍是舊檔,
    而且 console 一直報早已刪掉的變數名。原因是伺服器只送 Last-Modified、
    不送 Cache-Control,瀏覽器就自行猜一段新鮮期,期間完全不回頭問。
    """

    @staticmethod
    def _get(path, headers=None):
        import http.client
        import http.server
        import threading

        from server import Handler

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=srv.handle_request, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=5)
            conn.request("GET", path, headers=headers or {})
            resp = conn.getresponse()
            resp.read()
            return resp
        finally:
            conn.close()
            thread.join(timeout=5)
            srv.server_close()

    @pytest.mark.parametrize("path", ["/", "/app.js", "/style.css"])
    def test_code_files_must_be_revalidated(self, path):
        resp = self._get(path)
        assert resp.status == 200
        assert resp.getheader("Cache-Control") == "no-cache", (
            f"{path} 沒有 Cache-Control,瀏覽器會自行猜新鮮期而繼續跑舊程式碼"
        )

    def test_header_is_sent_once(self):
        """重複的 Cache-Control 語意由瀏覽器自行解讀,不該讓它有得猜。"""
        values = [v for k, v in self._get("/app.js").getheaders()
                  if k.lower() == "cache-control"]
        assert values == ["no-cache"]

    def test_unchanged_file_still_returns_304(self):
        """no-cache 是「用之前先問」,不是「每次重傳」。

        條件式請求仍要回 304 —— 否則每次開 App 都重新下載整包程式碼,
        豬舍網路慢的時候使用者會直接感覺得到。
        """
        first = self._get("/app.js")
        last_modified = first.getheader("Last-Modified")
        assert last_modified, "沒有 Last-Modified,瀏覽器無從發出條件式請求"

        again = self._get("/app.js", {"If-Modified-Since": last_modified})
        assert again.status == 304

    def test_api_responses_do_not_get_the_static_header(self):
        """API 與 SSE 各自送自己的標頭,靜態檔的規則不可外溢。

        靜態檔是 no-cache(用之前先問),API 要比它更嚴 —— 見下面那條。
        """
        resp = self._get("/api/health")
        assert resp.status == 200
        assert resp.getheader("Cache-Control") != "no-cache"

    @pytest.mark.parametrize("path", ["/api/health", "/api/sows", "/api/monthly-report"])
    def test_api_responses_are_never_stored(self, path):
        """API 回應一律 no-store。

        瀏覽器的快取是**以網址為鍵**的,跟哪個帳號登入無關。少了這個
        標頭,同一台電腦上換一個帳號登入時,/api/sows 這種每個帳號內容
        都不同、網址卻完全一樣的請求,可能被端出上一個帳號留在磁碟
        快取裡的回應 —— 也就是把 A 牧場的資料顯示給 B 看(憲法第十一條)。

        no-cache 不夠:那只要求「用之前先問」,回應仍然會被寫進磁碟,
        別的使用者拿得到這台電腦就翻得出來。no-store 才是「不准存」。
        """
        resp = self._get(path)
        assert resp.getheader("Cache-Control") == "no-store", (
            f"{path} 沒有 no-store,換帳號登入時可能讀到上一個帳號的快取"
        )


# --- 帳號系統 ---
#
# 全部用 InMemoryStore 注入,不連真的資料庫:測試要能離線跑、幾秒跑完,
# 而且不會因為外部服務不穩就變成紅燈。真的接資料庫的驗證另外手動做。

def _account_app():
    from db import InMemoryStore
    return Application(transport=FakeTransport(chunks=["建議內容"]), store=InMemoryStore())


def _register(app, username="farmer", password="hunter2hunter2"):
    """註冊並回傳 session token,供後續請求使用。"""
    status, body = _post(app, "/api/auth/register",
                         {"username": username, "password": password})
    assert status == 200, body
    return body[SET_SESSION_KEY]


def _post_as(app, path, payload, token):
    """帶著 session 的 POST。"""
    return app.handle_post(
        path, json.dumps(payload).encode("utf-8"), client="test", token=token
    )


class TestAccountsDisabledWithoutDatabase:
    """沒設定 DATABASE_URL 時帳號功能關閉,其餘功能完全不受影響 ——
    這是這個站的核心賣點:免帳號就能用,帳號只是加值。
    """

    def test_health_reports_accounts_unavailable(self, app):
        _, body = app.handle_get("/api/health")
        assert body["accountsAvailable"] is False

    def test_auth_endpoints_report_unavailable_not_crash(self, app):
        status, body = _post(app, "/api/auth/login",
                             {"username": "farmer", "password": "hunter2hunter2"})
        assert status == 503
        assert "error" in body

    def test_grade_still_works(self, app):
        assert _post(app, "/api/grade", {"values": {"psy": 20.63}})[0] == 200

    def test_me_reports_logged_out(self, app):
        status, body = app.handle_get("/api/auth/me")
        assert status == 200
        assert body["loggedIn"] is False


class TestAuthEndpoints:
    def test_health_reports_accounts_available(self):
        _, body = _account_app().handle_get("/api/health")
        assert body["accountsAvailable"] is True

    def test_register_then_me(self):
        app = _account_app()
        token = _register(app)
        status, body = app.handle_get("/api/auth/me", token)
        assert status == 200
        assert body["loggedIn"] is True
        assert body["username"] == "farmer"
        assert body["isGuest"] is False

    def test_duplicate_username_is_409(self):
        app = _account_app()
        _register(app)
        status, _ = _post(app, "/api/auth/register",
                          {"username": "farmer", "password": "another-password"})
        assert status == 409

    def test_weak_password_is_400(self):
        app = _account_app()
        status, _ = _post(app, "/api/auth/register",
                          {"username": "farmer", "password": "short"})
        assert status == 400

    def test_wrong_password_is_401(self):
        app = _account_app()
        _register(app)
        status, _ = _post(app, "/api/auth/login",
                          {"username": "farmer", "password": "wrong-password"})
        assert status == 401

    def test_login_returns_a_session(self):
        app = _account_app()
        _register(app)
        status, body = _post(app, "/api/auth/login",
                             {"username": "farmer", "password": "hunter2hunter2"})
        assert status == 200
        assert body[SET_SESSION_KEY]

    def test_logout_clears_the_session(self):
        app = _account_app()
        token = _register(app)
        status, body = _post_as(app, "/api/auth/logout", {}, token)
        assert status == 200
        assert body[CLEAR_SESSION_KEY] is True
        assert app.handle_get("/api/auth/me", token)[1]["loggedIn"] is False

    def test_invalid_token_is_treated_as_logged_out(self):
        app = _account_app()
        _, body = app.handle_get("/api/auth/me", "not-a-real-token")
        assert body["loggedIn"] is False

    def test_guest_login_creates_a_usable_identity(self):
        app = _account_app()
        status, body = _post(app, "/api/auth/guest", {})
        assert status == 200
        assert body["isGuest"] is True
        assert body["username"] is None
        assert app.handle_get("/api/auth/me", body[SET_SESSION_KEY])[1]["loggedIn"] is True

    def test_guest_can_save_and_read_own_data(self):
        app = _account_app()
        token = _post(app, "/api/auth/guest", {})[1][SET_SESSION_KEY]

        status, _ = _post_as(app, "/api/health-checks", {"values": {"psy": 20.63}}, token)
        assert status == 200
        assert len(app.handle_get("/api/health-checks", token)[1]["records"]) == 1

    def test_claim_keeps_the_data(self):
        app = _account_app()
        token = _post(app, "/api/auth/guest", {})[1][SET_SESSION_KEY]
        _post_as(app, "/api/health-checks", {"values": {"psy": 20.63}}, token)

        status, body = _post_as(
            app, "/api/auth/claim",
            {"username": "farmer", "password": "hunter2hunter2"}, token,
        )
        assert status == 200
        assert body["isGuest"] is False
        assert len(app.handle_get("/api/health-checks", token)[1]["records"]) == 1

    def test_registered_account_cannot_be_reclaimed(self):
        app = _account_app()
        token = _register(app)
        status, _ = _post_as(
            app, "/api/auth/claim",
            {"username": "other", "password": "hunter2hunter2"}, token,
        )
        assert status == 409

    def test_claim_without_session_is_rejected(self):
        app = _account_app()
        status, _ = _post(app, "/api/auth/claim",
                          {"username": "farmer", "password": "hunter2hunter2"})
        assert status == 401


class TestHealthCheckHistory:
    def test_requires_login(self):
        app = _account_app()
        assert app.handle_get("/api/health-checks")[0] == 401
        assert _post(app, "/api/health-checks", {"values": {"psy": 20.63}})[0] == 401

    def test_saved_record_comes_back_with_computed_grades(self):
        app = _account_app()
        token = _register(app)
        _post_as(app, "/api/health-checks", {"values": {"psy": 20.63}}, token)

        record = app.handle_get("/api/health-checks", token)[1]["records"][0]
        # 級距是讀取時即時算的,不是存起來的(單一事實來源)
        assert record["grades"]["psy"] == "D"
        assert record["values"]["psy"] == 20.63
        assert record["createdAt"]

    def test_invalid_values_are_rejected_before_saving(self):
        """壞資料一旦存進去,之後每次讀歷史都會再壞一次。"""
        app = _account_app()
        token = _register(app)
        status, _ = _post_as(app, "/api/health-checks", {"values": {"psy": "不是數字"}}, token)
        assert status == 400
        assert app.handle_get("/api/health-checks", token)[1]["records"] == []

    def test_empty_values_rejected(self):
        app = _account_app()
        token = _register(app)
        assert _post_as(app, "/api/health-checks", {"values": {}}, token)[0] == 400

    def test_newest_first(self):
        app = _account_app()
        token = _register(app)
        for psy in (20.0, 21.0, 22.0):
            _post_as(app, "/api/health-checks", {"values": {"psy": psy}}, token)

        records = app.handle_get("/api/health-checks", token)[1]["records"]
        assert [r["values"]["psy"] for r in records] == [22.0, 21.0, 20.0]

    def test_one_user_cannot_see_anothers_records(self):
        app = _account_app()
        alice = _register(app, "alice")
        bob = _register(app, "bob")
        _post_as(app, "/api/health-checks", {"values": {"psy": 20.63}}, alice)

        assert app.handle_get("/api/health-checks", bob)[1]["records"] == []

    def test_one_user_cannot_delete_anothers_record(self):
        app = _account_app()
        alice = _register(app, "alice")
        bob = _register(app, "bob")
        _, created = _post_as(app, "/api/health-checks", {"values": {"psy": 20.63}}, alice)

        assert app.handle_delete(f"/api/health-checks/{created['id']}", bob)[0] == 404
        assert len(app.handle_get("/api/health-checks", alice)[1]["records"]) == 1


class TestLoginGate:
    """兩項核心功能要先登入(含訪客)才能用。

    前端會把功能畫面藏起來,但那只是介面 —— 真正的限制必須在後端,
    否則任何人直接呼叫 API 就繞過去了,而疾病諮詢每次呼叫都在花錢。
    """

    def test_grade_blocked_without_login(self):
        app = _account_app()
        status, body = _post(app, "/api/grade", {"values": {"psy": 20.63}})
        assert status == 401
        assert body.get("reason") == "login_required"

    def test_advise_blocked_without_login(self):
        app = _account_app()
        status, _ = _post(app, "/api/advise", {"weaknesses": [
            {"key": "psy", "name": "PSY", "grade": "F", "shortfallSd": 1.0,
             "improvement": "", "downstreamNames": []},
        ]})
        assert status == 401

    def test_works_after_registering(self):
        app = _account_app()
        token = _register(app)
        assert _post_as(app, "/api/grade", {"values": {"psy": 20.63}}, token)[0] == 200

    def test_expired_or_forged_token_is_still_blocked(self):
        app = _account_app()
        assert _post_as(app, "/api/grade", {"values": {"psy": 20.63}}, "forged")[0] == 401

    def test_health_endpoint_announces_the_requirement(self):
        _, body = _account_app().handle_get("/api/health")
        assert body["loginRequired"] is True

    def test_auth_endpoints_stay_open(self):
        """登入相關的端點本身不能被門檻擋住,否則沒有人進得來。"""
        app = _account_app()
        assert app.handle_get("/api/auth/me")[0] == 200
        assert _post(app, "/api/auth/guest", {})[0] == 200
        assert app.handle_get("/api/health")[0] == 200


class TestLoginGateDisabledWithoutDatabase:
    """沒有資料庫時不得把所有人鎖在門外。

    資料庫故障或本機開發沒設 DATABASE_URL 時,網站要降級成免帳號可用,
    而不是整個不能用 —— 否則一個外部服務出問題就等於全站停擺。
    """

    def test_grade_still_open(self, app):
        assert _post(app, "/api/grade", {"values": {"psy": 20.63}})[0] == 200

    def test_health_reports_no_requirement(self, app):
        _, body = app.handle_get("/api/health")
        assert body["loginRequired"] is False


class TestLoginGateCanBeTurnedOff:
    """REQUIRE_LOGIN=0 時退回「帳號是選填」的行為。"""

    def test_health_reflects_the_setting(self, monkeypatch):
        monkeypatch.setattr(config, "REQUIRE_LOGIN", False)
        _, body = _account_app().handle_get("/api/health")
        assert body["loginRequired"] is False


class TestLoginThrottle:
    """密碼可以被暴力猜,訪客建立會寫入資料庫 —— 兩者都要設限。"""

    def test_repeated_attempts_are_throttled(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_LOGIN_ATTEMPTS_PER_WINDOW", 3)
        app = _account_app()
        for _ in range(3):
            _post(app, "/api/auth/login", {"username": "farmer", "password": "guess"})

        assert _post(app, "/api/auth/login",
                     {"username": "farmer", "password": "guess"})[0] == 429

    def test_guest_creation_is_throttled_too(self, monkeypatch):
        """不設限等於開放任何人把免費方案的資料庫容量灌爆。"""
        monkeypatch.setattr(config, "MAX_LOGIN_ATTEMPTS_PER_WINDOW", 3)
        app = _account_app()
        for _ in range(3):
            _post(app, "/api/auth/guest", {})

        assert _post(app, "/api/auth/guest", {})[0] == 429

    def test_logout_is_not_throttled(self, monkeypatch):
        """登出被擋住會讓使用者卡在登入狀態出不去。"""
        monkeypatch.setattr(config, "MAX_LOGIN_ATTEMPTS_PER_WINDOW", 1)
        app = _account_app()
        token = _register(app)
        for _ in range(5):
            assert _post_as(app, "/api/auth/logout", {}, token)[0] == 200


# --- 藥品標示拍照辨識 ---

_JPEG = b"\xff\xd8\xff" + b"\x00" * 40      # 檔頭正確的最小假 JPEG
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def _image_payload(raw=_JPEG, media_type="image/jpeg"):
    import base64
    return {"image": {"mediaType": media_type, "data": base64.b64encode(raw).decode()}}


def _label_app(chunks=None, error=None):
    from db import InMemoryStore
    return Application(
        transport=FakeTransport(chunks=chunks or ['{"name": "阿莫西林", "withdrawalDays": 7}'],
                                error=error),
        store=InMemoryStore(),
    )


