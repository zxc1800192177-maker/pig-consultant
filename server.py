"""HTTP 層 —— 只做路由、輸入檢查、限流。

商業邏輯都在 core/(計算)與 ai/(生成)。這個檔案不該出現任何
分級規則、領域判斷或提示詞 —— 那些改變時不該連帶動到伺服器。

Application 與 HTTP 傳輸分離,測試才能不開 socket 直接驗證行為。
"""

import http.server
import json
import pathlib
import socketserver
import time
from typing import Dict, Optional, Tuple

import config
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


class Application:
    """路由與請求處理。不綁定 HTTP 傳輸,方便測試。"""

    def __init__(self, transport=None):
        self.transport = transport or ClaudeCliTransport()
        self.consultant = Consultant(self.transport)
        self._last_ai_request: Dict[str, float] = {}
        self._ai_request_count = 0
        self._count_day = time.strftime("%Y-%m-%d")

    # --- 輔助 ---

    def _throttled(self, client: str) -> Optional[float]:
        """回傳還需等待幾秒;None 表示可以放行。只套用在會花額度的端點。"""
        now = time.monotonic()
        last = self._last_ai_request.get(client)
        if last is not None:
            elapsed = now - last
            if elapsed < config.MIN_REQUEST_INTERVAL_SEC:
                return round(config.MIN_REQUEST_INTERVAL_SEC - elapsed, 1)
        self._last_ai_request[client] = now
        return None

    def _over_daily_budget(self) -> bool:
        """對外上線走 API 計費,失控會直接扣款,不像訂閱額度頂多是用完。

        全站共用一個計數,不分客戶端 —— 這是保護帳單,不是保護單一使用者。
        以行程記憶體計數,重啟即重置;真正的花費上限要在
        console.anthropic.com 另外設定,這裡只是製程內的安全氣囊。
        """
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

    def handle_get(self, path: str) -> Tuple[int, dict]:
        if path == "/api/health":
            return 200, {
                "aiAvailable": self.transport.is_logged_in(),
                # 健檢是純計算,不依賴 AI 或網路,永遠可用(規格 6.5)
                "gradingAvailable": True,
                "source": source_label(),
                # 文字由後端提供,前端不自己寫一份(措辭改動只需改一處)
                "aiUnavailableNote": ai_unavailable_note(),
            }
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

    def handle_post(self, path: str, raw: bytes, client: str) -> Tuple[int, dict]:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return 400, {"error": "請求格式錯誤,需為 UTF-8 編碼的 JSON"}

        if path == "/api/grade":
            return self._grade(payload)
        if path == "/api/consult":
            return self._consult(payload, client)
        if path == "/api/advise":
            return self._advise(payload, client)
        return 404, {"error": "not found"}

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
        }

    def consult_events(self, payload: dict, client: str):
        """疾病諮詢,逐段產出事件供串流。

        通報須知與升級判斷是計算出來的,在呼叫 AI 之前就先送出 ——
        使用者可能在 AI 回完前關掉頁面,防疫提示不能等到最後(憲法第一條)。

        事件:
          meta   確定性的部分(通報須知、升級警示、免責聲明)
          delta  AI 生成的一段文字
          error  含 status,後續不再產出
          done   正常結束
        """
        raw_weaknesses = payload.get("weaknesses") or []
        weaknesses = [self._from_wire_weakness(w) for w in raw_weaknesses]

        try:
            consultation = self.consultant.consult(
                payload.get("question", ""),
                weaknesses=weaknesses,
            )
        except ValueError as e:
            yield {"type": "error", "status": 400, "error": str(e)}
            return

        yield {
            "type": "meta",
            "baselineNotice": consultation.baseline_notice,
            "disclaimer": reportable_disclaimer(),
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

    def _consult(self, payload: dict, client: str) -> Tuple[int, dict]:
        """把串流事件收攏成單一回應。

        供測試與不支援串流的呼叫端使用。與串流路徑共用同一份邏輯,
        避免兩條路走久了行為不一致。
        """
        meta: dict = {}
        answer = []
        error: Optional[dict] = None

        for event in self.consult_events(payload, client):
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
            return 200, {"advice": "".join(self.consultant.advise(weaknesses))}
        except TransportError as e:
            return 503, self._transport_error(e)

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

APP = Application(transport=select_transport())


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/"):
            self._send(*APP.handle_get(self.path))
            return
        super().do_GET()

    def _send_event(self, payload: dict) -> None:
        line = "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
        self.wfile.write(line.encode("utf-8"))
        self.wfile.flush()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        client = self.client_address[0]

        if self.path == "/api/consult":
            self._stream_consult(raw, client)
            return
        self._send(*APP.handle_post(self.path, raw, client))

    def _stream_consult(self, raw: bytes, client: str) -> None:
        """串流疾病諮詢,讓首段文字盡早出現(規格第 7 節:3 秒內)。"""
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
            for event in APP.consult_events(payload, client):
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
