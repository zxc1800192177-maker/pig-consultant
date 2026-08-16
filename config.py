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
MAX_DRUG_INGREDIENT_CHARS = 100   # 有效成分,例如「Amoxicillin trihydrate 10%」

# --- 藥品標示拍照辨識 ---
#
# 圖片一律由前端縮圖後才上傳(見 web/lib/image.js),這裡的上限是伺服器端
# 的最後一道防線 —— 前端送什麼過來都不可信(憲法第四條)。
# Render 免費方案只有 512MB 記憶體,而請求體會先整包讀進記憶體才輪到限流。
MAX_IMAGE_BYTES = 1_500_000            # base64 解碼後的實際大小
MAX_IMAGE_REQUEST_BYTES = 2_500_000    # base64 膨脹約 33%,加上 JSON 外層
ALLOWED_IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp")

# 拍照辨識與提問分開計算額度。用途不同:建置藥品庫是一次性的
# (一口氣拍十張很正常),問診是持續性的。共用一個計數會讓牧場主
# 建完藥品庫就突然不能問問題,而且看不出來為什麼。
MAX_LABEL_SCANS_PER_HOUR = int(os.environ.get("MAX_LABEL_SCANS_PER_HOUR", "20"))

# 生產健檢的「其他參考因素」上限。跟藥品庫同樣的道理:來自瀏覽器,
# 只存在使用者這台裝置上,伺服器不保存,但送進請求時一樣不可信。
MAX_REFERENCE_FACTORS = 10
MAX_FACTOR_CHARS = 100

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

# 本機開發用:把資料放在記憶體裡,不必先架一個 Postgres。
#
# v1 沒有這個需求 —— 疾病諮詢與生產健檢不碰資料庫,沒有 DATABASE_URL 也能
# 把整個網站點完。v2 不一樣:工作清單、母豬卡、提醒全都從資料表來,沒有
# store 就只剩一片「載入中…」,本機根本驗不了畫面。
#
# 一定要顯式打開,而且**只在沒有 DATABASE_URL 時**才生效 —— 否則哪天有人
# 把這個變數留在正式環境,就會安靜地繞過真資料庫,每次重啟資料全部消失。
# InMemoryStore 與 PostgresStore 的方法完整性由測試強制(test_db_farms.py),
# 所以這裡不會出現「本機好好的、上線才炸」的落差。
def memory_db_enabled(flag: str, database_url: str) -> bool:
    """判斷邏輯抽成純函式,測試才驗得動。

    直接測模組常數的話得 reload config,而 reload 會重新讀 .env ——
    開發者自己的 .env 裡有什麼,測試結果就跟著變,而且 reload 後的模組
    是全域共用的,會一路影響到後面的測試。
    """
    return flag in ("1", "true", "True") and not database_url


DEV_MEMORY_DB = memory_db_enabled(os.environ.get("DEV_MEMORY_DB", ""), DATABASE_URL)

SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "30"))
SESSION_COOKIE_NAME = "pig_session"

# 是否要求先登入才能使用疾病諮詢與生產健檢。
#
# 只有在帳號功能真的可用時才會生效 —— 沒設 DATABASE_URL 的環境
# (本機開發、demo)不會因為這個設定就把所有人擋在門外,那會讓網站
# 在資料庫故障時完全不能用,而不是降級。
#
# 「訪客試用」仍然是入口之一,所以門檻是「點一下」而不是「先註冊」。
REQUIRE_LOGIN = os.environ.get("REQUIRE_LOGIN", "1") not in ("0", "false", "False")

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

# --- v2:母豬場管理 ---
#
# 牧場所在時區。**「今天」要用牧場當地的日期,不是 UTC。**
#
# 正式站跑在 UTC 的機器上,原本直接取 UTC 日期 —— 台灣時間半夜 12 點到
# 早上 8 點之間,系統會以為還是昨天,清晨看工作清單會看到上一週的工作。
# 豬場的班表本來就從清晨開始,這段時間正是會被用到的時候。
FARM_TIMEZONE = os.environ.get("FARM_TIMEZONE", "Asia/Taipei")

# 上限比照憲法第九條:用量保護的參數寫在設定,不埋在邏輯深處。
# 數字參考這個牧場的實際規模(451 頭在場、1,690 頭歷史)並留餘裕。
MAX_SOWS_PER_FARM = int(os.environ.get("MAX_SOWS_PER_FARM", "5000"))
MAX_PENS_PER_FARM = int(os.environ.get("MAX_PENS_PER_FARM", "500"))
MAX_EAR_TAG_CHARS = 30
MAX_BREED_CHARS = 30
MAX_PEN_NAME_CHARS = 30
MAX_TASK_NAME_CHARS = 40       # 自訂工作的名稱
MAX_CUSTOM_TASKS_PER_FARM = 100
MAX_EVENT_FIELDS = 12          # 單筆事件的 detail 最多幾個欄位

# 紀錄頁的「已記錄」清單。批次生產一週一批,整批同一天記完 —— 一天記
# 三四十筆是常態,所以上限不能只給十幾筆,否則剛記的那批自己會被截掉。
MAX_RECENT_EVENTS = 200
MAX_RECENT_EVENT_DAYS = 14     # 補登前幾天的記錄時要看得到那幾天

# 匯入的檔案上限。32,814 筆的實際檔案是 1.45 MB,10 MB 已很寬裕。
MAX_IMPORT_BYTES = int(os.environ.get("MAX_IMPORT_BYTES", str(10 * 1024 * 1024)))
