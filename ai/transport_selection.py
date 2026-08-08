"""該用哪個傳輸層,由環境決定,不由程式碼裡的 if/else 猜測。

憲法第五條:個人 claude.ai 訂閱不得用於服務外部客戶。

規則:
  設了 ANTHROPIC_API_KEY  → 正式上線環境 → AnthropicApiTransport(按用量計費)
  沒設                     → 本機 / demo  → ClaudeCliTransport(訂閱額度)
"""

import os
from typing import Dict, Optional

from ai.transport import AnthropicApiTransport, ClaudeCliTransport


def select_transport(env: Optional[Dict[str, str]] = None):
    env = os.environ if env is None else env
    api_key = env.get("ANTHROPIC_API_KEY", "").strip()
    if api_key:
        return AnthropicApiTransport(api_key=api_key)
    return ClaudeCliTransport()
