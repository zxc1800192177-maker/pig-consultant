"""傳輸層選擇邏輯測試。

憲法第五條:個人訂閱不得用於服務外部客戶。上線用 API 計費,
本機/demo 用訂閱額度。這個選擇不該散落在 server.py 裡用 if/else 猜測,
獨立成一個函式,行為才可測、可信。

規則:設了 ANTHROPIC_API_KEY 就走 API(代表這是正式上線環境);
沒設就走 CLI(代表這是本機/demo,用訂閱額度)。
"""

import pytest

from ai.transport import AnthropicApiTransport, ClaudeCliTransport
from ai.transport_selection import select_transport


class TestSelection:
    def test_api_key_present_selects_api_transport(self):
        transport = select_transport(env={"ANTHROPIC_API_KEY": "sk-test"})
        assert isinstance(transport, AnthropicApiTransport)

    def test_no_api_key_selects_cli_transport(self):
        transport = select_transport(env={})
        assert isinstance(transport, ClaudeCliTransport)

    def test_empty_api_key_selects_cli_transport(self):
        """空字串不算「有設定」,避免因為環境變數殘留空值而誤判成上線模式。"""
        transport = select_transport(env={"ANTHROPIC_API_KEY": ""})
        assert isinstance(transport, ClaudeCliTransport)

    def test_default_reads_from_os_environ(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert isinstance(select_transport(), AnthropicApiTransport)

    def test_api_transport_receives_the_key(self):
        transport = select_transport(env={"ANTHROPIC_API_KEY": "sk-specific"})
        assert transport.api_key == "sk-specific"
