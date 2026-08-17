"""HTTP 層 —— 只做路由、輸入檢查、限流。

商業邏輯都在 core/(計算)與 ai/(生成)。這個檔案不該出現任何
分級規則、領域判斷或提示詞 —— 那些改變時不該連帶動到伺服器。

Application 與 HTTP 傳輸分離,測試才能不開 socket 直接驗證行為。
"""

import http.server
import json
import math
import pathlib
import socketserver
import threading
import time
from datetime import date, datetime, timedelta, timezone
from http.cookies import SimpleCookie
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Tuple

import config
import importer
import schedule
from auth import Auth, AuthError, InvalidCredentials, NotGuest, UsernameTaken, ValidationError
from db import select_store
from ai.consultant import Consultant
from ai.transport import (
    AnthropicApiTransport,
    ClaudeCliTransport,
    NotLoggedIn,
    QuotaExceeded,
    TransportError,
)
from ai.transport_selection import select_transport
from core.benchmark import get_metric, gradable_metrics, metrics_index
from core.diagnosis import is_weak, rank_weaknesses
from core.grading import grade_all
from core.labels import (
    ai_unavailable_note,
    grade_label,
    medical_disclaimer,
    sample_size_note,
    shortfall_note,
    source_label,
    upstream_note,
)
from core import labels
from core.metrics import validate

def _today() -> date:
    """牧場當地的「今天」。**只有這裡取得當下日期** —— schedule.py 與
    importer.py 都不自己取,一律由呼叫端傳入,測試才能固定日期斷言。

    **用牧場時區,不是 UTC。** 正式站跑在 UTC 的機器上,取 UTC 日期的話,
    台灣時間半夜 12 點到早上 8 點之間系統會以為還是昨天 —— 清晨看工作
    清單會看到上一週的工作,而豬場的班表正好從清晨開始。

    時區設錯或缺 tzdata 時退回 UTC:寧可日期偏一點,也不要整個服務起不來。
    """
    try:
        return datetime.now(ZoneInfo(config.FARM_TIMEZONE)).date()
    except Exception:
        return datetime.now(timezone.utc).date()


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _date(text) -> Optional[date]:
    """YYYY-MM-DD → date。壞掉的格式回 None,不拋例外 ——
    前端送什麼過來都不可信(憲法第四條)。
    """
    if isinstance(text, date):
        return text
    if not isinstance(text, str):
        return None
    try:
        return date.fromisoformat(text.strip()[:10])
    except ValueError:
        return None


