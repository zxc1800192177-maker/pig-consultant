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

    def build_image_args(self, system: str) -> List[str]:
        """帶圖片時的 CLI 參數。

        差別只有 --input-format stream-json:預設的純文字 stdin 沒有地方
        可以放圖片,改用這個格式後 stdin 收的是一行 JSON,content 可以是
        image + text 的陣列(與 Anthropic Messages API 同一種形狀)。

        仍然是單次呼叫 —— 寫一行、關 stdin、讀完就結束,跟 build_args()
        那條路徑一樣,不需要維持一個長命的互動 session。

        DENY_TOOLS 照樣要帶。圖片一樣來自網頁表單,是不可信輸入,
        不會因為換了輸入格式就變安全(憲法第四條)。
        """
        return self.build_args(system) + ["--input-format", "stream-json"]

    @staticmethod
    def build_image_message(prompt: str, image: dict) -> str:
        """組出送進 stdin 的那一行 NDJSON。

        圖片是 base64,體積大,同樣經 stdin 而非命令列 —— 命令列長度
        在 Windows 上有硬性上限,一張照片必定超過。
        """
        return json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": image["media_type"],
                            "data": image["data"],
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            },
        }, ensure_ascii=False)

    def _run(self, args: List[str], stdin_text: str) -> Iterator[str]:
        """跑一次 CLI,把輸出的文字片段逐段吐出來。

        純文字與帶圖片兩條路徑差別只在 args 與 stdin 的內容,收尾方式
        (等待、判斷有沒有產出、殺掉沒結束的子行程)完全一樣,共用一份
        才不會日後只修好其中一條。
        """
        if not self.binary:
            raise NotLoggedIn("找不到 claude CLI,請先安裝 Claude Code")

        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        try:
            proc.stdin.write(stdin_text)
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

    def stream(self, prompt: str, system: str) -> Iterator[str]:
        return self._run(self.build_args(system), prompt)

    def stream_image(self, prompt: str, system: str, image: dict) -> Iterator[str]:
        """帶一張圖片呼叫。image 是 {"media_type": ..., "data": base64 字串}。

        stdin 要換行結尾:CLI 是逐行讀 NDJSON 的,沒有換行它會一直等
        下一個位元組,直到 stdin 關閉才處理 —— 加上去比較不會踩到
        緩衝相關的邊界情況。
        """
        return self._run(
            self.build_image_args(system),
            self.build_image_message(prompt, image) + "\n",
        )

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
        return self._build(system, prompt)

    def build_image_request(
        self, prompt: str, system: str, image: dict
    ) -> urllib.request.Request:
        """帶圖片的請求。content 從字串換成 image + text 的陣列。

        一樣不傳 tools —— 這條路徑天生沒有工具可用,不需要 CLI 那份
        DENY_TOOLS 清單(憲法第四條在這裡是靠「不給」而不是「禁止」達成)。
        """
        return self._build(system, [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image["media_type"],
                    "data": image["data"],
                },
            },
            {"type": "text", "text": prompt},
        ])

    def _build(self, system: str, content) -> urllib.request.Request:
        body = json.dumps({
            "model": config.API_MODEL,
            "max_tokens": config.API_MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": content}],
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
        """串流回應文字。

        這個模型有時會把大量 token 花在內部思考(thinking block)上,
        極端情況下可能耗盡 max_tokens 而完全沒有輸出正式回答文字
        (stop_reason: max_tokens,但一個字都沒吐)。若靜默結束,
        呼叫端會顯示空白內容,使用者以為是系統壞了卻看不到任何錯誤。
        因此在串流結束時明確檢查:完全沒有文字產出就視為錯誤,而非成功。
        """
        return self._consume(self.build_request(prompt, system))

    def stream_image(self, prompt: str, system: str, image: dict) -> Iterator[str]:
        """帶一張圖片呼叫。image 是 {"media_type": ..., "data": base64 字串}。"""
        return self._consume(self.build_image_request(prompt, system, image))

    def _consume(self, req: urllib.request.Request) -> Iterator[str]:
        try:
            response = self._open(req)
        except urllib.error.HTTPError as e:
            raise self._map_http_error(e)

        produced = False
        stop_reason = None
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
                if event.get("type") == "message_delta":
                    stop_reason = event.get("delta", {}).get("stop_reason")
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            produced = True
                        yield text

        if not produced:
            if stop_reason == "max_tokens":
                raise TransportError(
                    "顧問沒有回覆內容(模型把 token 額度耗在內部思考上,"
                    "尚未開始輸出正式回答)。請再試一次。"
                )
            raise TransportError("顧問沒有回覆內容,請再試一次。")

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
        self.last_image: Optional[dict] = None

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

    def stream_image(self, prompt: str, system: str, image: dict) -> Iterator[str]:
        # 記下圖片,測試才能斷言它真的被送出去了 —— 少了這條,
        # 「圖片其實沒送到模型」這種 bug 會一路通過所有測試。
        self.last_image = image
        return self.stream(prompt, system)
