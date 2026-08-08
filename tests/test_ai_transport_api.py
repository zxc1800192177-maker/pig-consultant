"""API 傳輸層測試 —— 對外上線用,取代訂閱額度的 CLI 傳輸。

憲法第五條原文只提訂閱額度,現在新增這條路徑:個人訂閱不得用於
服務外部客戶,對外上線前必須改用 API 計費,見 README。

這一層不呼叫真實 API,用假的 HTTP handler 驗證行為,不花錢。
"""

import json

import pytest

from ai.transport import AnthropicApiTransport, NotLoggedIn, QuotaExceeded, TransportError


class FakeHttpResponse:
    """模擬 http.client.HTTPResponse,只提供程式碼實際用到的介面。"""

    def __init__(self, status, lines):
        self.status = status
        self._lines = iter(lines)

    def __iter__(self):
        return self._lines

    def read(self):
        return b"".join(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _event(delta_text=None, done=False, error=None):
    if error:
        payload = {"type": "error", "error": {"message": error}}
    elif done:
        payload = {"type": "message_stop"}
    else:
        payload = {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": delta_text},
        }
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def _message_delta(stop_reason):
    payload = {"type": "message_delta", "delta": {"stop_reason": stop_reason}}
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def _non_text_delta(kind):
    """thinking_delta / signature_delta 等非文字事件 —— 曾實際觀測到:
    模型把整個 token 額度用在這類事件上,content_block_delta 從未帶 text_delta。
    """
    payload = {"type": "content_block_delta", "delta": {"type": kind}}
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


class TestRequestConstruction:
    """驗證送給 Anthropic API 的請求內容,不實際發送。"""

    @pytest.fixture
    def transport(self):
        return AnthropicApiTransport(api_key="sk-test-key")

    def test_uses_messages_endpoint(self, transport):
        req = transport.build_request("問題", "系統提示")
        assert req.full_url == "https://api.anthropic.com/v1/messages"

    def test_carries_api_key_header_not_bearer(self, transport):
        """Messages API 用 x-api-key,不是 Authorization: Bearer。"""
        req = transport.build_request("問題", "系統提示")
        assert req.get_header("X-api-key") == "sk-test-key"

    def test_carries_anthropic_version_header(self, transport):
        req = transport.build_request("問題", "系統提示")
        assert req.get_header("Anthropic-version")

    def test_body_contains_system_and_message(self, transport):
        req = transport.build_request("小豬下痢", "你是顧問")
        body = json.loads(req.data.decode("utf-8"))
        assert body["system"] == "你是顧問"
        assert body["messages"] == [{"role": "user", "content": "小豬下痢"}]

    def test_streaming_enabled(self, transport):
        req = transport.build_request("問題", "系統提示")
        body = json.loads(req.data.decode("utf-8"))
        assert body["stream"] is True

    def test_no_tools_passed(self, transport):
        """關鍵:不傳 tools 參數,模型就沒有任何工具可用 —— 這是這條路徑
        天生比 CLI 路徑更簡單的安全邊界,不需要 DENY_TOOLS 清單。"""
        req = transport.build_request("問題", "系統提示")
        body = json.loads(req.data.decode("utf-8"))
        assert "tools" not in body

    def test_uses_configured_model(self, transport):
        req = transport.build_request("問題", "系統提示")
        body = json.loads(req.data.decode("utf-8"))
        assert body["model"]


class TestStreamParsing:
    """Anthropic API 的 SSE 事件格式與 Claude Code CLI 不同,需要獨立解析。"""

    @pytest.fixture
    def transport(self):
        return AnthropicApiTransport(api_key="sk-test-key")

    def test_extracts_text_deltas(self, transport, monkeypatch):
        response = FakeHttpResponse(200, [
            b'event: content_block_delta\n',
            _event(delta_text="你好"),
            b'\n',
            b'event: message_stop\n',
            _event(done=True),
            b'\n',
        ])
        monkeypatch.setattr(transport, "_open", lambda req: response)
        assert list(transport.stream("問題", "系統提示")) == ["你好"]

    def test_multiple_deltas_concatenate_in_order(self, transport, monkeypatch):
        response = FakeHttpResponse(200, [
            _event(delta_text="第一"),
            b'\n',
            _event(delta_text="第二"),
            b'\n',
        ])
        monkeypatch.setattr(transport, "_open", lambda req: response)
        result = list(transport.stream("問題", "系統提示"))
        assert result == ["第一", "第二"]

    def test_ignores_non_delta_events_but_still_needs_real_text(self, transport, monkeypatch):
        """非文字事件本身會被忽略,但若整段串流完全沒有文字,
        現在視為錯誤而非成功的空清單(見 TestNoTextProduced)。
        """
        response = FakeHttpResponse(200, [
            (b'data: {"type":"message_start"}\n'),
            b'\n',
            (b'data: {"type":"content_block_start"}\n'),
            b'\n',
        ])
        monkeypatch.setattr(transport, "_open", lambda req: response)
        with pytest.raises(TransportError):
            list(transport.stream("問題", "系統提示"))


class TestNoTextProduced:
    """實際發生過的 bug:模型把整個 token 額度耗在內部思考上,
    stop_reason 變成 max_tokens,一個字的正式回答都沒輸出。

    原本這種情況會被當成「成功但空白」靜默結束,呼叫端顯示空白內容、
    使用者以為系統壞了卻看不到任何錯誤訊息。串流結束時必須明確檢查:
    完全沒有文字產出就是錯誤,不是成功的空結果。
    """

    @pytest.fixture
    def transport(self):
        return AnthropicApiTransport(api_key="sk-test-key")

    def test_raises_when_only_thinking_events_and_max_tokens(self, transport, monkeypatch):
        response = FakeHttpResponse(200, [
            _non_text_delta("thinking_delta"),
            _non_text_delta("signature_delta"),
            _message_delta("max_tokens"),
        ])
        monkeypatch.setattr(transport, "_open", lambda req: response)
        with pytest.raises(TransportError, match="思考"):
            list(transport.stream("問題", "系統提示"))

    def test_raises_generic_error_when_empty_without_max_tokens(self, transport, monkeypatch):
        """沒文字但也不是因為撞到 max_tokens,仍要報錯,不能悄悄回傳空字串。"""
        response = FakeHttpResponse(200, [_message_delta("end_turn")])
        monkeypatch.setattr(transport, "_open", lambda req: response)
        with pytest.raises(TransportError):
            list(transport.stream("問題", "系統提示"))

    def test_does_not_raise_when_text_is_produced(self, transport, monkeypatch):
        """正常情況(哪怕只有一個字)不該被這道新檢查誤傷。"""
        response = FakeHttpResponse(200, [
            _event(delta_text="好"),
            _message_delta("end_turn"),
        ])
        monkeypatch.setattr(transport, "_open", lambda req: response)
        assert list(transport.stream("問題", "系統提示")) == ["好"]

    def test_thinking_before_real_text_is_fine(self, transport, monkeypatch):
        """思考過程之後只要有輸出正式文字,就不算「沒有回覆內容」。"""
        response = FakeHttpResponse(200, [
            _non_text_delta("thinking_delta"),
            _event(delta_text="這是真正的回答"),
            _message_delta("end_turn"),
        ])
        monkeypatch.setattr(transport, "_open", lambda req: response)
        assert list(transport.stream("問題", "系統提示")) == ["這是真正的回答"]


class TestErrorMapping:
    """API 的錯誤要對應到與 CLI 傳輸層相同的例外類型,上層才能統一處理。"""

    @pytest.fixture
    def transport(self):
        return AnthropicApiTransport(api_key="sk-test-key")

    def test_authentication_error_maps_to_not_logged_in(self, transport, monkeypatch):
        import urllib.error
        def raise_401(req):
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", {},
                fp=None,
            )
        monkeypatch.setattr(transport, "_open", raise_401)
        with pytest.raises(NotLoggedIn):
            list(transport.stream("問題", "系統提示"))

    def test_rate_limit_maps_to_quota_exceeded(self, transport, monkeypatch):
        import io
        import urllib.error
        def raise_429(req):
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests", {},
                fp=io.BytesIO(b'{"error":{"message":"rate limited"}}'),
            )
        monkeypatch.setattr(transport, "_open", raise_429)
        with pytest.raises(QuotaExceeded):
            list(transport.stream("問題", "系統提示"))

    def test_other_http_error_is_generic_transport_error(self, transport, monkeypatch):
        import io
        import urllib.error
        def raise_500(req):
            raise urllib.error.HTTPError(
                req.full_url, 500, "Server Error", {},
                fp=io.BytesIO(b'{"error":{"message":"boom"}}'),
            )
        monkeypatch.setattr(transport, "_open", raise_500)
        with pytest.raises(TransportError):
            list(transport.stream("問題", "系統提示"))


class TestMissingKey:
    """必須不依賴機器上是否存在 .env —— 明確清掉環境變數,測試才可重現。

    開發機上真的有 .env 時,若不隔離,「沒有金鑰」這個情境會被
    意外蓋成「用環境變數的金鑰」,兩個完全不同的案例混在一起。
    """

    def test_empty_key_raises_on_construction(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValueError):
            AnthropicApiTransport(api_key="")

    def test_none_key_raises_on_construction(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValueError):
            AnthropicApiTransport(api_key=None)


class TestAvailability:
    def test_is_available_when_key_present(self):
        assert AnthropicApiTransport(api_key="sk-test").is_available() is True

    def test_is_logged_in_means_key_present(self):
        """這條路徑沒有登入概念,is_logged_in 等同於「有金鑰」。"""
        assert AnthropicApiTransport(api_key="sk-test").is_logged_in() is True
