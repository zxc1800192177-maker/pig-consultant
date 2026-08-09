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

# 每個 IP 每小時的提問上限。滑動視窗,不是整點重置。
MAX_QUESTIONS_PER_HOUR = int(os.environ.get("MAX_QUESTIONS_PER_HOUR", "20"))
RATE_WINDOW_SEC = 3600

# 請求體大小上限。必須在讀取前就依 Content-Length 拒絕 ——
# 伺服器會先把整包讀進記憶體才輪到限流,所以「每小時 20 次」擋不住大包攻擊。
# 最大合法請求約 30KB(2000 字問題 + 20 則 × 500 字歷史),64KB 已很寬裕。
MAX_REQUEST_BYTES = int(os.environ.get("MAX_REQUEST_BYTES", str(64 * 1024)))

# 對話歷史上限。歷史由瀏覽器帶上來(伺服器不保存任何人的問題內容),
# 因此伺服器必須自己設限 —— 前端送什麼過來都不可信。
MAX_HISTORY_TURNS = 20
MAX_HISTORY_CHARS = 500          # 單則歷史保留的字數;超過即截斷

# 使用者自己的藥品庫上限。跟對話歷史一樣存在瀏覽器、跟著請求送上來,
# 一樣不可信,一樣要在伺服器端強制設限。
MAX_MY_DRUGS = 20
MAX_DRUG_NAME_CHARS = 60
MAX_DRUG_NOTE_CHARS = 200

# 要從 X-Forwarded-For 尾端砍掉幾層基礎設施位址。
#
# 正式環境實測的鏈:真實IP, Cloudflare, Render內部 —— 尾端兩層是平台的,
# 且 Render 那層每次請求都換一個位址。砍掉這 2 層才拿得到穩定的使用者身分。
# 本機開發沒有代理,這個值不影響(沒有 X-Forwarded-For 就直接用連線來源)。
#
# 換平台時要重新確認層數:設太少會抓到每次都變的內部位址(限流失效),
# 設太多會抓到使用者可偽造的前段(限流可被繞過)。
TRUSTED_PROXY_HOPS = int(os.environ.get("TRUSTED_PROXY_HOPS", "2"))

# 對外上線走 API 計費,失控會直接扣款,不像訂閱額度頂多是用完。
# 這是製程內的安全氣囊,不是計費上限本身 —— 真正的花費上限要在
# console.anthropic.com 另外設定。
MAX_AI_REQUESTS_PER_DAY = int(os.environ.get("MAX_AI_REQUESTS_PER_DAY", "500"))

# --- 帳號系統 ---
#
# 沒有 DATABASE_URL 就整個帳號功能關閉,網站退回「純工具、免帳號」模式。
# 這是刻意的:本機開發、demo、以及任何資料庫故障的情況下,疾病諮詢與
# 生產健檢都必須照常可用 —— 帳號是加值,不是使用門檻。
DATABASE_URL = os.environ.get("DATABASE_URL", "")

SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "30"))
SESSION_COOKIE_NAME = "pig_session"

MIN_PASSWORD_CHARS = 8
MIN_USERNAME_CHARS = 2
MAX_USERNAME_CHARS = 30

# 登入嘗試節流。密碼可以被暴力猜,提問不行 —— 所以這裡的窗口比
# MAX_QUESTIONS_PER_HOUR 嚴格得多,而且是以「失敗次數」計算,
# 成功登入不佔額度(自己打錯一次密碼不該讓正常使用者被鎖住)。
MAX_LOGIN_ATTEMPTS_PER_WINDOW = int(os.environ.get("MAX_LOGIN_ATTEMPTS_PER_WINDOW", "10"))
LOGIN_WINDOW_SEC = 900           # 15 分鐘

# 每個帳號可保存的健檢紀錄筆數上限。超過時最舊的會被刪掉 ——
# 這是免費方案的資料庫,不能讓單一使用者無限寫入把容量吃光。
MAX_HEALTH_CHECKS_PER_USER = int(os.environ.get("MAX_HEALTH_CHECKS_PER_USER", "100"))

# --- AI 呼叫 ---
MODEL = "sonnet"                          # CLI 傳輸層用的別名
API_MODEL = "claude-sonnet-5"              # Anthropic API 傳輸層用的完整型號名稱

# 實測發現:這個模型有時會自行進行大量內部思考(thinking token),
# 曾在 1500 上限下把整個額度耗在思考、完全沒剩空間輸出真正答案
# (stop_reason: max_tokens,thinking_tokens 就吃掉全部 1500)。
# 思考過程是否觸發不可預期,拉高上限確保思考+正式回答都有空間。
# 只影響「允許多長」,實際計費仍按模型真正產生的 token 數,不會白花錢。
API_MAX_TOKENS = 8000

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