def _text(value, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _query(path: str, key: str) -> Optional[str]:
    if "?" not in path:
        return None
    from urllib.parse import parse_qs
    values = parse_qs(path.split("?", 1)[1]).get(key)
    return values[0] if values else None


def _route(path: str) -> str:
    return path.split("?", 1)[0]


BASE_DIR = pathlib.Path(__file__).parent
WEB_DIR = BASE_DIR / "web"
EXAMPLE_PATH = BASE_DIR / "data" / "example_farm.json"


def request_limit(path: str = "") -> int:
    """這條路徑的請求體上限。

    藥品標示照片必定超過純文字的上限(一張縮圖過的 JPEG 轉 base64
    就有數百 KB),所以單獨放寬。刻意做成「只有這個路徑例外」而不是
    把全站上限拉高 —— 其他端點沒有理由收得下一個 2MB 的請求。
    """
    if path in ("/api/import", "/api/import/preview"):
        return config.MAX_IMPORT_BYTES
    return config.MAX_REQUEST_BYTES


def too_large(content_length: int, path: str = "") -> bool:
    """請求體是否超過上限。

    必須在 read() 之前依 Content-Length 判斷 —— 伺服器原本會先把整包
    讀進記憶體,之後才輪到限流,所以每小時 20 次的限制完全擋不住大包灌流
    (實測 19.7MB 照單全收)。免費方案只有 512MB 記憶體。
    """
    return content_length > request_limit(path)


def client_ip(headers, peer_address: str) -> str:
    """判定真實使用者 IP。

    部署在 Render 這類平台時,伺服器看到的連線來源永遠是代理的位址
    (實測為 127.0.0.1)。若直接用它當識別,所有使用者會共用同一份額度 ——
    一個人送出請求就擋住全世界。

    X-Forwarded-For 的格式是「客戶端, 代理1, 代理2」。正式環境實測的鏈:

        203.204.236.67 , 172.71.146.124 , 10.28.196.132
          真實使用者         Cloudflare       Render 內部

    尾端是平台自己的基礎設施,而且 **Render 那層每次請求都不同**
    (實測 10.25.32.132 / 10.28.196.132 / 10.28.128.130),
    直接取最後一段會讓同一個人每次都被當成新使用者,限流完全失效。

    但也不能取第一段 —— 那是使用者送什麼就是什麼,等於讓攻擊者自行指定身分。

    正確做法:從尾端往回跳過固定層數的基礎設施(TRUSTED_PROXY_HOPS),
    取剩下的最後一段。攻擊者在前面塞再多假資料,都會被推到更前面而取不到。
    """
    forwarded = ""
    if headers:
        for name in ("X-Forwarded-For", "x-forwarded-for"):
            value = headers.get(name)
            if value:
                forwarded = value
                break

    hops = [part.strip() for part in forwarded.split(",") if part.strip()]
    if not hops:
        return peer_address

    # 砍掉尾端的基礎設施層,取剩下的最後一段。
    # 鏈比預期短時保留至少一段,不回傳空值。
    remaining = hops[:-config.TRUSTED_PROXY_HOPS] if config.TRUSTED_PROXY_HOPS else hops
    return (remaining or hops)[-1]


# Application 回傳的 dict 裡,這兩個鍵是給 Handler 看的內部指令,
# 不是要回給瀏覽器的資料 —— Handler 會在序列化之前 pop 掉。
# 這樣做是為了讓 handle_get/handle_post 維持單純的 (status, dict) 形狀,
# 不必為了「順便設一個 cookie」把所有呼叫端與測試的簽章都改掉。
SET_SESSION_KEY = "_setSession"
CLEAR_SESSION_KEY = "_clearSession"


def to_json_bytes(payload: dict) -> bytes:
    """把回應序列化成要送出去的 bytes。

    date/datetime 一律轉成 ISO 字串。資料庫回來的日期欄位就是 date 物件,
    json 不認得它 —— 少了這一步,`/api/sows` 與 `/api/alerts` 會在序列化時
    拋 TypeError,連線直接斷掉,瀏覽器只看得到「Failed to fetch」。

    測試看不到這個 bug:它們呼叫 Application.handle_get() 拿 dict 就斷言,
    而序列化發生在 Handler 那一層。所以這裡獨立成一個函式,測試才有東西
    可以直接測(見 TestResponsesAreSerializable)。

    修在這個唯一出口而不是逐個端點補 —— 每加一個回傳日期的端點就再踩
    一次的話,遲早會漏。ISO 字串本來就是前端要的格式,schedule.py 早就
    自己這樣轉了。
    """
    def _default(value):
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        raise TypeError(f"無法序列化的型別:{type(value).__name__}")

    return json.dumps(payload, ensure_ascii=False, default=_default).encode("utf-8")


class Application:
    """路由與請求處理。不綁定 HTTP 傳輸,方便測試。"""

    def __init__(self, transport=None, store=None):
        self.transport = transport or ClaudeCliTransport()
        self.consultant = Consultant(self.transport)
        # store 為 None 代表沒設定資料庫 —— 帳號功能整個關閉,
        # 其餘功能(疾病諮詢、生產健檢)完全不受影響。
        self.store = store
        self.auth = Auth(store) if store else None
        self._last_ai_request: Dict[str, float] = {}
        self._hourly_hits: Dict[str, List[float]] = {}
        self._login_attempts: Dict[str, List[float]] = {}
        self._label_scans: Dict[str, List[float]] = {}
        self._ai_request_count = 0
        self._count_day = time.strftime("%Y-%m-%d")

        # 伺服器是多執行緒的,限流的「檢查」與「記錄」之間若可被插隊,
        # 攻擊者同時灌請求就能突破上限(實測:上限 5 次會放行 30 次)。
        # 這兩個動作必須是不可分割的整體。
        self._limit_lock = threading.Lock()

    # --- 輔助 ---

    def _throttled(self, client: str) -> Optional[float]:
        """回傳還需等待幾秒;None 表示可以放行。只套用在會花額度的端點。

        整段在鎖內完成 —— 檢查與記錄之間若可被其他請求插隊,同時送達的
        請求會各自讀到舊值而全部放行。
        """
        with self._limit_lock:
            now = time.monotonic()
            last = self._last_ai_request.get(client)
            if last is not None:
                elapsed = now - last
                if elapsed < config.MIN_REQUEST_INTERVAL_SEC:
                    return round(config.MIN_REQUEST_INTERVAL_SEC - elapsed, 1)
            self._last_ai_request[client] = now
            return None

    def _over_window_limit(
        self, bucket: Dict[str, List[float]], client: str, limit: int, window: int
    ) -> int:
        """滑動視窗限流。每一種額度各自帶一個 bucket 進來。

        回傳「還要等幾秒」,放行時是 0 —— 0 是假值,所以呼叫端照樣可以
        直接寫 `if self._over_login_limit(client):`。會回秒數而不是 True
        是為了讓畫面講得出「請等 X 分鐘」:只說「請稍後再試」的話,
        使用者只能每隔幾秒重按一次碰運氣(實際發生過)。

        只在實際放行時記錄 —— 被擋下的嘗試不計入,否則使用者越重試,
        額度恢復時間被推得越晚,等於因為被擋而受到額外懲罰。

        整段在鎖內完成:檢查與記錄之間若可被插隊,同時送達的請求會各自
        讀到舊值而全部放行(實測:上限 5 次會放行 30 次)。這也是為什麼
        三種額度共用這一份實作而不是各抄一遍 —— 併發正確性只想驗證一次。
        """
        with self._limit_lock:
            now = time.monotonic()
            cutoff = now - window

            # 順手清掉已經完全過期的來源,避免這份紀錄無限成長成攻擊面
            for ip in [ip for ip, hits in bucket.items()
                       if not hits or hits[-1] <= cutoff]:
                del bucket[ip]

            hits = [t for t in bucket.get(client, []) if t > cutoff]
            if len(hits) >= limit:
                bucket[client] = hits
                # 最舊的那一筆滿 window 秒就會讓出一個名額,不必等整個窗口。
                return max(1, math.ceil(hits[0] + window - now))

            hits.append(now)
            bucket[client] = hits
            return 0

    def _over_hourly_limit(self, client: str) -> int:
        """每個 IP 每小時的提問上限。"""
        return self._over_window_limit(
            self._hourly_hits, client,
            config.MAX_QUESTIONS_PER_HOUR, config.RATE_WINDOW_SEC,
        )

    def _over_login_limit(self, client: str) -> int:
        """登入/註冊/訪客建立的嘗試次數上限。

        跟提問限流分開計算,原因是威脅不同:提問是花錢,登入是被猜密碼,
        後者需要嚴格得多的窗口。訪客建立也算在這裡 —— 那會在資料庫寫入
        一列,不設限等於開放任何人把免費方案的容量灌爆。
        """
        return self._over_window_limit(
            self._login_attempts, client,
            config.MAX_LOGIN_ATTEMPTS_PER_WINDOW, config.LOGIN_WINDOW_SEC,
        )

    def _over_scan_limit(self, client: str) -> int:
        """拍照辨識的每小時上限。

        跟提問分開計算:建置藥品庫是一次性的(一口氣拍十張很正常),
        問診是持續性的。共用一個計數會讓牧場主建完藥品庫就突然不能
        問問題,而且畫面上看不出來為什麼。
        """
        return self._over_window_limit(
            self._label_scans, client,
            config.MAX_LABEL_SCANS_PER_HOUR, config.RATE_WINDOW_SEC,
        )

    def _current_user(self, token):
        """目前登入的使用者;未登入或帳號功能未啟用時回 None。"""
        return self.auth.resolve_session(token) if self.auth else None

    def _login_required(self) -> bool:
        """兩項核心功能是否需要先登入。

        帳號功能不可用時一律回 False —— 沒設資料庫的環境(本機開發、
        demo),或資料庫故障時,網站要降級成免帳號可用,而不是整個鎖死。
        """
        return bool(config.REQUIRE_LOGIN and self.auth)

    def _gate(self, token):
        """未登入時回傳給前端的錯誤內容;可以放行則回 None。

        前端會自己擋一次(未登入就不顯示功能畫面),但那只是介面。
        真正的限制必須在這裡 —— 不然任何人直接呼叫 API 就繞過去了,
        而疾病諮詢每一次呼叫都在花錢。
        """
        if not self._login_required():
            return None
        if self._current_user(token) is not None:
            return None
        return {"reason": "login_required", "error": "請先登入或使用訪客試用"}

    def _farm_of(self, token):
        """(farm_id, user) —— 兩者都拿不到時回 (None, None)。

        v2 的每一個資料端點都從這裡取 farm_id,不從請求內容拿。
        讓前端傳 farm_id 等於讓任何人換一個號碼就看到別的牧場
        (憲法第十一條)。
        """
        user = self._current_user(token)
        if user is None or user.farm_id is None:
            return None, user
        return user.farm_id, user

    def _need_farm(self, token):
        """回 (farm_id, user, 錯誤)。錯誤不是 None 就直接回給前端。"""
        farm_id, user = self._farm_of(token)
        if user is None:
            return None, None, (401, {"error": "請先登入"})
        if farm_id is None:
            return None, user, (409, {"error": "這個帳號還沒有對應的牧場"})
        return farm_id, user, None

    @staticmethod
    def _need_owner(user):
        """只有牧場主能做的事:月報、值得檢視、設定、匯入、觸發 AI 建議。

        員工看得到工作清單與母豬卡(那是他做事需要的),但花錢的動作與
        經營層面的資訊由牧場主控制(憲法第十一條第 5 款、使用者決定)。
        """
        if user is not None and not user.is_owner:
            return 403, {"reason": "owner_only", "error": "這項功能只有牧場主可以使用"}
        return None

    @staticmethod
    def _user_payload(user) -> dict:
        if user is None:
            return {"loggedIn": False}
        return {
            "loggedIn": True,
            "role": user.role,
            "isOwner": user.is_owner,
            "username": user.username,
            "isGuest": user.is_guest,
        }

    @staticmethod
    def _auth_error_status(error: AuthError) -> int:
        """例外型別決定狀態碼。訊息一律用例外自己帶的文字,不在這裡
        改寫 —— 稍早的教訓:改寫過的訊息會蓋掉真正的原因,把除錯帶偏。
        """
        if isinstance(error, UsernameTaken):
            return 409
        if isinstance(error, ValidationError):
            return 400
        if isinstance(error, NotGuest):
            return 409
        return 401     # InvalidCredentials 與其他

    def _over_daily_budget(self) -> bool:
        """對外上線走 API 計費,失控會直接扣款,不像訂閱額度頂多是用完。

        全站共用一個計數,不分客戶端 —— 這是保護帳單,不是保護單一使用者。
        以行程記憶體計數,重啟即重置;真正的花費上限要在
        console.anthropic.com 另外設定,這裡只是製程內的安全氣囊。
        """
        with self._limit_lock:
            today = time.strftime("%Y-%m-%d")
            if today != self._count_day:
                self._count_day = today
                self._ai_request_count = 0
            self._ai_request_count += 1
            return self._ai_request_count > config.MAX_AI_REQUESTS_PER_DAY

    @staticmethod
    def _weakness_payload(weakness) -> dict:
        return {
            "key": weakness.key,
            "name": weakness.name,
            "grade": weakness.grade,
            "gradeLabel": grade_label(weakness.grade),
            "shortfallSd": weakness.shortfall_sd,
            "unit": weakness.unit,
            "improvement": weakness.improvement,
            "downstream": weakness.downstream,
            "downstreamNames": [get_metric(k)["name"] for k in weakness.downstream],
        }

    # --- GET ---

    def handle_get(self, path: str, token: Optional[str] = None) -> Tuple[int, dict]:
        if path == "/api/health":
            return 200, {
                "aiAvailable": self.transport.is_logged_in(),
                # 健檢是純計算,不依賴 AI 或網路,永遠可用(規格 6.5)
                "gradingAvailable": True,
                "source": source_label(),
                # 文字由後端提供,前端不自己寫一份(措辭改動只需改一處)
                "aiUnavailableNote": ai_unavailable_note(),
                # 前端據此決定要不要顯示登入相關的介面。沒有資料庫時
                # 整個帳號區塊不出現,而不是出現了按下去才報錯。
                "accountsAvailable": self.auth is not None,
                # 前端據此決定未登入時要不要把功能畫面藏起來。
                # 真正的限制在後端(見 _gate),這個欄位只是為了讓畫面
                # 一開始就顯示登入引導,而不是讓人填完表單才被拒絕。
                "loginRequired": self._login_required(),
            }
        if path == "/api/auth/me":
            return 200, self._user_payload(self._current_user(token))
        if path == "/api/health-checks":
            return self._list_health_checks(token)

        # ── v2 ──
        route = _route(path)
        if route == "/api/sows":
            return self._list_sows(token, path)
        if route.startswith("/api/sows/"):
            sow_id = self._path_id(route)
            if sow_id is None:
                return 400, {"error": "編號格式錯誤"}
            return self._sow_detail(token, sow_id)
        if route == "/api/tasks":
            return self._tasks(token, path)
        if route == "/api/alerts":
            return self._alerts(token)
        if route == "/api/pens":
            return self._pens(token, path)
        if route == "/api/review":
            return self._review(token)
        if route == "/api/monthly-report":
            return self._monthly_report(token, path)
        if route == "/api/settings":
            return self._get_settings(token)
        if route == "/api/boars":
            return self._list_boars(token, path)
        if route.startswith("/api/boars/"):
            boar_id = self._path_id(route)
            if boar_id is None:
                return 400, {"error": "編號格式錯誤"}
            return self._boar_detail(token, boar_id)
        if route == "/api/custom-tasks":
            return self._list_custom_tasks(token)
        if route == "/api/recent-events":
            return self._recent_events(token, path)
        if path == "/api/metrics":
            return 200, {
                "metrics": [
                    {
                        "key": m["key"],
                        "name": m["name"],
                        "unit": m.get("unit", ""),
                        "definition": m["definition"],
                        "range": m.get("range"),
                    }
                    for m in gradable_metrics()
                ],
                        "source": source_label(),
            }
        if path == "/api/example":
            with open(EXAMPLE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            return 200, {"label": data["label"], "values": data["values"]}
        return 404, {"error": "not found"}

    # --- POST ---

    def handle_post(
        self, path: str, raw: bytes, client: str, token: Optional[str] = None
    ) -> Tuple[int, dict]:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return 400, {"error": "請求格式錯誤,需為 UTF-8 編碼的 JSON"}

        if path == "/api/grade":
            gate = self._gate(token)
            return (401, gate) if gate else self._grade(payload)
        if path == "/api/advise":
            gate = self._gate(token)
            return (401, gate) if gate else self._advise(payload, client)
        if path == "/api/advise-chat":
            return self._advise_chat(payload, client, token)
        if path.startswith("/api/auth/"):
            return self._auth_post(path, payload, client, token)
        if path == "/api/health-checks":
            return self._save_health_check(payload, token)

        # ── v2 ──
        if path == "/api/sows":
            return self._add_sow(payload, token)
        if path == "/api/sow-events":
            return self._add_event(payload, token)
        if path == "/api/pens":
            return self._add_pen(payload, token)
        if path == "/api/boars":
            return self._add_boar(payload, token)
        if path == "/api/boar-events":
            return self._add_boar_event(payload, token)
        if path == "/api/custom-tasks":
            return self._add_custom_task(payload, token)
        if path == "/api/custom-tasks/done":
            return self._toggle_custom_task(payload, token)
        if path == "/api/settings":
            return self._put_settings(payload, token)
        if path == "/api/import/preview":
            return self._import_preview(payload, token)
        if path == "/api/import":
            return self._import_commit(payload, token)
        return 404, {"error": "not found"}

    def handle_delete(self, path: str, token: Optional[str] = None) -> Tuple[int, dict]:
        user = self._current_user(token)
        if user is None:
            return 401, {"error": "請先登入"}

        # 刪除一律連 user_id 一起帶進查詢(見 db.py 的約定)——
        # 只用 id 的話,換個號碼就能刪別人的資料。
        if path.startswith("/api/health-checks/"):
            check_id = self._path_id(path)
            if check_id is None:
                return 400, {"error": "編號格式錯誤"}
            ok = self.store.delete_health_check(user.id, check_id)
            return (200, {"ok": True}) if ok else (404, {"error": "找不到這筆資料"})

        # ── v2 ──
        if path.startswith("/api/sow-events/"):
            event_id = self._path_id(path)
            if event_id is None:
                return 400, {"error": "編號格式錯誤"}
            return self._delete_event(token, event_id)

        if path.startswith("/api/boar-events/"):
            event_id = self._path_id(path)
            if event_id is None:
                return 400, {"error": "編號格式錯誤"}
            return self._delete_boar_event(token, event_id)

        if path.startswith("/api/custom-tasks/"):
            task_id = self._path_id(path)
            if task_id is None:
                return 400, {"error": "編號格式錯誤"}
            return self._delete_custom_task(token, task_id)

        if path.startswith("/api/pens/"):
            pen_id = self._path_id(path)
            if pen_id is None:
                return 400, {"error": "編號格式錯誤"}
            return self._delete_pen(token, pen_id)

        return 404, {"error": "not found"}

    @staticmethod
    def _path_id(path: str) -> Optional[int]:
        try:
            return int(path.rstrip("/").rsplit("/", 1)[-1])
        except ValueError:
            return None

    # --- 帳號 ---

    def _auth_post(self, path, payload, client, token) -> Tuple[int, dict]:
        if self.auth is None:
            return 503, {"error": "本站目前未啟用帳號功能"}

        if path == "/api/auth/logout":
            self.auth.logout(token)
            return 200, {"loggedIn": False, CLEAR_SESSION_KEY: True}

        if path == "/api/auth/delete":
            # 密碼錯誤走的是限流(可以被拿來猜密碼),所以先過節流再驗。
            wait = self._over_login_limit(client)
            if wait:
                minutes = math.ceil(wait / 60)
                return 429, {"error": f"嘗試次數過多,請等 {minutes} 分鐘後再試"
                                      "(重複嘗試不會讓等待時間變長)"}
            try:
                self.auth.delete_account(token, payload.get("password"))
            except AuthError as e:
                return self._auth_error_status(e), {"error": str(e)}
            # 帳號已經不存在,cookie 一定要跟著清掉,否則瀏覽器會一直
            # 帶著一張指向空號的 session。
            return 200, {"loggedIn": False, CLEAR_SESSION_KEY: True}

        # 註冊/登入/訪客建立都會消耗資源(雜湊運算或資料庫寫入),
        # 而且都是可以被自動化重複嘗試的入口,一律先過節流。
        # 講得出還要等多久。只說「請稍後再試」的話,使用者只能每隔幾秒
        # 重按一次碰運氣,而重按並不會讓額度更快恢復。
        wait = self._over_login_limit(client)
        if wait:
            minutes = math.ceil(wait / 60)
            return 429, {"error": f"嘗試次數過多,請等 {minutes} 分鐘後再試"
                                  "(重複嘗試不會讓等待時間變長)"}

        # 救援碼流程。兩支都放在節流之後 —— 救援碼是可以被暴力猜的東西。
        if path == "/api/auth/recover":
            try:
                fresh = self.auth.reset_with_recovery_code(
                    payload.get("username"), payload.get("code"),
                    payload.get("password"))
            except AuthError as e:
                return self._auth_error_status(e), {"error": str(e)}
            # 刻意不順便登入(OWASP):要求重新登入一次,才能確認新密碼
            # 真的被記住了,而不是等下次要用時才發現又進不去。
            return 200, {"recoveryCode": fresh}

        if path == "/api/auth/recovery-code":
            try:
                fresh = self.auth.regenerate_recovery_code(token, payload.get("password"))
            except AuthError as e:
                return self._auth_error_status(e), {"error": str(e)}
            return 200, {"recoveryCode": fresh}

        try:
            if path == "/api/auth/register":
                result = self.auth.register(payload.get("username"), payload.get("password"))
            elif path == "/api/auth/login":
                result = self.auth.login(payload.get("username"), payload.get("password"))
            elif path == "/api/auth/guest":
                result = self.auth.guest_login()
            elif path == "/api/auth/claim":
                result = self.auth.claim(
                    token, payload.get("username"), payload.get("password")
                )
            else:
                return 404, {"error": "not found"}
        except AuthError as e:
            return self._auth_error_status(e), {"error": str(e)}

        body = {
            **self._user_payload(result.user),
            SET_SESSION_KEY: result.token,
        }
        # 註冊才會帶救援碼。明碼只有這一刻存在,前端必須當場顯示給
        # 使用者抄下來 —— 之後連我們自己都拿不回來(資料庫裡只剩雜湊)。
        if result.recovery_code:
            body["recoveryCode"] = result.recovery_code
        return 200, body

    # --- 健檢紀錄 ---

    def _list_health_checks(self, token) -> Tuple[int, dict]:
        user = self._current_user(token)
        if user is None:
            return 401, {"error": "請先登入"}

        records = []
        for row in self.store.list_health_checks(user.id):
            # 級距是即時算的,不是存起來的 —— core/grading.py 是唯一
            # 算級距的地方(單一事實來源)。代價是常模改版後舊紀錄顯示的
            # 級距會跟著變,那是刻意的取捨:寧可跟現行標準一致,
            # 也不要留下一份無法追溯是用哪版規則算出來的數字。
            report = validate(row["values"])
            graded = grade_all(report.cleaned, metrics_index()) if report.ok else {}
            records.append({
                "id": row["id"],
                "createdAt": row["created_at"].isoformat(),
                "values": row["values"],
                "grades": {k: g.grade for k, g in graded.items()},
                "weakCount": sum(1 for k, g in graded.items() if is_weak(k, g)),
            })
        return 200, {"records": records}

    def _save_health_check(self, payload, token) -> Tuple[int, dict]:
        user = self._current_user(token)
        if user is None:
            return 401, {"error": "請先登入"}

        # 存進去之前先驗證。壞掉的資料存進資料庫後,每次讀取歷史紀錄
        # 都會再壞一次,而且使用者不會知道是哪一筆有問題。
        report = validate(payload.get("values") or {})
        if not report.ok:
            return 400, {
                "errors": [{"key": e.key, "message": e.message} for e in report.errors],
            }
        if not report.cleaned:
            return 400, {"error": "請至少填入一項指標"}

        check_id = self.store.add_health_check(user.id, report.cleaned)
        return 200, {"id": check_id}

    # --- 藥品庫 ---


    @staticmethod
    def _sow_payload(row) -> dict:
        return {
            "id": row["id"],
            "earTag": row["ear_tag"],
            "breed": row.get("breed") or "",
            "parity": row.get("parity") or 0,
            "status": row.get("status") or "active",
            "penId": row.get("pen_id"),
            "sireTag": row.get("sire_tag") or "",
            "damTag": row.get("dam_tag") or "",
            "entryDate": _iso(row.get("entry_date")),
            "birthDate": _iso(row.get("birth_date")),
        }

    @staticmethod
    def _event_payload(row) -> dict:
        return {
            "id": row["id"],
            "sowId": row["sow_id"],
            "type": row["event_type"],
            "date": _iso(row["event_date"]),
            "detail": row.get("detail") or {},
            "excluded": bool(row.get("excluded")),
            "recordedBy": row.get("recorded_by"),
        }

    def _list_sows(self, token, path) -> Tuple[int, dict]:
        farm_id, user, err = self._need_farm(token)
        if err:
            return err
        status = "active"
        if "?" in path and "all" in path.split("?", 1)[1]:
            status = None
        return 200, {"sows": [self._sow_payload(s)
                              for s in self.store.list_sows(farm_id, status)]}

    def _add_sow(self, payload, token) -> Tuple[int, dict]:
        farm_id, user, err = self._need_farm(token)
        if err:
            return err

        tag = payload.get("earTag")
        if not isinstance(tag, str) or not tag.strip():
            return 400, {"error": "請填寫耳號"}
        tag = tag.strip()[:config.MAX_EAR_TAG_CHARS]

        if len(self.store.list_sows(farm_id)) >= config.MAX_SOWS_PER_FARM:
            return 400, {"error": f"母豬數量已達上限 {config.MAX_SOWS_PER_FARM} 頭"}
        if self.store.find_sow_by_tag(farm_id, tag):
            return 409, {"error": f"耳號 {tag} 已經在場,不能重複"}

        try:
            sow_id = self.store.add_sow(
                farm_id, tag,
                entry_date=_date(payload.get("entryDate")),
                birth_date=_date(payload.get("birthDate")),
                breed=_text(payload.get("breed"), config.MAX_BREED_CHARS),
                sire_tag=_text(payload.get("sireTag"), config.MAX_EAR_TAG_CHARS),
                dam_tag=_text(payload.get("damTag"), config.MAX_EAR_TAG_CHARS),
            )
        except ValueError as e:
            return 409, {"error": str(e)}
        return 200, {"id": sow_id}

    def _sow_detail(self, token, sow_id) -> Tuple[int, dict]:
        farm_id, user, err = self._need_farm(token)
        if err:
            return err
        sow = self.store.get_sow(farm_id, sow_id)
        if sow is None:
            return 404, {"error": "找不到這頭母豬"}
        events = self.store.list_sow_events(farm_id, sow_id)
        cfg = self._farm_settings(farm_id)

        # 場內比較要撈全場的事件。母豬卡的級距是「與同場其他母豬比」
        # (已確認的設計決定),只看這一頭是比不出來的。
        #
        # 比較基準含已離群(死亡/淘汰)的母豬 —— 只拿在場的當分母,
        # 表現最差、正是離群原因的那批一離群就從比較基準消失,活著的
        # 級距會愈算愈寬鬆(見 schedule.performance_with_tiers)。
        all_events = self.store.list_sow_events(farm_id)
        grouped = schedule._by_sow(all_events)
        sows = self.store.list_sows(farm_id, None)

        status = schedule.sow_status(sow, grouped.get(sow_id, []), _today(), cfg)
        performance = schedule.performance_with_tiers(sow_id, sows, grouped)

        pen = None
        if sow.get("pen_id"):
            pen = next((p for p in self.store.list_pens(farm_id)
                       if p["id"] == sow["pen_id"]), None)

        return 200, {
            "sow": self._sow_payload(sow),
            "status": {
                "state": status["state"],
                "label": labels.sow_state_label(status["state"]),
                "dayLabel": labels.sow_day_label(status["state"], status["day"]),
                "since": _iso(status.get("since")),
                "due": _iso(status.get("due")),
                "weanDue": _iso(status.get("wean_due")),
                "moveInDue": _iso(status.get("move_in_due")),
                "overdueLabel": labels.overdue_farrow_label(status["overdue_days"])
                                if status.get("overdue_days") else "",
                "pregCheckNote": labels.pending_check_note(
                    status["preg_checked"], status["day"],
                    cfg["preg_check_days"], status["preg_check_overdue_days"],
                ) if "preg_checked" in status else "",
                "pen": {"name": pen["name"], "zone": pen["zone"],
                       "zoneLabel": labels.zone_label(pen["zone"])} if pen else None,
            },
            "performance": performance and {
                "litters": performance["litters"],
                "basis": labels.performance_basis(),
                "note": labels.stillborn_note(
                    performance["stillborn_note"]["overall"],
                    performance["stillborn_note"]["without_first"],
                ) if performance["stillborn_note"] else "",
                "metrics": [
                    {"key": m["key"],
                     "label": labels.performance_label(m["key"]),
                     "unit": labels.performance_unit(m["key"]),
                     "digits": labels.performance_digits(m["key"]),
                     "value": m["value"],
                     "tier": m["tier"],
                     "tierLabel": labels.tier_label(m["tier"]) if m["tier"] else ""}
                    for m in performance["metrics"]
                ],
            },
            "events": [self._event_payload(e) for e in events],
        }

    def _add_event(self, payload, token) -> Tuple[int, dict]:
        """記一筆事件。**記錄即完成** —— 不另設「勾選完成」,所以不會出現
        「說做了卻沒有資料」的缺口(使用者決定)。
        """
        farm_id, user, err = self._need_farm(token)
        if err:
            return err

        sow_id = payload.get("sowId")
        if not isinstance(sow_id, int):
            return 400, {"error": "請指定母豬"}
        sow = self.store.get_sow(farm_id, sow_id)
        if sow is None:
            return 404, {"error": "找不到這頭母豬"}

        code = payload.get("type")
        if code not in schedule.KNOWN_EVENTS:
            return 400, {"error": f"不認得的事件類型:{code}"}

        when = _date(payload.get("date"))
        if when is None:
            return 400, {"error": "日期格式錯誤"}

        detail = payload.get("detail")
        detail = detail if isinstance(detail, dict) else {}
        detail = {k: v for k, v in list(detail.items())[:config.MAX_EVENT_FIELDS]}

        # 離乳仔豬評分由牧場主自評,1~5 分,**可以不評**。
        # 沒評分時不可以補一個中間值 —— 那會讓「沒人看過」與「看過覺得
        # 普通」變成同一件事(憲法第三條第 6 款)。
        if "wean_score" in detail:
            score = detail["wean_score"]
            if score in (None, ""):
                detail.pop("wean_score")
            elif isinstance(score, bool) or not isinstance(score, int) \
                    or not 1 <= score <= 5:
                return 400, {"error": "離乳評分請填 1 到 5,或留空不評"}

        # 移欄:直接打欄位編號,不必先到設定頁一個一個新增 —— 一區動輒
        # 幾百個欄位,要求先手動建一輪根本不會有人做(使用者要求)。
        # 第一次用到某個編號就直接建立;之後同一區打同樣的編號會找到
        # 同一個欄位,不會越用越多筆。
        # detail 存人類看得懂的快照(欄位名稱、區域)而不是只存 id ——
        # 之後這個欄位被刪除或改名,時間軸上這筆記錄仍看得懂當時搬去
        # 了哪裡。
        move_to_pen_id = None
        if code == schedule.MOVE_PEN:
            zone = detail.get("zone")
            if zone not in schedule.ZONES:
                return 400, {"error": "請選擇區域"}
            name = detail.get("pen_name")
            if not isinstance(name, str) or not name.strip():
                return 400, {"error": "請填寫欄位編號"}
            name = name.strip()[:config.MAX_PEN_NAME_CHARS]

            pen = next((p for p in self.store.list_pens(farm_id, zone) if p["name"] == name),
                      None)
            if pen is None:
                if len(self.store.list_pens(farm_id)) >= config.MAX_PENS_PER_FARM:
                    return 400, {"error": f"產房欄位最多 {config.MAX_PENS_PER_FARM} 個"}
                pen = {"id": self.store.add_pen(farm_id, name, zone), "name": name}

            occupied_by = next(
                (s for s in self.store.list_sows(farm_id, "active")
                 if s.get("pen_id") == pen["id"] and s["id"] != sow_id), None)
            if occupied_by is not None:
                return 409, {"error": f"{pen['name']} 已經有 {occupied_by['ear_tag']} 在裡面"}
            move_to_pen_id = pen["id"]
            detail = {"pen_id": pen["id"], "pen_name": pen["name"], "zone": zone}

        event_id = self.store.add_sow_event(
            farm_id, sow_id, code, when, detail, recorded_by=user.id)

        # 事件的連帶效果。寫在這裡而非 schedule.py:那一層是純推算,
        # 不該有副作用。
        after = dict(sow)
        if code == "FW":
            after["parity"] = (sow.get("parity") or 0) + 1
        elif code == "WN":
            after["pen_id"] = None            # 離乳後產房欄位空出來
        elif code == schedule.MOVE_PEN:
            after["pen_id"] = move_to_pen_id
        elif code in ("SAL", "DTH"):
            after["status"] = "culled" if code == "SAL" else "dead"
            after["pen_id"] = None
            # 離群時自動加民國年後綴,裸號釋放給新豬(牧場既有慣例)。
            # **用事件日期的年份而非今天** —— 補登去年的淘汰才不會標錯。
            suffix = f"-D{when.year - 1911}"
            if not sow["ear_tag"].endswith(suffix):
                after["ear_tag"] = sow["ear_tag"] + suffix

        changed = {k: v for k, v in after.items()
                   if k in ("parity", "pen_id", "status", "ear_tag")
                   and v != sow.get(k)}
        if changed:
            self.store.update_sow(farm_id, sow_id, **changed)

        return 200, {"id": event_id, "sow": self._sow_payload({**sow, **changed})}

    def _delete_event(self, token, event_id) -> Tuple[int, dict]:
        """員工只能刪自己記的、且是最新一筆。

        完全不能改的話,實務上會變成「先不記、等老闆來」—— 反而遺失資料
        (憲法第十一條第 5 款)。
        """
        farm_id, user, err = self._need_farm(token)
        if err:
            return err

        events = self.store.list_sow_events(farm_id)
        target = next((e for e in events if e["id"] == event_id), None)
        if target is None:
            return 404, {"error": "找不到這筆記錄"}

        if not user.is_owner:
            newest = max(events, key=lambda e: (e["event_date"], e["id"]))
            if target["recorded_by"] != user.id:
                return 403, {"error": "只能修正自己記的那一筆"}
            if target["id"] != newest["id"]:
                return 403, {"error": "只能修正最新一筆,較舊的請牧場主處理"}

        self.store.delete_sow_event(farm_id, event_id)
        if target["event_type"] == schedule.MOVE_PEN:
            self._revert_pen_after_undo(farm_id, target["sow_id"])
        return 200, {"ok": True}

    def _revert_pen_after_undo(self, farm_id, sow_id) -> None:
        """收回一筆移欄記錄後,母豬目前的欄位要退回上一筆移欄記錄(或退回
        沒有指派,若這是她第一筆移欄)。收回代表「這筆記錄不算數」,
        不是「這頭豬還留在原地」—— 不退回的話,那個欄位會一直顯示被
        佔用,擋住其他母豬移進去,而使用者已經按了「收回」以為復原了。
        """
        remaining = [e for e in self.store.list_sow_events(farm_id, sow_id)
                    if e["event_type"] == schedule.MOVE_PEN]
        pen_id = remaining[-1]["detail"].get("pen_id") if remaining else None
        self.store.update_sow(farm_id, sow_id, pen_id=pen_id)

    def _tasks(self, token, path) -> Tuple[int, dict]:
        """這一週的工作。依工作類型分組,不按日期 —— 這個場跑批次生產,
        一週一批(specs/v2-facts.md 第 7 條)。
        """
        farm_id, user, err = self._need_farm(token)
        if err:
            return err

        start = _date(_query(path, "start")) or _monday(_today())
        end = start + timedelta(days=6)
        sows = self.store.list_sows(farm_id, "active")
        events = self.store.list_sow_events(farm_id)
        cfg = self._farm_settings(farm_id)

        groups = schedule.build_week_tasks(sows, events, start, end, cfg)

        # 自訂工作**分開回傳**(已確認的設計決定)—— 混在 groups 裡的話
        # 使用者分不出哪些是系統依生產週期推算的、哪些是自己排的。
        custom = schedule.build_custom_tasks(
            self.store.list_custom_tasks(farm_id),
            self.store.list_task_done(farm_id, start, end),
            start, end)

        return 200, {
            "weekStart": start.isoformat(),
            "weekEnd": end.isoformat(),
            "groups": [
                {"kind": g["kind"],
                 "label": labels.task_label(g["kind"]),
                 "tasks": [{"sowId": t.sow_id, "earTag": t.ear_tag,
                            "due": t.due.isoformat(), "why": t.why}
                           for t in g["tasks"]]}
                for g in groups
            ],
            "custom": [
                {"id": t["id"], "name": t["name"], "repeat": t["repeat"],
                 "repeatLabel": labels.repeat_label(t["repeat"]),
                 "due": t["due"].isoformat(), "done": t["done"]}
                for t in custom
            ],
        }

    def _alerts(self, token) -> Tuple[int, dict]:
        farm_id, user, err = self._need_farm(token)
        if err:
            return err

        sows = self.store.list_sows(farm_id, "active")
        events = self.store.list_sow_events(farm_id)
        pens = self.store.list_pens(farm_id)
        cfg = self._farm_settings(farm_id)
        today = _today()

        return 200, {
            "openSows": schedule.overdue_sows(sows, events, today, cfg),
            "pens": schedule.pen_pressure(sows, events, pens, today, cfg),
        }

    def _farm_settings(self, farm_id) -> dict:
        """牧場自訂的生產參數,補上未設定項目的預設值。"""
        return schedule.settings_with_defaults(self.store.get_farm_settings(farm_id))

    def _get_settings(self, token) -> Tuple[int, dict]:
        """設定畫面的內容。

        一起回傳預設值與範圍,前端才不必自己維護一份 —— 兩邊各存一份的話,
        改了後端而忘了改前端,畫面上的「預設 114 天」會變成謊話。
        """
        farm_id, user, err = self._need_farm(token)
        if err:
            return err
        deny = self._need_owner(user)
        if deny:
            return deny

        saved = self.store.get_farm_settings(farm_id)
        return 200, {
            "settings": schedule.settings_with_defaults(saved),
            "defaults": dict(schedule.DEFAULTS),
            "custom": sorted(saved.keys()),      # 哪幾項被改過,畫面要標出來
            "fields": [
                {"key": key, "label": labels.setting_label(key),
                 "hint": labels.setting_hint(key),
                 "min": low, "max": high, "unit": labels.setting_unit(key)}
                for key, (low, high) in schedule.SETTING_RANGES.items()
            ],
        }

    def _put_settings(self, payload, token) -> Tuple[int, dict]:
        """儲存設定。

        **只存與預設值不同的項目**(見 db.Store.get_farm_settings)。
        整份存下來的話,日後量到更好的預設值不會生效在任何既有牧場。
        """
        farm_id, user, err = self._need_farm(token)
        if err:
            return err
        deny = self._need_owner(user)
        if deny:
            return deny

        incoming = payload.get("settings")
        if not isinstance(incoming, dict):
            return 400, {"error": "設定格式錯誤"}

        cleaned, problems = schedule.clean_settings(incoming)
        if problems:
            return 400, {"error": problems[0], "problems": problems}

        self.store.set_farm_settings(farm_id, cleaned)
        return 200, {
            "settings": schedule.settings_with_defaults(cleaned),
            "custom": sorted(cleaned.keys()),
        }

    def _review(self, token) -> Tuple[int, dict]:
        """值得檢視的母豬。**不是淘汰建議** —— 見 core/labels.review_caveat。"""
        farm_id, user, err = self._need_farm(token)
        if err:
            return err
        deny = self._need_owner(user)
        if deny:
            return deny

        # 含已離群的母豬 —— sows_worth_review 只把離群的納入比較基準,
        # 最終名單仍然只列在場的(她才有「要不要繼續留」這個決定可做)。
        sows = self.store.list_sows(farm_id, None)
        events = self.store.list_sow_events(farm_id)
        cfg = self._farm_settings(farm_id)

        rows = schedule.sows_worth_review(sows, events, _today(), cfg)
        return 200, {
            "caveat": labels.review_caveat(),
            "sows": [
                {"sowId": r["sow_id"], "earTag": r["ear_tag"],
                 "parity": r["parity"], "litters": r["litters"],
                 "reasons": [{"code": x["code"],
                              "label": labels.review_label(x["code"]),
                              "detail": x["detail"]}
                             for x in r["reasons"]]}
                for r in rows
            ],
        }

    def _monthly_report(self, token, path) -> Tuple[int, dict]:
        """生產月報,12 項指標。owner 專屬(規格第 17 條)。"""
        farm_id, user, err = self._need_farm(token)
        if err:
            return err
        deny = self._need_owner(user)
        if deny:
            return deny

        today = _today()
        year, month = today.year, today.month
        raw = _query(path, "month")
        if raw:
            parts = raw.strip()[:7].split("-")
            if len(parts) != 2 or not all(p.isdigit() for p in parts):
                return 400, {"error": "月份格式錯誤,請用 YYYY-MM"}
            year, month = int(parts[0]), int(parts[1])
        if not 1 <= month <= 12:
            return 400, {"error": "月份格式錯誤,請用 YYYY-MM"}

        start, end = schedule.month_bounds(year, month)
        sows = self.store.list_sows(farm_id, None)
        events = self.store.list_sow_events(farm_id)
        cfg = self._farm_settings(farm_id)
        report = schedule.monthly_report(sows, events, start, end, cfg)

        return 200, {
            "start": _iso(report["start"]),
            "end": _iso(report["end"]),
            "herdSize": report["herdSize"],
            "metrics": [
                {"key": key,
                 "label": labels.month_report_label(key),
                 "unit": labels.month_report_unit(key),
                 "digits": labels.month_report_digits(key),
                 "value": m["value"], "n": m["n"]}
                for key, m in report["metrics"].items()
            ],
            "basis": labels.month_report_basis(),
        }

    def _import_preview(self, payload, token) -> Tuple[int, dict]:
        farm_id, user, err = self._need_farm(token)
        if err:
            return err
        deny = self._need_owner(user)
        if deny:
            return deny

        raw = payload.get("content")
        if not isinstance(raw, str) or not raw.strip():
            return 400, {"error": "請選擇要匯入的檔案"}

        result = importer.parse(raw, today=_today())
        return 200, importer.summarize(result)

    def _import_commit(self, payload, token) -> Tuple[int, dict]:
        farm_id, user, err = self._need_farm(token)
        if err:
            return err
        deny = self._need_owner(user)
        if deny:
            return deny

        raw = payload.get("content")
        if not isinstance(raw, str) or not raw.strip():
            return 400, {"error": "請選擇要匯入的檔案"}

        exclude = payload.get("excludeLines")
        exclude = [n for n in exclude if isinstance(n, int)] if isinstance(exclude, list) else []

        result = importer.parse(raw, today=_today())
        stats = importer.import_into(self.store, farm_id, result,
                                     exclude_lines=exclude, recorded_by=user.id)
        return 200, stats

    def _pens(self, token, path) -> Tuple[int, dict]:
        """產房欄位清單,含目前佔用者。

        設定頁要顯示每一欄目前是誰佔用;紀錄頁的移欄表單要知道哪些欄位
        是空的才能列出來選 —— 兩邊共用同一個端點,不必為此各自算一次。
        """
        farm_id, user, err = self._need_farm(token)
        if err:
            return err

        zone = _query(path, "zone")
        pens = self.store.list_pens(farm_id, zone)
        occupant = {s["pen_id"]: s for s in self.store.list_sows(farm_id, "active")
                   if s.get("pen_id")}

        return 200, {"pens": [
            {"id": p["id"], "name": p["name"], "zone": p["zone"],
             "zoneLabel": labels.zone_label(p["zone"]),
             "occupant": ({"sowId": occupant[p["id"]]["id"],
                          "earTag": occupant[p["id"]]["ear_tag"]}
                         if p["id"] in occupant else None)}
            for p in pens
        ]}

    def _add_pen(self, payload, token) -> Tuple[int, dict]:
        farm_id, user, err = self._need_farm(token)
        if err:
            return err
        deny = self._need_owner(user)
        if deny:
            return deny

        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            return 400, {"error": "請填寫欄位編號"}

        zone = payload.get("zone") or schedule.ZONE_FARROWING
        if zone not in schedule.ZONES:
            return 400, {"error": f"不認得的區域:{zone}"}

        if len(self.store.list_pens(farm_id)) >= config.MAX_PENS_PER_FARM:
            return 400, {"error": f"產房欄位最多 {config.MAX_PENS_PER_FARM} 個"}
        return 200, {"id": self.store.add_pen(
            farm_id, name.strip()[:config.MAX_PEN_NAME_CHARS], zone)}

    def _delete_pen(self, token, pen_id) -> Tuple[int, dict]:
        """刪除欄位。有母豬在裡面一樣可以刪(比照 ON DELETE SET NULL,
        db.py 的 delete_pen 會把那頭母豬的 pen_id 清成 None)—— 這只是
        「這個欄位不存在了」,不代表那頭母豬不存在,不該因為欄位設定
        錯誤而擋住刪除。
        """
        farm_id, user, err = self._need_farm(token)
        if err:
            return err
        deny = self._need_owner(user)
        if deny:
            return deny

        ok = self.store.delete_pen(farm_id, pen_id)
        return (200, {"ok": True}) if ok else (404, {"error": "找不到這個欄位"})

    def _list_custom_tasks(self, token) -> Tuple[int, dict]:
        """自訂工作的設定清單(不是這週的排程,那在 /api/tasks)。

        員工看得到 —— 他要知道自己被排了什麼。新增與刪除才限牧場主。
        """
        farm_id, user, err = self._need_farm(token)
        if err:
            return err
        return 200, {"tasks": [
            {"id": t["id"], "name": t["name"],
             "startDate": _iso(t["start_date"]),
             "repeat": t["repeat_rule"],
             "repeatLabel": labels.repeat_label(t["repeat_rule"])}
            for t in self.store.list_custom_tasks(farm_id)
        ]}

    def _add_custom_task(self, payload, token) -> Tuple[int, dict]:
        farm_id, user, err = self._need_farm(token)
        if err:
            return err
        deny = self._need_owner(user)
        if deny:
            return deny

        name = _text(payload.get("name"), config.MAX_TASK_NAME_CHARS)
        if not name:
            return 400, {"error": "請填寫工作名稱"}

        start = _date(payload.get("startDate"))
        if start is None:
            return 400, {"error": "請選擇起始日期"}

        rule = payload.get("repeat") or "once"
        if rule not in schedule.REPEAT_RULES:
            return 400, {"error": f"不認得的重複方式:{rule}"}

        if len(self.store.list_custom_tasks(farm_id)) >= config.MAX_CUSTOM_TASKS_PER_FARM:
            return 400, {"error": f"自訂工作最多 {config.MAX_CUSTOM_TASKS_PER_FARM} 項"}

        task_id = self.store.add_custom_task(farm_id, name, start, rule)
        return 200, {"id": task_id}

    def _delete_custom_task(self, token, task_id) -> Tuple[int, dict]:
        farm_id, user, err = self._need_farm(token)
        if err:
            return err
        deny = self._need_owner(user)
        if deny:
            return deny

        ok = self.store.delete_custom_task(farm_id, task_id)
        return (200, {"ok": True}) if ok else (404, {"error": "找不到這項工作"})

    def _toggle_custom_task(self, payload, token) -> Tuple[int, dict]:
        """把某一次發生標記成完成 / 取消完成。

        **員工也能標** —— 工作就是他在做的,標完成是記錄不是經營決策。
        帶 due_date 而不是只有 task_id:重複性工作每一次發生各自標記,
        少了日期就只記得住最後一次(見 db.py 的 custom_task_done)。
        """
        farm_id, user, err = self._need_farm(token)
        if err:
            return err

        task_id = payload.get("taskId")
        if not isinstance(task_id, int):
            return 400, {"error": "請指定工作"}

        due = _date(payload.get("due"))
        if due is None:
            return 400, {"error": "日期格式錯誤"}

        done = bool(payload.get("done"))
        ok = (self.store.mark_task_done(farm_id, task_id, due) if done
              else self.store.unmark_task_done(farm_id, task_id, due))
        if not ok and done:
            return 404, {"error": "找不到這項工作"}
        return 200, {"ok": True, "done": done}

    @staticmethod
    def _boar_payload(row) -> dict:
        return {
            "id": row["id"],
            "earTag": row["ear_tag"],
            "breed": row.get("breed") or "",
            "status": row.get("status") or "active",
            "sireTag": row.get("sire_tag") or "",
            "damTag": row.get("dam_tag") or "",
            "entryDate": _iso(row.get("entry_date")),
        }

    @staticmethod
    def _boar_event_payload(row) -> dict:
        return {
            "id": row["id"],
            "boarId": row["boar_id"],
            "type": row["event_type"],
            "date": _iso(row["event_date"]),
            "detail": row.get("detail") or {},
            "recordedBy": row.get("recorded_by"),
        }

    def _list_boars(self, token, path) -> Tuple[int, dict]:
        """公豬清單。配種記錄要選公豬,所以員工也讀得到。

        預設只回在場的 —— 記錄用的選單(配種/採精/種豬死亡)不該選到
        已經死亡的公豬。跟母豬清單同樣的慣例:`?all=1` 才回全部
        (含已死亡的),公豬頁的瀏覽/搜尋用這份,死亡的公豬還是要看得到、
        找得到,不能整個從畫面上消失。
        """
        farm_id, user, err = self._need_farm(token)
        if err:
            return err
        status = "active"
        if "?" in path and "all" in path.split("?", 1)[1]:
            status = None
        return 200, {"boars": [self._boar_payload(b)
                               for b in self.store.list_boars(farm_id, status)]}

    def _add_boar(self, payload, token) -> Tuple[int, dict]:
        """種豬進場。公豬走這裡,母豬走 /api/sows —— 兩者是不同的實體。"""
        farm_id, user, err = self._need_farm(token)
        if err:
            return err

        tag = payload.get("earTag")
        if not isinstance(tag, str) or not tag.strip():
            return 400, {"error": "請填寫耳號"}
        tag = tag.strip()[:config.MAX_EAR_TAG_CHARS]

        if self.store.find_boar_by_tag(farm_id, tag):
            return 409, {"error": f"耳號 {tag} 已經在場,不能重複"}

        boar_id = self.store.add_boar(
            farm_id, tag,
            entry_date=_date(payload.get("entryDate")) or _today(),
            breed=_text(payload.get("breed"), config.MAX_BREED_CHARS),
            sire_tag=_text(payload.get("sireTag"), config.MAX_EAR_TAG_CHARS),
            dam_tag=_text(payload.get("damTag"), config.MAX_EAR_TAG_CHARS),
        )
        return 200, {"id": boar_id, "earTag": tag}

    def _boar_detail(self, token, boar_id) -> Tuple[int, dict]:
        farm_id, user, err = self._need_farm(token)
        if err:
            return err
        boar = self.store.get_boar(farm_id, boar_id)
        if boar is None:
            return 404, {"error": "找不到這頭公豬"}

        events = self.store.list_boar_events(farm_id, boar_id)

        # 配種績效比對的是全場母豬的配種記錄,不是這頭公豬自己的
        # boar_events —— 他配過誰、配了幾次是記在母豬的 MT 事件裡。
        sow_events = self.store.list_sow_events(farm_id)
        performance = schedule.boar_performance(boar["ear_tag"], sow_events)

        return 200, {
            "boar": self._boar_payload(boar),
            "performance": performance and {
                **performance,
                "basis": labels.boar_performance_basis(),
            },
            "events": [self._boar_event_payload(e) for e in events],
        }

    def _add_boar_event(self, payload, token) -> Tuple[int, dict]:
        """公豬事件:採精、死亡。死亡跟母豬死亡是同一種事件(使用者決定
        合併,改名「種豬死亡」)——分開存在 sow_events/boar_events 只是
        因為公豬跟母豬本來就是不同資料表。跟母豬事件一樣是「記錄即
        完成」,員工也能記 —— 配種記錄本來就是他在做的事(憲法第十一條)。
        """
        farm_id, user, err = self._need_farm(token)
        if err:
            return err

        boar_id = payload.get("boarId")
        if not isinstance(boar_id, int):
            return 400, {"error": "請指定公豬"}
        boar = self.store.get_boar(farm_id, boar_id)
        if boar is None:
            return 404, {"error": "找不到這頭公豬"}

        code = payload.get("type")
        if code not in schedule.KNOWN_BOAR_EVENTS:
            return 400, {"error": f"不認得的事件類型:{code}"}

        when = _date(payload.get("date"))
        if when is None:
            return 400, {"error": "日期格式錯誤"}

        detail = payload.get("detail")
        detail = detail if isinstance(detail, dict) else {}
        detail = {k: v for k, v in list(detail.items())[:config.MAX_EVENT_FIELDS]}

        event_id = self.store.add_boar_event(farm_id, boar_id, code, when, detail,
                                             recorded_by=user.id)

        # 跟母豬死亡同樣的連帶效果:離群時自動加民國年後綴,裸號釋放給
        # 新豬(牧場既有慣例),用事件日期的年份而非今天。
        if code == schedule.DEATH:
            after = {"status": "dead"}
            suffix = f"-D{when.year - 1911}"
            if not boar["ear_tag"].endswith(suffix):
                after["ear_tag"] = boar["ear_tag"] + suffix
            self.store.update_boar(farm_id, boar_id, **after)

        return 200, {"id": event_id}

    def _delete_boar_event(self, token, event_id) -> Tuple[int, dict]:
        """跟 _delete_event 同樣的收回規則:員工只能刪自己記的、且是
        最新一筆(憲法第十一條第 5 款)。
        """
        farm_id, user, err = self._need_farm(token)
        if err:
            return err

        events = self.store.list_boar_events(farm_id)
        target = next((e for e in events if e["id"] == event_id), None)
        if target is None:
            return 404, {"error": "找不到這筆記錄"}

        if not user.is_owner:
            newest = max(events, key=lambda e: (e["event_date"], e["id"]))
            if target["recorded_by"] != user.id:
                return 403, {"error": "只能修正自己記的那一筆"}
            if target["id"] != newest["id"]:
                return 403, {"error": "只能修正最新一筆,較舊的請牧場主處理"}

        self.store.delete_boar_event(farm_id, event_id)
        return 200, {"ok": True}

    def _recent_events(self, token, path) -> Tuple[int, dict]:
        """最近記錄的事件,給紀錄頁的「已記錄」清單用。母豬事件跟公豬事件
        (採精)合併成一份清單 —— 巡欄時連續記好幾筆,使用者
        要看到同一份「剛剛記了什麼」,不必分兩處確認。

        帶上 `canUndo`:能不能收回是**伺服器判定**的,不是前端自己算。
        前端只用它決定要不要畫按鈕;真正的把關在 _delete_event /
        _delete_boar_event(員工只能改自己記的、且是最新一筆)。兩邊
        各判一次是刻意的 —— 前端那次是為了不給使用者一個按了必定
        失敗的按鈕。

        兩種事件各自的「最新一筆」分開算 —— 員工能不能收回一筆母豬
        事件,跟他今天有沒有記過公豬事件無關。
        """
        farm_id, user, err = self._need_farm(token)
        if err:
            return err

        days = 1
        raw = _query(path, "days")
        if raw and raw.isdigit():
            days = min(int(raw), config.MAX_RECENT_EVENT_DAYS)
        since = _today() - timedelta(days=days - 1)

        def can_undo(e, newest):
            return bool(user.is_owner
                       or (e["recorded_by"] == user.id
                           and newest is not None and e["id"] == newest["id"]))

        sow_events = self.store.list_sow_events(farm_id)
        sow_tags = {s["id"]: s["ear_tag"] for s in self.store.list_sows(farm_id, None)}
        sow_newest = max(sow_events, key=lambda e: (e["event_date"], e["id"]), default=None)
        recent = [
            {**self._event_payload(e), "kind": "sow",
             "earTag": sow_tags.get(e["sow_id"], ""),
             "canUndo": can_undo(e, sow_newest)}
            for e in sow_events if e["event_date"] >= since
        ]

        boar_events = self.store.list_boar_events(farm_id)
        boar_tags = {b["id"]: b["ear_tag"] for b in self.store.list_boars(farm_id)}
        boar_newest = max(boar_events, key=lambda e: (e["event_date"], e["id"]), default=None)
        recent += [
            {**self._boar_event_payload(e), "kind": "boar",
             "earTag": boar_tags.get(e["boar_id"], ""),
             "canUndo": can_undo(e, boar_newest)}
            for e in boar_events if e["event_date"] >= since
        ]

        recent.sort(key=lambda e: (e["date"], e["id"]), reverse=True)
        return 200, {"events": recent[:config.MAX_RECENT_EVENTS]}


    def _grade(self, payload: dict) -> Tuple[int, dict]:
        """生產健檢。純計算,不呼叫 AI —— 額度用盡時這裡照常運作。"""
        report = validate(payload.get("values") or {})
        if not report.ok:
            return 400, {
                "errors": [{"key": e.key, "message": e.message} for e in report.errors],
            }

        graded = grade_all(report.cleaned, metrics_index())
        weaknesses = rank_weaknesses(graded)

        return 200, {
            "grades": {
                key: {
                    "value": result.value,
                    "grade": result.grade,
                    "gradeLabel": grade_label(result.grade),
                    "percentileBand": list(result.percentile_band),
                    "name": get_metric(key)["name"],
                    "unit": get_metric(key).get("unit", ""),
                    "mean": get_metric(key)["mean"],
                    "sampleNote": sample_size_note(key),
                    # 弱項判斷規則只存在後端(core/diagnosis.py),
                    # 前端不自行判斷,直接讀這個欄位,避免同一條規則有兩份定義。
                    "isWeak": is_weak(key, result),
                }
                for key, result in graded.items()
            },
            "weaknesses": [self._weakness_payload(w) for w in weaknesses],
            "warnings": [{"key": w.key, "message": w.message} for w in report.warnings],
            "source": source_label(),
            "shortfallNote": shortfall_note(),
            "upstreamNote": upstream_note(),
            "medicalDisclaimer": medical_disclaimer(),
        }


    @staticmethod
    def _from_wire_weakness(w: dict) -> dict:
        """把瀏覽器送回來的弱項(駝峰式,如 /api/grade 回傳的格式)轉成
        內部慣例的底線式,交給 ai/prompts.py 使用。

        曾經在這裡漏掉轉換:/api/grade 用 shortfallSd/downstreamNames 回給瀏覽器,
        瀏覽器原封不動送回 /api/advise,但 ai/prompts.py 用的是
        shortfall_sd/downstream_names,兩者對不上導致 KeyError、伺服器 502。
        駝峰↔底線的轉換只該在 HTTP 邊界做一次,不該要求呼叫端自己轉。
        """
        return {
            "name": w.get("name"),
            "grade": w.get("grade"),
            "shortfall_sd": w.get("shortfallSd", w.get("shortfall_sd")),
            "improvement": w.get("improvement", ""),
            "downstream_names": w.get("downstreamNames", w.get("downstream_names", [])),
        }

    def _advise(self, payload: dict, client: str) -> Tuple[int, dict]:
        """健檢的改善建議。AI 只解讀已算好的弱項(憲法第二條)。"""
        raw_weaknesses = payload.get("weaknesses") or []
        if not raw_weaknesses:
            return 200, {"advice": ""}
        weaknesses = [self._from_wire_weakness(w) for w in raw_weaknesses]

        wait = self._throttled(client)
        if wait is not None:
            return 429, {"error": f"請稍候 {wait} 秒再送出"}

        if self._over_daily_budget():
            return 503, {
                "reason": "daily_limit",
                "error": f"今日 AI 諮詢已達上限。{ai_unavailable_note()}",
            }

        try:
            return 200, {
                "advice": "".join(self.consultant.advise(
                    weaknesses, reference_factors=payload.get("referenceFactors"),
                )),
            }
        except TransportError as e:
            return 503, self._transport_error(e)

    def advise_events(self, payload: dict, client: str, token: Optional[str] = None):
        """生產健檢改善建議的追問,逐段產出事件供串流。

        延續同一份改善建議繼續討論,用的是同一個 ADVICE_SYSTEM_PROMPT
        persona,不是疾病諮詢 —— 這樣使用者才能順著同一個脈絡問「這幾項
        應該先做哪個」,不會突然被當成在問診。

        事件:
          delta  AI 生成的一段文字
          error  含 status,後續不再產出
          done   正常結束
        """
        gate = self._gate(token)
        if gate:
            yield {"type": "error", "status": 401, **gate}
            return

        raw_weaknesses = payload.get("weaknesses") or []
        if not raw_weaknesses:
            yield {"type": "error", "status": 400, "error": "沒有健檢結果可以討論,請先完成健檢"}
            return
        weaknesses = [self._from_wire_weakness(w) for w in raw_weaknesses]

        wait = self._throttled(client)
        if wait is not None:
            yield {
                "type": "error", "status": 429,
                "error": f"請稍候 {wait} 秒再送出下一題",
            }
            return

        if self._over_hourly_limit(client):
            yield {
                "type": "error", "status": 429, "reason": "hourly_limit",
                "error": (
                    f"每小時最多提問 {config.MAX_QUESTIONS_PER_HOUR} 次,"
                    "已達上限,請稍後再試。生產健檢不受影響。"
                ),
            }
            return

        if self._over_daily_budget():
            yield {
                "type": "error", "status": 503, "reason": "daily_limit",
                "error": "今日 AI 諮詢已達上限,請明天再試,或聯繫管理員調整額度。",
            }
            return

        try:
            stream = self.consultant.advise(
                weaknesses,
                reference_factors=payload.get("referenceFactors"),
                question=payload.get("question", ""),
                history=payload.get("history"),
            )
            for chunk in stream:
                yield {"type": "delta", "text": chunk}
        except ValueError as e:
            yield {"type": "error", "status": 400, "error": str(e)}
            return
        except TransportError as e:
            yield {"type": "error", "status": 503, **self._transport_error(e)}
            return

        yield {"type": "done"}

    def _advise_chat(self, payload: dict, client: str, token=None) -> Tuple[int, dict]:
        """把追問串流事件收攏成單一回應。

        供測試與不支援串流的呼叫端使用,邏輯與串流路徑共用,
        避免兩條路走久了行為不一致(見 _consult 的同一個理由)。
        """
        answer = []
        error: Optional[dict] = None

        for event in self.advise_events(payload, client, token):
            kind = event.pop("type")
            if kind == "delta":
                answer.append(event["text"])
            elif kind == "error":
                error = event

        if error is not None:
            status = error.pop("status")
            return status, error
        return 200, {"answer": "".join(answer)}

    @staticmethod
    def _transport_error(error: TransportError) -> dict:
        """把錯誤分類,讓前端能做出正確的降級提示(規格 6.5)。

        訊息一律用傳輸層自己產生的文字(str(error)),不在這裡覆蓋。
        兩個傳輸層(CLI/API)對同一種錯誤類型會給出不同、各自準確的說明——
        例如同樣是 NotLoggedIn,CLI 傳輸層講的是「請執行 claude auth login」,
        API 傳輸層講的是「請確認 ANTHROPIC_API_KEY」。這裡若寫死其中一種文字,
        另一條路徑出錯時會顯示不相關甚至誤導的訊息(曾實際發生:API key 401
        被錯誤顯示成「CLI 尚未登入」,診斷方向整個被帶偏)。
        """
        if isinstance(error, NotLoggedIn):
            return {"reason": "not_logged_in", "error": str(error)}
        if isinstance(error, QuotaExceeded):
            return {"reason": "quota", "error": str(error)}
        return {"reason": "error", "error": str(error)}


# --- HTTP 傳輸 ---

APP = Application(transport=select_transport(), store=select_store())


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def _session_token(self) -> Optional[str]:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:
            # 壞掉的 cookie 標頭視為未登入,不要讓整個請求爆掉
            return None
        morsel = jar.get(config.SESSION_COOKIE_NAME)
        return morsel.value if morsel else None

    @staticmethod
    def _session_cookie(token: str, max_age: int) -> str:
        # HttpOnly:JavaScript 讀不到,萬一有 XSS 也偷不走 session。
        # SameSite=Lax:別的網站送來的跨站請求不會帶上這張 cookie(CSRF)。
        # Secure:只走 HTTPS。本機開發是 http://localhost,瀏覽器對
        #   localhost 有豁免,所以兩邊都能運作,不必分環境設定。
        return (
            f"{config.SESSION_COOKIE_NAME}={token}; Path=/; HttpOnly; "
            f"SameSite=Lax; Secure; Max-Age={max_age}"
        )

    def _send(self, status: int, payload: dict) -> None:
        # Application 用這兩個鍵告訴 Handler 要不要動 cookie。
        # 一定要 pop 掉再序列化 —— session token 本身絕不能出現在
        # 回應內容裡,那等於繞過 HttpOnly 把它交給 JavaScript。
        set_token = payload.pop(SET_SESSION_KEY, None)
        clear = payload.pop(CLEAR_SESSION_KEY, False)

        body = to_json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 瀏覽器的快取**以網址為鍵**,跟哪個帳號登入無關。/api/sows 這種
        # 每個牧場內容都不同、網址卻一模一樣的請求,少了這個標頭就可能在
        # 同一台電腦換帳號登入時,端出上一個帳號留在磁碟快取裡的回應 ——
        # 也就是把 A 牧場的資料顯示給 B 看(憲法第十一條)。
        #
        # 靜態檔用的 no-cache 不夠:那只要求「用之前先問」,回應照樣寫進
        # 磁碟,拿得到這台電腦的人就翻得出別人的牧場資料。no-store 才是
        # 「不准存」。API 回應本來就很小,不快取沒有效能問題。
        self.send_header("Cache-Control", "no-store")
        if set_token:
            self.send_header(
                "Set-Cookie",
                self._session_cookie(set_token, config.SESSION_TTL_DAYS * 86400),
            )
        elif clear:
            self.send_header("Set-Cookie", self._session_cookie("", 0))
        self.end_headers()
        self.wfile.write(body)

    # 這次回應是不是靜態檔。只有靜態檔要加 Cache-Control —— API 與 SSE
    # 各自送自己的標頭,重複送同一個標頭反而是壞事。
    _serving_static = False

    def send_head(self):
        """靜態檔一律要瀏覽器回頭驗證,不可憑經驗自行判斷還新鮮。

        SimpleHTTPRequestHandler 只送 Last-Modified,不送 Cache-Control。
        少了這個標頭,瀏覽器會套用「啟發式快取」:自行猜一段新鮮期,期間
        完全不回頭問伺服器。實際踩到的情形是改完 app.js、重啟伺服器、
        重新整理,分頁跑的仍是舊檔 —— 連 <script src> 都拿到舊版,而同
        一支檔案用 fetch 加上查詢字串就是新的,因為那換成了另一個快取鍵。

        no-cache 不是「不要快取」,是「每次用之前先問」。既有的
        Last-Modified 仍然有效,沒改動時回 304 不重傳內容,所以不會變慢。

        正式站的 service worker 對程式碼採網路優先,本來就繞過這個問題;
        但 service worker 註冊起來之前的第一次載入不受它保護,那次一樣
        會拿到瀏覽器猜出來的舊檔。
        """
        self._serving_static = True
        try:
            return super().send_head()
        finally:
            self._serving_static = False

    def end_headers(self):
        if self._serving_static:
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_GET(self):
        if self.path.startswith("/api/"):
            self._send(*APP.handle_get(self.path, self._session_token()))
            return
        super().do_GET()

    def do_DELETE(self):
        if self.path.startswith("/api/"):
            self._send(*APP.handle_delete(self.path, self._session_token()))
            return
        self.send_error(405)

    def _send_event(self, payload: dict) -> None:
        line = "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
        self.wfile.write(line.encode("utf-8"))
        self.wfile.flush()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0

        # 在讀進記憶體之前就擋掉,否則限流形同虛設
        if too_large(length, self.path):
            self._send(413, {
                "error": f"請求過大,上限 {request_limit(self.path) // 1024} KB",
            })
            return

        raw = self.rfile.read(length) if length else b"{}"
        client = client_ip(self.headers, self.client_address[0])
        token = self._session_token()

        if self.path == "/api/advise-chat":
            self._stream(raw, lambda payload: APP.advise_events(payload, client, token))
            return
        self._send(*APP.handle_post(self.path, raw, client, token))

    def _stream(self, raw: bytes, make_events) -> None:
        """SSE 串流的共用外殼:解析請求體、送 SSE 表頭、把事件逐一寫出去。

        疾病諮詢與健檢改善建議的追問都走這裡 —— 差別只在 make_events
        呼叫哪個事件產生器,HTTP 傳輸這一層完全一樣,不必寫兩份。
        """
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {"error": "請求格式錯誤,需為 UTF-8 編碼的 JSON"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            for event in make_events(payload):
                self._send_event(event)
        except (BrokenPipeError, ConnectionAbortedError):
            pass  # 瀏覽器中途離開

    def log_message(self, fmt, *args):
        pass


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    transport = APP.transport
    mode = (
        "API 計費(對外上線)"
        if isinstance(transport, AnthropicApiTransport)
        else "訂閱額度(本機/demo)"
    )

    if not transport.is_available():
        print(f"警告: {ai_unavailable_note()}")
    elif not transport.is_logged_in():
        print("警告: 尚未登入/設定金鑰,請確認 claude auth login 或 ANTHROPIC_API_KEY")
    else:
        print(f"AI 傳輸層就緒:{mode}")

    print(f"豬豬顧問啟動: http://{config.HOST}:{config.PORT}")
    ThreadedServer((config.HOST, config.PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
