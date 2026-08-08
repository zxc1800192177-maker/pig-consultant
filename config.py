"""執行參數。

憲法第九條要求用量保護的參數寫在設定,不埋在邏輯深處。
"""

import os
import pathlib


def _load_dotenv():
    """讀取本機 .env(若存在)。部署平台用自己的環境變數介面,不需要這個檔案。

    刻意只設定「尚未存在」的環境變數,平台/系統既有設定優先。
    """
    env_path = pathlib.Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

# --- 用量保護(憲法第九條)---
MAX_QUESTION_CHARS = 2000        # 單題字數上限
MIN_REQUEST_INTERVAL_SEC = 3     # 同一來源連續請求的最短間隔
AI_TIMEOUT_SEC = 180

# 對外上線走 API 計費,失控會直接扣款,不像訂閱額度頂多是用完。
# 這是製程內的安全氣囊,不是計費上限本身 —— 真正的花費上限要在
# console.anthropic.com 另外設定。
MAX_AI_REQUESTS_PER_DAY = int(os.environ.get("MAX_AI_REQUESTS_PER_DAY", "500"))

# --- AI 呼叫 ---
MODEL = "sonnet"                          # CLI 傳輸層用的別名
API_MODEL = "claude-sonnet-5"              # Anthropic API 傳輸層用的完整型號名稱
API_MAX_TOKENS = 1500

# 網頁表單的內容會送進 CLI。不關閉工具等於讓任何能開啟網頁的人
# 在這台電腦上執行指令(憲法第四條)。這份清單是安全邊界,有測試把關。
DENY_TOOLS = [
    "Bash", "PowerShell", "Read", "Write", "Edit", "NotebookEdit", "Glob",
    "Grep", "WebFetch", "WebSearch", "Task", "Agent", "TodoWrite",
    "BashOutput", "KillShell", "SlashCommand", "Skill", "Artifact",
    "ToolSearch", "ScheduleWakeup", "SendUserFile", "AskUserQuestion",
    "ReportFindings", "Monitor", "CronCreate", "CronList", "CronDelete",
    "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TaskOutput",
    "TaskStop", "SendMessage", "PushNotification", "RemoteTrigger",
    "EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree",
    "DesignSync", "mcp__*",
]

# --- 伺服器 ---
# 本機/demo 預設只聽本機。部署平台(Render/Railway)會自動設定 HOST=0.0.0.0,
# 本機開發者不需要手動改這裡。
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", 8000))
