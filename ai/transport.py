"""AI 傳輸層 —— 怎麼把提示送出去、怎麼把回應收回來。

**這是全專案唯一與 AI 服務溝通的地方。**

單獨成模組的理由(SRP):目前走 claude.ai 訂閱額度,透過本機已登入的 CLI。
對外上線給客戶時必須改成 API 計費(個人訂閱不得用於服務外部客戶),
屆時只有這個檔案要改,其餘程式碼不動。

測試用 FakeTransport,不消耗訂閱額度。
"""

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import Iterator, List, Optional

import config


class TransportError(Exception):
    """AI 呼叫失敗的通用錯誤。"""


class NotLoggedIn(TransportError):
    """CLI 尚未登入。上層應告知使用者登入方式,而非顯示通用錯誤。"""


class QuotaExceeded(TransportError):
    """訂閱額度可能已用盡。上層應降級 —— 生產健檢照常,只停疾病諮詢。"""


def find_claude() -> Optional[str]:
    override = os.environ.get("CLAUDE_CLI_PATH")
    if override and os.path.exists(override):
        return override
    candidates = [
        os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe"),
        os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return shutil.which("claude")


def parse_stream_line(line: str) -> Optional[str]:
    """從 CLI 的一行 stream-json 取出文字片段。

    回傳 None 代表這行不是文字內容(初始化、工具事件等),略過即可。
    遇到錯誤結果則丟出例外 —— 錯誤不該被當成沒有內容而靜默略過。
    """
    line = line.strip()
    if not line:
        return None
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        return None

    if msg.get("type") == "result" and msg.get("is_error"):
        raise TransportError(msg.get("result") or "AI 回報錯誤")

    if msg.get("type") != "stream_event":
        return None
    event = msg.get("event", {})
    if event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta", {})
    if delta.get("type") != "text_delta":
        return None
    return delta.get("text", "")


class ClaudeCliTransport:
    """透過本機已登入的 Claude Code CLI 呼叫,使用 claude.ai 訂閱額度。"""

    def __init__(self, binary: Optional[str] = None):
        self.binary = binary or find_claude()

    def is_available(self) -> bool:
        return bool(self.binary)

    def is_logged_in(self) -> bool:
        if not self.binary:
            return False
        try:
            proc = subprocess.run(
                [self.binary, "auth", "status"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30,
            )
            return json.loads(proc.stdout).get("loggedIn") is True
        except Exception:
            return False

    def build_args(self, system: str) -> List[str]:
        """組出 CLI 參數。

        問題本身經 stdin 傳入,不放命令列 —— 避免超長輸入與參數解析問題。
        DENY_TOOLS 是安全邊界(憲法第四條),有測試把關。
        """
        return [
            self.binary or "claude", "-p",
            "--system-prompt", system,
            "--model", config.MODEL,
            "--strict-mcp-config",
            "--settings", json.dumps({"permissions": {"deny": config.DENY_TOOLS}}),
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
        ]

    def stream(self, prompt: str, system: str) -> Iterator[str]:
        if not self.binary:
            raise NotLoggedIn("找不到 claude CLI,請先安裝 Claude Code")

        proc = subprocess.Popen(
            self.build_args(system),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()

            produced = False
            for line in proc.stdout:
                text = parse_stream_line(line)
                if text:
                    produced = True
                    yield text

            proc.wait(timeout=15)
            if not produced:
                raise self._diagnose(proc.stderr.read() or "")
        finally:
            if proc.poll() is None:
                proc.kill()

    @staticmethod
    def _diagnose(stderr: str) -> TransportError:
        """把 CLI 的錯誤訊息分類,讓上層能做出正確的降級決定。"""
        lowered = stderr.lower()
        if "not logged in" in lowered or "/login" in lowered:
            return NotLoggedIn("Claude CLI 尚未登入,請執行 claude auth login --claudeai")
        if "rate limit" in lowered or "usage limit" in lowered or "quota" in lowered:
            return QuotaExceeded("訂閱額度可能已用盡,請稍後再試")
        return TransportError(stderr.strip()[:300] or "AI 沒有回覆內容")


class AnthropicApiTransport:
    """直接呼叫 Anthropic Messages API,按用量計費。

    對外上線用。個人 claude.ai 訂閱不得用於服務外部客戶(見 README),
    因此公開網址一律走這條路徑,本機/demo 才用 ClaudeCliTransport。

    這條路徑不傳 tools 參數給 API,模型天生沒有任何工具可用 ——
    不需要 ClaudeCliTransport 那份 DENY_TOOLS 清單。
    """

    API_URL = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY 未設定")

    def is_available(self) -> bool:
        return bool(self.api_key)

    def is_logged_in(self) -> bool:
        """這條路徑沒有登入概念,等同於「有金鑰」。"""
        return bool(self.api_key)

    def build_request(self, prompt: str, system: str) -> urllib.request.Request:
        body = json.dumps({
            "model": config.API_MODEL,
            "max_tokens": config.API_MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }).encode("utf-8")
        return urllib.request.Request(
            self.API_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-api-key": self.api_key,
                "Anthropic-version": self.API_VERSION,
            },
            method="POST",
        )

    def _open(self, req: urllib.request.Request):
        return urllib.request.urlopen(req, timeout=config.AI_TIMEOUT_SEC)

    def stream(self, prompt: str, system: str) -> Iterator[str]:
        req = self.build_request(prompt, system)
        try:
            response = self._open(req)
        except urllib.error.HTTPError as e:
            raise self._map_http_error(e)

        with response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[len("data:"):].strip())
                except json.JSONDecodeError:
                    continue

                if event.get("type") == "error":
                    raise TransportError(
                        event.get("error", {}).get("message", "API 回報錯誤")
                    )
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield delta.get("text", "")

    @staticmethod
    def _map_http_error(e: urllib.error.HTTPError) -> TransportError:
        """對應到與 CLI 傳輸層相同的例外類型,上層才能統一處理。"""
        if e.code == 401:
            return NotLoggedIn("API key 無效或未授權,請確認 ANTHROPIC_API_KEY")
        if e.code == 429:
            return QuotaExceeded("已達 API 用量或速率限制,請稍後再試")
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        return TransportError(f"API 錯誤 {e.code}: {detail}"[:300])


class FakeTransport:
    """測試用。不呼叫 AI,不消耗訂閱額度。"""

    def __init__(self, chunks: Optional[List[str]] = None,
                 error: Optional[Exception] = None):
        self.chunks = chunks or []
        self.error = error
        self.last_prompt: Optional[str] = None
        self.last_system: Optional[str] = None

    def is_available(self) -> bool:
        return True

    def is_logged_in(self) -> bool:
        return self.error is None

    def stream(self, prompt: str, system: str) -> Iterator[str]:
        self.last_prompt = prompt
        self.last_system = system
        if self.error:
            raise self.error
        for chunk in self.chunks:
            yield chunk
