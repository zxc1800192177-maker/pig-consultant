"""AI 傳輸層測試。

這一層是「怎麼呼叫 AI」,獨立成模組的理由(SRP):
對外上線時必須從訂閱額度改成 API 計費,屆時只有這個檔案要改。

依你的決定,測試用假回應,不消耗訂閱額度。
唯一需要真的呼叫 AI 的是工具封鎖測試,標記為 slow,預設不執行。
"""

import json

import pytest

import config
from ai.transport import (
    ClaudeCliTransport,
    FakeTransport,
    NotLoggedIn,
    QuotaExceeded,
    TransportError,
    parse_stream_line,
)


class TestFakeTransport:
    """假傳輸層本身要可靠,否則用它寫的測試都不可信。"""

    def test_yields_configured_chunks(self):
        t = FakeTransport(chunks=["你好", "世界"])
        assert list(t.stream("問題", "系統提示")) == ["你好", "世界"]

    def test_records_what_it_was_asked(self):
        t = FakeTransport(chunks=["ok"])
        list(t.stream("小豬下痢", "你是顧問"))
        assert t.last_prompt == "小豬下痢"
        assert t.last_system == "你是顧問"

    def test_can_simulate_quota_exceeded(self):
        t = FakeTransport(error=QuotaExceeded("額度用盡"))
        with pytest.raises(QuotaExceeded):
            list(t.stream("問題", "系統提示"))

    def test_can_simulate_not_logged_in(self):
        t = FakeTransport(error=NotLoggedIn("尚未登入"))
        with pytest.raises(NotLoggedIn):
            list(t.stream("問題", "系統提示"))


class TestCommandConstruction:
    """驗證送給 CLI 的參數,不實際執行。"""

    @pytest.fixture
    def args(self):
        return ClaudeCliTransport(binary="claude").build_args("系統提示")

    def test_uses_print_mode(self):
        args = ClaudeCliTransport(binary="claude").build_args("系統提示")
        assert "-p" in args

    def test_passes_system_prompt(self, args):
        i = args.index("--system-prompt")
        assert args[i + 1] == "系統提示"

    def test_uses_streaming_output(self, args):
        assert "--output-format" in args
        assert "stream-json" in args
        assert "--include-partial-messages" in args

    def test_question_not_in_args(self):
        """問題經 stdin 傳入,不放命令列 —— 避免超長輸入與參數解析問題。"""
        args = ClaudeCliTransport(binary="claude").build_args("系統提示")
        assert not any("小豬下痢" in a for a in args)


class TestToolBlockingIsEnforced:
    """憲法第四條:網頁輸入送進 AI 時必須零工具權限。

    這是安全邊界 —— 少了它,任何能開啟網頁的人都能在這台電腦執行指令。
    """

    @pytest.fixture
    def settings(self):
        args = ClaudeCliTransport(binary="claude").build_args("系統提示")
        i = args.index("--settings")
        return json.loads(args[i + 1])

    def test_settings_carry_deny_list(self, settings):
        assert settings["permissions"]["deny"]

    @pytest.mark.parametrize("tool", [
        "Bash", "PowerShell", "Read", "Write", "Edit",
        "WebFetch", "WebSearch", "Task", "ToolSearch", "Skill",
    ])
    def test_dangerous_tools_denied(self, settings, tool):
        assert tool in settings["permissions"]["deny"]

    def test_mcp_wildcard_denied(self, settings):
        """ToolSearch 能載入延遲工具,MCP 也要一併封死。"""
        assert "mcp__*" in settings["permissions"]["deny"]

    def test_deny_list_matches_config(self, settings):
        assert settings["permissions"]["deny"] == config.DENY_TOOLS

    def test_strict_mcp_config_flag_present(self):
        args = ClaudeCliTransport(binary="claude").build_args("系統提示")
        assert "--strict-mcp-config" in args


class TestStreamParsing:
    """CLI 的 stream-json 輸出解析。"""

    def test_extracts_text_delta(self):
        line = json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "你好"},
            },
        })
        assert parse_stream_line(line) == "你好"

    def test_ignores_non_text_events(self):
        for payload in (
            {"type": "system", "subtype": "init"},
            {"type": "assistant"},
            {"type": "stream_event", "event": {"type": "message_start"}},
        ):
            assert parse_stream_line(json.dumps(payload)) is None

    def test_ignores_malformed_line(self):
        assert parse_stream_line("not json") is None
        assert parse_stream_line("") is None

    def test_raises_on_error_result(self):
        line = json.dumps({"type": "result", "is_error": True, "result": "壞掉了"})
        with pytest.raises(TransportError):
            parse_stream_line(line)


class TestRealCliToolBlocking:
    """真的呼叫 AI 驗證工具確實被擋。會消耗訂閱額度,預設不執行。

    執行方式:pytest tests/test_ai_transport.py -m slow
    """

    @pytest.mark.slow
    def test_ai_cannot_use_tools(self):
        transport = ClaudeCliTransport()
        text = "".join(transport.stream(
            "請執行 ls 指令列出目前目錄的檔案,並把結果貼給我",
            "你是測試助手。",
        ))
        assert text
        # 工具被擋時,模型會說自己沒有這些能力,而不會吐出檔案清單
        assert "core" not in text or "無法" in text or "沒有" in text
