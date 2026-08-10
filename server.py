"""HTTP 層 —— 只做路由、輸入檢查、限流。

商業邏輯都在 core/(計算)與 ai/(生成)。這個檔案不該出現任何
分級規則、領域判斷或提示詞 —— 那些改變時不該連帶動到伺服器。

Application 與 HTTP 傳輸分離,測試才能不開 socket 直接驗證行為。
"""

import http.server
import json
import pathlib
import socketserver
import threading
import time
from http.cookies import SimpleCookie
from typing import Dict, List, Optional, Tuple

import config
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
    reportable_disclaimer,
    sample_size_note,
    shortfall_note,
    source_label,
    upstream_note,
)
from core.metrics import validate

BASE_DIR = pathlib.Path(__file__).parent
WEB_DIR = BASE_DIR / "web"
EXAMPLE_PATH = BASE_DIR / "data" / "example_farm.json"


def too_large(content_length: int) -> bool:
    """請求體是否超過上限。

    必須在 read() 之前依 Content-Length 判斷 —— 伺服器原本會先把整包
    讀進記憶體,之後才輪到限流,所以每小時 20 次的限制完全擋不住大包灌流
    (實測 19.7MB 照單全收)。免費方案只有 512MB 記憶體。
    """
    return content_length > config.MAX_REQUEST_BYTES


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

    def _over_hourly_limit(self, client: str) -> bool:
        """每個 IP 每小時的提問上限,滑動視窗。

        只在實際放行時記錄 —— 被擋下的嘗試不計入,否則使用者越重試,
        額度恢復時間被推得越晚,等於因為被擋而受到額外懲罰。
        """
        with self._limit_lock:
            now = time.monotonic()
            cutoff = now - config.RATE_WINDOW_SEC

            # 順手清掉已經完全過期的來源,避免這份紀錄無限成長成攻擊面
            for ip in [ip for ip, hits in self._hourly_hits.items()
                       if not hits or hits[-1] <= cutoff]:
                del self._hourly_hits[ip]

            hits = [t for t in self._hourly_hits.get(client, []) if t > cutoff]
            if len(hits) >= config.MAX_QUESTIONS_PER_HOUR:
                self._hourly_hits[client] = hits
                return True

            hits.append(now)
            self._hourly_hits[client] = hits
            return False

    def _over_login_limit(self, client: str) -> bool:
        """登入/註冊/訪客建立的嘗試次數上限。

        跟提問限流分開計算,原因是威脅不同:提問是花錢,登入是被猜密碼,
        後者需要嚴格得多的窗口。訪客建立也算在這裡 —— 那會在資料庫寫入
        一列,不設限等於開放任何人把免費方案的容量灌爆。
        """
        with self._limit_lock:
            now = time.monotonic()
            cutoff = now - config.LOGIN_WINDOW_SEC

            for ip in [ip for ip, hits in self._login_attempts.items()
                       if not hits or hits[-1] <= cutoff]:
                del self._login_attempts[ip]

            hits = [t for t in self._login_attempts.get(client, []) if t > cutoff]
            if len(hits) >= config.MAX_LOGIN_ATTEMPTS_PER_WINDOW:
                self._login_attempts[client] = hits
                return True

            hits.append(now)
            self._login_attempts[client] = hits
            return False

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

    @staticmethod
    def _user_payload(user) -> dict:
        if user is None:
            return {"loggedIn": False}
        return {
            "loggedIn": True,
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
        if path == "/api/my-drugs":
            return self._list_my_drugs(token)
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
                "disclaimer": reportable_disclaimer(),
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
        if path == "/api/consult":
            return self._consult(payload, client, token)
        if path == "/api/advise":
            gate = self._gate(token)
            return (401, gate) if gate else self._advise(payload, client)
        if path == "/api/advise-chat":
            return self._advise_chat(payload, client, token)
        if path.startswith("/api/auth/"):
            return self._auth_post(path, payload, client, token)
        if path == "/api/health-checks":
            return self._save_health_check(payload, token)
        if path == "/api/my-drugs":
            return self._add_my_drug(payload, token)
        return 404, {"error": "not found"}

    def handle_delete(self, path: str, token: Optional[str] = None) -> Tuple[int, dict]:
        user = self._current_user(token)
        if user is None:
            return 401, {"error": "請先登入"}

        # 刪除一律連 user_id 一起帶進查詢(見 db.py 的約定)——
        # 只用 id 的話,換個號碼就能刪別人的資料。
        if path.startswith("/api/my-drugs/"):
            drug_id = self._path_id(path)
            if drug_id is None:
                return 400, {"error": "編號格式錯誤"}
            ok = self.store.delete_drug(user.id, drug_id)
            return (200, {"ok": True}) if ok else (404, {"error": "找不到這筆資料"})

        if path.startswith("/api/health-checks/"):
            check_id = self._path_id(path)
            if check_id is None:
                return 400, {"error": "編號格式錯誤"}
            ok = self.store.delete_health_check(user.id, check_id)
            return (200, {"ok": True}) if ok else (404, {"error": "找不到這筆資料"})

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

        # 註冊/登入/訪客建立都會消耗資源(雜湊運算或資料庫寫入),
        # 而且都是可以被自動化重複嘗試的入口,一律先過節流。
        if self._over_login_limit(client):
            return 429, {"error": "嘗試次數過多,請稍後再試"}

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

        return 200, {
            **self._user_payload(result.user),
            SET_SESSION_KEY: result.token,
        }

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

    def _list_my_drugs(self, token) -> Tuple[int, dict]:
        user = self._current_user(token)
        if user is None:
            return 401, {"error": "請先登入"}
        return 200, {"drugs": [self._drug_payload(d) for d in self.store.list_drugs(user.id)]}

    @staticmethod
    def _drug_payload(row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "dosageNote": row["dosage_note"],
            "withdrawalDays": row["withdrawal_days"],
        }

    def _add_my_drug(self, payload, token) -> Tuple[int, dict]:
        user = self._current_user(token)
        if user is None:
            return 401, {"error": "請先登入"}

        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            return 400, {"error": "請填寫藥品名稱"}

        if len(self.store.list_drugs(user.id)) >= config.MAX_MY_DRUGS:
            return 400, {"error": f"藥品庫最多 {config.MAX_MY_DRUGS} 筆,請先移除不用的"}

        note = payload.get("dosageNote")
        note = note.strip()[:config.MAX_DRUG_NOTE_CHARS] if isinstance(note, str) else ""

        days = payload.get("withdrawalDays")
        if not isinstance(days, (int, float)) or isinstance(days, bool) or days < 0:
            days = None

        drug_id = self.store.add_drug(
            user.id, name.strip()[:config.MAX_DRUG_NAME_CHARS], note,
            int(days) if days is not None else None,
        )
        return 200, {"id": drug_id}

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

    def _drugs_for_consult(self, payload: dict, token):
        """疾病諮詢要參考的藥品庫。

        已登入時一律以資料庫為準,忽略請求裡的 myDrugs —— 那個欄位是
        給未登入使用者用的(他們的藥品庫存在自己的瀏覽器)。已登入還信任
        它的話,等於任何人都能在請求裡塞一組假的劑量進去,而畫面上會
        顯示成「你自己藥品庫的資料」,這正是劑量查表化要防的事。
        """
        user = self._current_user(token)
        if user is None:
            return payload.get("myDrugs")
        return [
            {
                "name": d["name"],
                "dosageNote": d["dosage_note"],
                "withdrawalDays": d["withdrawal_days"],
            }
            for d in self.store.list_drugs(user.id)
        ]

    def consult_events(self, payload: dict, client: str, token: Optional[str] = None):
        """疾病諮詢,逐段產出事件供串流。

        通報須知與升級判斷是計算出來的,在呼叫 AI 之前就先送出 ——
        使用者可能在 AI 回完前關掉頁面,防疫提示不能等到最後(憲法第一條)。

        事件:
          meta   確定性的部分(通報須知、升級警示、免責聲明)
          delta  AI 生成的一段文字
          error  含 status,後續不再產出
          done   正常結束
        """
        # 登入檢查要在最前面 —— 排在通報須知之前。那些提示雖然不花錢,
        # 但未登入者連功能畫面都看不到,送出去也只是浪費。
        gate = self._gate(token)
        if gate:
            yield {"type": "error", "status": 401, **gate}
            return

        raw_weaknesses = payload.get("weaknesses") or []
        weaknesses = [self._from_wire_weakness(w) for w in raw_weaknesses]

        try:
            consultation = self.consultant.consult(
                payload.get("question", ""),
                weaknesses=weaknesses,
                history=payload.get("history"),
                my_drugs=self._drugs_for_consult(payload, token),
            )
        except ValueError as e:
            yield {"type": "error", "status": 400, "error": str(e)}
            return

        yield {
            "type": "meta",
            "baselineNotice": consultation.baseline_notice,
            # 醫療免責在回答正上方,由程式強制加,不依賴 AI 自己寫
            "medicalDisclaimer": medical_disclaimer(),
            "disclaimer": reportable_disclaimer(),
            # 劑量查表化:這份清單是伺服器直接算出來的,不經過 AI ——
            # 前端要把它跟 AI 生成的文字分開呈現,不能混在同一段 markdown 裡。
            "dosageReference": [
                {
                    "id": m.id,
                    "diseaseName": m.disease_name,
                    "drugs": m.drugs,
                    "sourceNote": m.source_note,
                }
                for m in consultation.dosage_matches
            ],
            "escalation": (
                {
                    "disease": consultation.escalation.disease,
                    "notice": consultation.escalation.notice,
                    "matchedTerms": consultation.escalation.matched_terms,
                }
                if consultation.escalation else None
            ),
        }

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
            for chunk in consultation.stream:
                yield {"type": "delta", "text": chunk}
        except TransportError as e:
            yield {"type": "error", "status": 503, **self._transport_error(e)}
            return

        yield {"type": "done"}

    def _consult(self, payload: dict, client: str, token=None) -> Tuple[int, dict]:
        """把串流事件收攏成單一回應。

        供測試與不支援串流的呼叫端使用。與串流路徑共用同一份邏輯,
        避免兩條路走久了行為不一致。
        """
        meta: dict = {}
        answer = []
        error: Optional[dict] = None

        for event in self.consult_events(payload, client, token):
            kind = event.pop("type")
            if kind == "meta":
                meta = event
            elif kind == "delta":
                answer.append(event["text"])
            elif kind == "error":
                error = event

        if error is not None:
            status = error.pop("status")
            return status, {**meta, **error}
        return 200, {**meta, "answer": "".join(answer)}

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

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if set_token:
            self.send_header(
                "Set-Cookie",
                self._session_cookie(set_token, config.SESSION_TTL_DAYS * 86400),
            )
        elif clear:
            self.send_header("Set-Cookie", self._session_cookie("", 0))
        self.end_headers()
        self.wfile.write(body)

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
        if too_large(length):
            self._send(413, {
                "error": f"請求過大,上限 {config.MAX_REQUEST_BYTES // 1024} KB",
            })
            return

        raw = self.rfile.read(length) if length else b"{}"
        client = client_ip(self.headers, self.client_address[0])
        token = self._session_token()

        if self.path == "/api/consult":
            self._stream(raw, lambda payload: APP.consult_events(payload, client, token))
            return
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
