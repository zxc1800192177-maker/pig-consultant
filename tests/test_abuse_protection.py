"""濫用防護測試。

三道防線:
  1. 真實使用者識別 —— 取得代理後方的真實 IP,否則所有人共用一個額度
  2. 每 IP 每小時 20 次 —— 伺服器端強制,不可由前端繞過
  3. 對話歷史上限 —— 由前端帶上來,但伺服器必須自己設限

背景:上線後發現伺服器看到的來源永遠是 127.0.0.1(Render 的代理),
代表原本的「每人」限制實際上是全球共用 —— 一個人送出請求會擋住所有人。
"""

import json
import threading
import time

import pytest

import config
from ai.transport import FakeTransport
from server import Application, client_ip


def _post(app, path, payload, client="1.1.1.1"):
    return app.handle_post(path, json.dumps(payload).encode("utf-8"), client=client)


class TestClientIdentification:
    """代理後方的真實 IP 判定。"""

    def test_uses_socket_address_when_no_proxy(self):
        """本機開發沒有代理,直接用連線來源。"""
        assert client_ip({}, "203.0.113.9") == "203.0.113.9"

    def test_uses_forwarded_header_behind_proxy(self):
        headers = {"X-Forwarded-For": "203.0.113.9"}
        assert client_ip(headers, "127.0.0.1") == "203.0.113.9"

    def test_skips_infrastructure_hops_from_the_end(self, monkeypatch):
        """從尾端往回跳過基礎設施層,取第一個非基礎設施的位址。

        正式環境實測的轉發鏈:
            203.204.236.67, 172.71.146.124, 10.28.196.132
              真實使用者        Cloudflare      Render 內部

        最後一段是 Render 的內部負載平衡器,而且**每次請求都不同**
        (10.25.32.132 / 10.28.196.132 / 10.28.128.130),
        導致每次都被當成新使用者,限流完全失效。

        也不能單純取第一個 —— 那段是使用者送什麼就是什麼,
        等於讓攻擊者自行指定身分、無限重置額度。
        """
        monkeypatch.setattr(config, "TRUSTED_PROXY_HOPS", 2)
        headers = {
            "X-Forwarded-For": "203.204.236.67, 172.71.146.124, 10.28.196.132"
        }
        assert client_ip(headers, "127.0.0.1") == "203.204.236.67"

    def test_same_user_gets_same_id_despite_changing_infra_hops(self, monkeypatch):
        """同一個使用者的身分不得因為內部節點輪替而改變。

        這正是限流失效的直接原因。
        """
        monkeypatch.setattr(config, "TRUSTED_PROXY_HOPS", 2)
        ids = {
            client_ip(
                {"X-Forwarded-For": f"203.204.236.67, 172.71.146.124, {infra}"},
                "127.0.0.1",
            )
            for infra in ("10.25.32.132", "10.28.196.132", "10.28.128.130")
        }
        assert ids == {"203.204.236.67"}

    def test_forged_leading_entries_do_not_win(self, monkeypatch):
        """偽造前導項換不到新身分。

        攻擊者送出 'X-Forwarded-For: 9.9.9.9' 時,代理會把真實來源接在後面,
        偽造值被推到最前面。砍掉尾端的基礎設施層後,取到的仍是攻擊者的
        真實 IP —— 他還是受同一份額度約束,偽造沒有好處。
        """
        monkeypatch.setattr(config, "TRUSTED_PROXY_HOPS", 2)
        headers = {
            "X-Forwarded-For": "9.9.9.9, 203.204.236.67, 172.71.146.124, 10.28.196.132"
        }
        assert client_ip(headers, "127.0.0.1") == "203.204.236.67"
        assert client_ip(headers, "127.0.0.1") != "9.9.9.9"

    def test_short_chain_falls_back_to_first_available(self, monkeypatch):
        """鏈比預期短時取最前面的,不可回傳空值或爆掉。"""
        monkeypatch.setattr(config, "TRUSTED_PROXY_HOPS", 2)
        assert client_ip({"X-Forwarded-For": "203.0.113.9"}, "127.0.0.1") == "203.0.113.9"

    def test_handles_spaces(self, monkeypatch):
        monkeypatch.setattr(config, "TRUSTED_PROXY_HOPS", 2)
        headers = {"X-Forwarded-For": "203.0.113.9,  5.6.7.8 , 10.0.0.1"}
        assert client_ip(headers, "127.0.0.1") == "203.0.113.9"

    def test_ignores_empty_header(self):
        assert client_ip({"X-Forwarded-For": ""}, "203.0.113.9") == "203.0.113.9"

    def test_header_name_is_case_insensitive(self):
        headers = {"x-forwarded-for": "203.0.113.9"}
        assert client_ip(headers, "127.0.0.1") == "203.0.113.9"


class TestMemoryBounded:
    """記錄不能無限成長,否則防護本身變成攻擊面。"""

    def test_stale_ips_are_pruned(self, monkeypatch):
        monkeypatch.setattr(config, "MIN_REQUEST_INTERVAL_SEC", 0)
        app = Application(transport=FakeTransport(chunks=["ok"]))

        for i in range(50):
            _post(app, "/api/consult", {"question": "問題"}, client=f"10.0.0.{i}")

        real = time.monotonic()
        monkeypatch.setattr(time, "monotonic", lambda: real + 7200)
        _post(app, "/api/consult", {"question": "觸發清理"}, client="10.0.1.1")

        assert len(app._hourly_hits) <= 2, (
            f"過期 IP 未被清理,累積 {len(app._hourly_hits)} 筆"
        )


class TestRequestSizeLimit:
    """請求體大小上限。

    資安稽核實測:送出 19.7 MB 請求體,伺服器照單全收並正常處理。
    更關鍵的是順序 —— do_POST 先把整包讀進記憶體,之後才輪到限流,
    所以「每小時 20 次」完全擋不住這件事。Render 免費方案只有 512MB 記憶體。
    """

    def test_limit_is_configured(self):
        assert config.MAX_REQUEST_BYTES > 0
        # 正常請求:2000 字問題 + 20 則歷史 × 500 字 ≈ 30KB,64KB 綽綽有餘
        assert config.MAX_REQUEST_BYTES <= 128 * 1024

    def test_oversized_body_is_rejected(self):
        from server import too_large
        assert too_large(config.MAX_REQUEST_BYTES + 1) is True

    def test_normal_body_is_allowed(self):
        from server import too_large
        assert too_large(30 * 1024) is False

    def test_missing_length_is_allowed(self):
        from server import too_large
        assert too_large(0) is False

    def test_real_payload_fits_comfortably(self):
        """實際最大合法請求要能通過,不能把正常使用者擋掉。"""
        from server import too_large
        payload = json.dumps({
            "question": "痢" * config.MAX_QUESTION_CHARS,
            "history": [{"role": "user", "content": "痢" * config.MAX_HISTORY_CHARS}]
                       * config.MAX_HISTORY_TURNS,
        }, ensure_ascii=False).encode("utf-8")
        assert too_large(len(payload)) is False, (
            f"合法請求 {len(payload)} bytes 被擋,上限設太小"
        )


