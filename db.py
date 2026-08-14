"""資料存取層 —— 全專案唯一碰資料庫的地方。

單獨成模組的理由跟 ai/transport.py 完全一樣(SRP):換資料庫、換代管商
時只有這個檔案要改。測試注入 InMemoryStore,不需要真的資料庫,
600+ 個測試才能維持全離線、幾秒跑完的特性。

**資料隔離的關鍵約定:每一個讀寫方法都必須收 user_id,並且把它寫進
WHERE 條件。** 只靠「前端只會傳自己的 id」是不夠的 —— 使用者可以直接
呼叫 API 換一個 id。凡是少一個 user_id 條件的查詢,就是一個讓 A 看到
或刪掉 B 的資料的漏洞。這件事沒有例外,新增方法時一併照做。

Postgres 連線刻意「每次操作開一條、用完關掉」:免費方案的資料庫
(Neon)閒置時會自動休眠,長期持有的連線會在休眠後失效,下次使用時
才發現已經斷掉。每次重開雖然多花一點延遲,但不會出現「昨天還好好的
連線今天突然壞掉」這種難查的問題。以這個站的流量(每人每小時 20 題)
完全划算。
"""

import collections
import json
import secrets
from datetime import datetime, timezone
from typing import List, Optional

import config

try:  # psycopg 只有正式部署才需要;沒裝也要能 import db 跑測試
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - 取決於環境有沒有裝
    psycopg = None
    Jsonb = None


SCHEMA = """
-- 牧場是 v2 的隔離單位(憲法第十一條)。v1 以使用者隔離,因為那時的資料
-- (對話歷史、藥品庫)本來就是私人的;v2 的資料是一座牧場的共同財產,
-- 牧場主與員工都要記、都要看,但別的牧場一筆都不能看到。
CREATE TABLE IF NOT EXISTS farms (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  -- 只存與預設值不同的項目(見 Store.get_farm_settings)。用 JSONB 的
  -- 理由跟 sow_events.detail 一樣:欄位會隨功能增減,而且從不單獨查詢。
  settings JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 既有資料庫沒有這一欄,建表語句對它們不生效。
ALTER TABLE farms ADD COLUMN IF NOT EXISTS settings JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS users (
  id SERIAL PRIMARY KEY,
  username TEXT,
  password_hash TEXT,
  is_guest BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- farm_id 與 role 是 v2 才加的。介面上先做單人(不建邀請功能),但欄位
-- 現在就要有 —— 等資料進去了再改就是一次資料遷移。
-- role: owner 看得到全部;worker 只能記錄,看不到月報、值得檢視、設定。
ALTER TABLE users ADD COLUMN IF NOT EXISTS farm_id INTEGER REFERENCES farms(id);
ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'owner';

-- username 只在正式帳號上要求唯一。訪客的 username 是 NULL,
-- 而 SQL 的 UNIQUE 允許多個 NULL,但部分索引講得更明白也更安全。
CREATE UNIQUE INDEX IF NOT EXISTS users_username_unique
  ON users (username) WHERE username IS NOT NULL;

-- token 欄位存的是 sha256 雜湊,不是原始 token(見 auth.py 的 hash_token)。
-- 資料庫外洩時,裡面的值不能直接拿來冒用身分。
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS health_checks (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  values JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS health_checks_user_idx ON health_checks (user_id, created_at DESC);

-- 健檢紀錄在 v2 改成屬於牧場而非個人 —— 同場的人要看得到同一份健檢。
-- 舊資料的 farm_id 由 _backfill_farms() 補上(見下方)。
ALTER TABLE health_checks ADD COLUMN IF NOT EXISTS farm_id INTEGER REFERENCES farms(id);
CREATE INDEX IF NOT EXISTS health_checks_farm_idx ON health_checks (farm_id, created_at DESC);

-- ── v2:母豬場管理 ──

CREATE TABLE IF NOT EXISTS pens (
  id SERIAL PRIMARY KEY,
  farm_id INTEGER NOT NULL REFERENCES farms(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pens_farm_idx ON pens (farm_id, name);

-- 耳號不是唯一鍵:離群時會加上民國年後綴(2580 → 2580-D115),裸號釋放
-- 給新豬(見 specs/v2-facts.md 第 6 條)。因此唯一鍵要帶進場日期,
-- 才擋得住「同一個裸號同時有兩頭在場」。
CREATE TABLE IF NOT EXISTS sows (
  id SERIAL PRIMARY KEY,
  farm_id INTEGER NOT NULL REFERENCES farms(id) ON DELETE CASCADE,
  ear_tag TEXT NOT NULL,
  entry_date DATE,
  birth_date DATE,
  breed TEXT NOT NULL DEFAULT '',
  sire_tag TEXT NOT NULL DEFAULT '',
  dam_tag TEXT NOT NULL DEFAULT '',
  parity INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  pen_id INTEGER REFERENCES pens(id) ON DELETE SET NULL,
  photo_url TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS sows_tag_unique ON sows (farm_id, ear_tag, entry_date);
CREATE INDEX IF NOT EXISTS sows_farm_idx ON sows (farm_id, status);

CREATE TABLE IF NOT EXISTS boars (
  id SERIAL PRIMARY KEY,
  farm_id INTEGER NOT NULL REFERENCES farms(id) ON DELETE CASCADE,
  ear_tag TEXT NOT NULL,
  entry_date DATE,
  breed TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS boars_tag_unique ON boars (farm_id, ear_tag, entry_date);

-- detail 用 JSONB:八種事件各自需要的欄位差很多(分娩要活產/死產/木乃伊,
-- 仔豬損失要數量/原因,驗孕只要 +/-),但都只是「這次事件的附註」,
-- 不會被單獨查詢 —— 與 health_checks.values 同一個判斷。
--
-- excluded:匯入時偵測到的離群值,由使用者決定不納入統計。刻意不刪資料 ——
-- 日後可以改回來,母豬卡的時間軸也仍看得到那筆事件。
CREATE TABLE IF NOT EXISTS sow_events (
  id SERIAL PRIMARY KEY,
  farm_id INTEGER NOT NULL REFERENCES farms(id) ON DELETE CASCADE,
  sow_id INTEGER NOT NULL REFERENCES sows(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  event_date DATE NOT NULL,
  detail JSONB NOT NULL DEFAULT '{}',
  -- 同一頭豬、同一天、同樣內容的第幾筆。來源檔案裡合法地存在一模一樣的
  -- 連續兩行:同一天死了兩隻仔豬、死因相同,就是各記一筆。
  seq INTEGER NOT NULL DEFAULT 0,
  recorded_by INTEGER REFERENCES users(id),
  excluded BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sow_events_farm_idx ON sow_events (farm_id, event_date DESC);
CREATE INDEX IF NOT EXISTS sow_events_sow_idx ON sow_events (sow_id, event_date);
-- 匯入冪等。**detail 必須納入唯一鍵**:同一頭豬同一天本來就可能有多筆
-- 同類事件,而且內容不同 ——
--   仔豬損失:同一天死兩隻,一隻「母豬壓死」一隻「虛弱」(實測 186 組)
--   配種:同一天用兩頭公豬是真的雙重配種(實測 101 組)
-- 只用 (sow_id, event_type, event_date) 會把這些當成重複而合併掉,
-- 實測會靜默吃掉 358 筆真實記錄。
CREATE UNIQUE INDEX IF NOT EXISTS sow_events_dedupe
  ON sow_events (sow_id, event_type, event_date, detail, seq);

CREATE TABLE IF NOT EXISTS boar_events (
  id SERIAL PRIMARY KEY,
  farm_id INTEGER NOT NULL REFERENCES farms(id) ON DELETE CASCADE,
  boar_id INTEGER NOT NULL REFERENCES boars(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  event_date DATE NOT NULL,
  detail JSONB NOT NULL DEFAULT '{}',
  recorded_by INTEGER REFERENCES users(id),
  excluded BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS boar_events_farm_idx ON boar_events (farm_id, event_date DESC);

CREATE TABLE IF NOT EXISTS custom_tasks (
  id SERIAL PRIMARY KEY,
  farm_id INTEGER NOT NULL REFERENCES farms(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  start_date DATE NOT NULL,
  repeat_rule TEXT NOT NULL DEFAULT 'once',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS custom_tasks_farm_idx ON custom_tasks (farm_id, start_date);

-- 完成紀錄獨立一張表,不是在 custom_tasks 上放 done 布林值 ——
-- 重複性工作每一次發生都要能各自標記(這週消毒了、上週沒有),
-- 一個布林值只記得住最後一次。
CREATE TABLE IF NOT EXISTS custom_task_done (
  id SERIAL PRIMARY KEY,
  farm_id INTEGER NOT NULL REFERENCES farms(id) ON DELETE CASCADE,
  task_id INTEGER NOT NULL REFERENCES custom_tasks(id) ON DELETE CASCADE,
  due_date DATE NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS custom_task_done_unique
  ON custom_task_done (task_id, due_date);
"""


def _detail_key(detail) -> str:
    """把 detail 轉成可比較的鍵。同一天的兩筆同類事件若內容不同,
    就是兩件不同的事(例如兩隻仔豬死因不同),不可以合併。
    """
    return json.dumps(detail or {}, sort_keys=True, ensure_ascii=False)


def new_token() -> str:
    """session token。secrets 而非 random —— 後者可被預測,等於任何人
    都能算出別人的 token 直接冒用身分。
    """
    return secrets.token_urlsafe(32)


class Store:
    """資料存取介面。實作見 InMemoryStore / PostgresStore。"""

    # --- 使用者 ---
    def create_user(self, username, password_hash, is_guest=False) -> int:
        raise NotImplementedError

    def get_user_by_id(self, user_id: int) -> Optional[dict]:
        raise NotImplementedError

    def get_user_by_username(self, username: str) -> Optional[dict]:
        raise NotImplementedError

    def promote_guest(self, user_id: int, username: str, password_hash: str) -> bool:
        """訪客升級為正式帳號。user_id 不變,既有資料因此自動延續。

        回傳 False 代表該使用者已經不是訪客(不該被覆寫掉密碼)。
        """
        raise NotImplementedError

    # --- session ---
    def create_session(self, token_hash: str, user_id: int, expires_at: datetime) -> None:
        raise NotImplementedError

    def get_session_user_id(self, token_hash: str, now: datetime) -> Optional[int]:
        raise NotImplementedError

    def delete_session(self, token_hash: str) -> None:
        raise NotImplementedError

    # --- 健檢紀錄 ---
    def add_health_check(self, user_id: int, values: dict) -> int:
        raise NotImplementedError

    def list_health_checks(self, user_id: int) -> List[dict]:
        raise NotImplementedError

    def delete_health_check(self, user_id: int, check_id: int) -> bool:
        raise NotImplementedError

    # --- 藥品庫 ---
    def add_drug(self, user_id, name, dosage_note="", withdrawal_days=None,
                 active_ingredient="") -> int:
        raise NotImplementedError

    def list_drugs(self, user_id: int) -> List[dict]:
        raise NotImplementedError

    def delete_drug(self, user_id: int, drug_id: int) -> bool:
        raise NotImplementedError

    # ── v2:以下全部以 farm_id 隔離(憲法第十一條) ──
    #
    # 每一個方法都必須收 farm_id 並寫進 WHERE。只靠「前端只會傳自己的 id」
    # 不算數 —— 使用者可以直接呼叫 API 換一個 id。

    # --- 牧場 ---
    def create_farm(self, name: str) -> int:
        raise NotImplementedError

    def get_farm(self, farm_id: int) -> Optional[dict]:
        raise NotImplementedError

    def set_user_farm(self, user_id: int, farm_id: int, role: str = "owner") -> None:
        raise NotImplementedError

    # --- 牧場設定 ---
    #
    # 只存**與預設值不同**的項目。整份存下來的話,日後調整預設值(例如
    # 量到更好的中位數)不會生效在任何既有牧場 —— 他們都被凍結在舊值,
    # 而且沒有人會知道。
    def get_farm_settings(self, farm_id: int) -> dict:
        raise NotImplementedError

    def set_farm_settings(self, farm_id: int, settings: dict) -> None:
        raise NotImplementedError

    # --- 產房欄位 ---
    def add_pen(self, farm_id: int, name: str) -> int:
        raise NotImplementedError

    def list_pens(self, farm_id: int) -> List[dict]:
        raise NotImplementedError

    def delete_pen(self, farm_id: int, pen_id: int) -> bool:
        raise NotImplementedError

    # --- 母豬 ---
    def add_sow(self, farm_id, ear_tag, entry_date=None, birth_date=None,
                breed="", sire_tag="", dam_tag="", parity=0) -> int:
        raise NotImplementedError

    def list_sows(self, farm_id: int, status: Optional[str] = None) -> List[dict]:
        raise NotImplementedError

    def get_sow(self, farm_id: int, sow_id: int) -> Optional[dict]:
        raise NotImplementedError

    def find_sow_by_tag(self, farm_id: int, ear_tag: str) -> Optional[dict]:
        """依耳號找**在場**的母豬。離群的豬耳號會加年份後綴,不會撞號。"""
        raise NotImplementedError

    def update_sow(self, farm_id: int, sow_id: int, **fields) -> bool:
        raise NotImplementedError

    def delete_sow(self, farm_id: int, sow_id: int) -> bool:
        raise NotImplementedError

    # --- 公豬 ---
    def add_boar(self, farm_id, ear_tag, entry_date=None, breed="") -> int:
        raise NotImplementedError

    def list_boars(self, farm_id: int) -> List[dict]:
        raise NotImplementedError

    def find_boar_by_tag(self, farm_id: int, ear_tag: str) -> Optional[dict]:
        raise NotImplementedError

    # --- 母豬事件 ---
    def add_sow_event(self, farm_id, sow_id, event_type, event_date,
                      detail=None, recorded_by=None, seq=0) -> int:
        raise NotImplementedError

    def list_sow_events(self, farm_id: int, sow_id: Optional[int] = None,
                        since=None, until=None) -> List[dict]:
        raise NotImplementedError

    def delete_sow_event(self, farm_id: int, event_id: int) -> bool:
        raise NotImplementedError

    def set_event_excluded(self, farm_id: int, event_id: int, excluded: bool) -> bool:
        """把離群的事件標記為不納入統計。**刻意不刪資料** —— 日後可以改回來,
        母豬卡的時間軸也仍看得到那筆事件。
        """
        raise NotImplementedError

    # --- 自訂工作 ---
    def add_custom_task(self, farm_id, name, start_date, repeat_rule="once") -> int:
        raise NotImplementedError

    def list_custom_tasks(self, farm_id: int) -> List[dict]:
        raise NotImplementedError

    def delete_custom_task(self, farm_id: int, task_id: int) -> bool:
        raise NotImplementedError

    def mark_task_done(self, farm_id: int, task_id: int, due_date) -> bool:
        raise NotImplementedError

    def unmark_task_done(self, farm_id: int, task_id: int, due_date) -> bool:
        raise NotImplementedError

    def list_task_done(self, farm_id: int, since, until) -> List[dict]:
        raise NotImplementedError


class InMemoryStore(Store):
    """測試用。行為必須跟 PostgresStore 一致,尤其是 user_id 隔離 ——
    如果這裡漏掉隔離,測試會通過但正式環境會外洩,那比沒有測試更糟。
    """

    def __init__(self):
        self.users = {}
        self.sessions = {}
        self.health_checks = []
        self.drugs = []
        self.farms = {}
        self.farm_settings = {}
        self.pens = []
        self.sows = []
        self.boars = []
        self.sow_events = []
        self.boar_events = []
        self.custom_tasks = []
        self.task_done = []
        self._next_user_id = 1
        self._next_check_id = 1
        self._next_drug_id = 1
        self._next = collections.Counter()
        # (sow_id, event_type, event_date) → event_id。對應 PostgresStore 的
        # sow_events_dedupe 唯一索引 —— 沒有它,匯入時的判重是 O(n²)。
        self._event_key = {}

    def _new_id(self, kind: str) -> int:
        self._next[kind] += 1
        return self._next[kind]

    @staticmethod
    def _owned(rows, farm_id, **match):
        """farm_id 一律要比對 —— 這是 InMemoryStore 唯一不能寫錯的地方。
        少一個條件的話測試會通過而正式環境外洩,比沒有測試更糟。
        """
        out = []
        for r in rows:
            if r["farm_id"] != farm_id:
                continue
            if all(r.get(k) == v for k, v in match.items()):
                out.append(r)
        return out

    def create_user(self, username, password_hash, is_guest=False) -> int:
        if username is not None and self.get_user_by_username(username):
            raise ValueError("username 已存在")
        user_id = self._next_user_id
        self._next_user_id += 1
        self.users[user_id] = {
            "id": user_id,
            "username": username,
            "password_hash": password_hash,
            "is_guest": is_guest,
            "farm_id": None,
            "role": "owner",
        }
        return user_id

    def get_user_by_id(self, user_id):
        user = self.users.get(user_id)
        return dict(user) if user else None

    def get_user_by_username(self, username):
        # username 為 None 時必須查無此人,不能比對到訪客(他們的
        # username 就是 None)。SQL 的 `WHERE username = NULL` 天生
        # 永不成立,但 Python 的 None == None 為真 —— 兩邊行為不一致,
        # 會讓測試過關而正式環境出事(或反過來)。
        if username is None:
            return None
        for user in self.users.values():
            if user["username"] == username:
                return dict(user)
        return None

    def promote_guest(self, user_id, username, password_hash) -> bool:
        user = self.users.get(user_id)
        if not user or not user["is_guest"]:
            return False
        if self.get_user_by_username(username):
            raise ValueError("username 已存在")
        user.update(username=username, password_hash=password_hash, is_guest=False)
        return True

    def create_session(self, token_hash, user_id, expires_at):
        self.sessions[token_hash] = {"user_id": user_id, "expires_at": expires_at}

    def get_session_user_id(self, token_hash, now):
        session = self.sessions.get(token_hash)
        if not session or session["expires_at"] <= now:
            return None
        return session["user_id"]

    def delete_session(self, token_hash):
        self.sessions.pop(token_hash, None)

    def add_health_check(self, user_id, values) -> int:
        check_id = self._next_check_id
        self._next_check_id += 1
        self.health_checks.append({
            "id": check_id,
            "user_id": user_id,
            "values": dict(values),
            "created_at": datetime.now(timezone.utc),
        })
        self._trim_health_checks(user_id)
        return check_id

    def _trim_health_checks(self, user_id):
        """超過上限就刪掉最舊的。免費方案的資料庫容量有限,不能讓
        單一使用者無限寫入。
        """
        owned = [c for c in self.health_checks if c["user_id"] == user_id]
        excess = len(owned) - config.MAX_HEALTH_CHECKS_PER_USER
        if excess <= 0:
            return
        doomed = {id(c) for c in owned[:excess]}
        self.health_checks = [c for c in self.health_checks if id(c) not in doomed]

    def list_health_checks(self, user_id):
        owned = [c for c in self.health_checks if c["user_id"] == user_id]
        return [dict(c) for c in reversed(owned)]   # 新的在前

    def delete_health_check(self, user_id, check_id) -> bool:
        before = len(self.health_checks)
        self.health_checks = [
            c for c in self.health_checks
            if not (c["id"] == check_id and c["user_id"] == user_id)
        ]
        return len(self.health_checks) < before

    def add_drug(self, user_id, name, dosage_note="", withdrawal_days=None,
                 active_ingredient="") -> int:
        drug_id = self._next_drug_id
        self._next_drug_id += 1
        self.drugs.append({
            "id": drug_id,
            "user_id": user_id,
            "name": name,
            "active_ingredient": active_ingredient,
            "dosage_note": dosage_note,
            "withdrawal_days": withdrawal_days,
        })
        return drug_id

    def list_drugs(self, user_id):
        return [dict(d) for d in self.drugs if d["user_id"] == user_id]

    def delete_drug(self, user_id, drug_id) -> bool:
        before = len(self.drugs)
        self.drugs = [
            d for d in self.drugs
            if not (d["id"] == drug_id and d["user_id"] == user_id)
        ]
        return len(self.drugs) < before

    # ── v2 ──

    def create_farm(self, name) -> int:
        farm_id = self._new_id("farm")
        self.farms[farm_id] = {"id": farm_id, "name": name}
        return farm_id

    def get_farm(self, farm_id):
        f = self.farms.get(farm_id)
        return dict(f) if f else None

    def set_user_farm(self, user_id, farm_id, role="owner") -> None:
        user = self.users.get(user_id)
        if user:
            user["farm_id"] = farm_id
            user["role"] = role

    def get_farm_settings(self, farm_id) -> dict:
        # 回複本:呼叫端改了回傳值不該影響儲存的內容
        # (PostgresStore 每次都從 JSONB 重建,行為必須一致)
        return dict(self.farm_settings.get(farm_id, {}))

    def set_farm_settings(self, farm_id, settings) -> None:
        self.farm_settings[farm_id] = dict(settings)

    def add_pen(self, farm_id, name) -> int:
        pen_id = self._new_id("pen")
        self.pens.append({"id": pen_id, "farm_id": farm_id, "name": name})
        return pen_id

    def list_pens(self, farm_id):
        return [dict(p) for p in self._owned(self.pens, farm_id)]

    def delete_pen(self, farm_id, pen_id) -> bool:
        before = len(self.pens)
        self.pens = [p for p in self.pens
                     if not (p["id"] == pen_id and p["farm_id"] == farm_id)]
        if len(self.pens) < before:
            for s in self.sows:                      # 比照 ON DELETE SET NULL
                if s.get("pen_id") == pen_id:
                    s["pen_id"] = None
            return True
        return False

    def add_sow(self, farm_id, ear_tag, entry_date=None, birth_date=None,
                breed="", sire_tag="", dam_tag="", parity=0) -> int:
        dup = [s for s in self._owned(self.sows, farm_id, ear_tag=ear_tag)
               if s["entry_date"] == entry_date]
        if dup:
            raise ValueError(f"耳號 {ear_tag} 與進場日期 {entry_date} 重複")
        sow_id = self._new_id("sow")
        self.sows.append({
            "id": sow_id, "farm_id": farm_id, "ear_tag": ear_tag,
            "entry_date": entry_date, "birth_date": birth_date, "breed": breed,
            "sire_tag": sire_tag, "dam_tag": dam_tag, "parity": parity,
            "status": "active", "pen_id": None, "photo_url": "",
        })
        return sow_id

    def list_sows(self, farm_id, status=None):
        rows = self._owned(self.sows, farm_id)
        if status is not None:
            rows = [r for r in rows if r["status"] == status]
        return [dict(r) for r in rows]

    def get_sow(self, farm_id, sow_id):
        rows = self._owned(self.sows, farm_id, id=sow_id)
        return dict(rows[0]) if rows else None

    def find_sow_by_tag(self, farm_id, ear_tag):
        rows = [s for s in self._owned(self.sows, farm_id, ear_tag=ear_tag)
                if s["status"] == "active"]
        return dict(rows[0]) if rows else None

    def update_sow(self, farm_id, sow_id, **fields) -> bool:
        rows = self._owned(self.sows, farm_id, id=sow_id)
        if not rows:
            return False
        rows[0].update(fields)
        return True

    def delete_sow(self, farm_id, sow_id) -> bool:
        before = len(self.sows)
        self.sows = [s for s in self.sows
                     if not (s["id"] == sow_id and s["farm_id"] == farm_id)]
        if len(self.sows) < before:
            self.sow_events = [e for e in self.sow_events if e["sow_id"] != sow_id]
            self._forget_event_keys()
            return True
        return False

    def add_boar(self, farm_id, ear_tag, entry_date=None, breed="") -> int:
        boar_id = self._new_id("boar")
        self.boars.append({
            "id": boar_id, "farm_id": farm_id, "ear_tag": ear_tag,
            "entry_date": entry_date, "breed": breed, "status": "active",
        })
        return boar_id

    def list_boars(self, farm_id):
        return [dict(b) for b in self._owned(self.boars, farm_id)]

    def find_boar_by_tag(self, farm_id, ear_tag):
        rows = self._owned(self.boars, farm_id, ear_tag=ear_tag)
        return dict(rows[0]) if rows else None

    def add_sow_event(self, farm_id, sow_id, event_type, event_date,
                      detail=None, recorded_by=None, seq=0) -> int:
        # 用索引而非掃全表。PostgresStore 靠 sow_events_dedupe 唯一索引,
        # 這裡要對應 —— 掃全表在匯入三萬筆時是 O(n²),實測會慢到幾十秒。
        # detail 與 seq 都要納入 key,理由見 SCHEMA 的註解。
        key = (sow_id, event_type, event_date, _detail_key(detail), seq)
        if key in self._event_key:
            return self._event_key[key]  # 冪等:匯入重跑不會產生重複事件
        event_id = self._new_id("sow_event")
        self._event_key[key] = event_id
        self.sow_events.append({
            "id": event_id, "farm_id": farm_id, "sow_id": sow_id,
            "event_type": event_type, "event_date": event_date,
            "detail": dict(detail or {}), "seq": seq,
            "recorded_by": recorded_by, "excluded": False,
        })
        return event_id

    def list_sow_events(self, farm_id, sow_id=None, since=None, until=None):
        rows = self._owned(self.sow_events, farm_id)
        if sow_id is not None:
            rows = [r for r in rows if r["sow_id"] == sow_id]
        if since is not None:
            rows = [r for r in rows if r["event_date"] >= since]
        if until is not None:
            rows = [r for r in rows if r["event_date"] <= until]
        rows.sort(key=lambda r: (r["event_date"], r["id"]))
        return [dict(r) for r in rows]

    def delete_sow_event(self, farm_id, event_id) -> bool:
        before = len(self.sow_events)
        self.sow_events = [e for e in self.sow_events
                           if not (e["id"] == event_id and e["farm_id"] == farm_id)]
        if len(self.sow_events) < before:
            self._forget_event_keys()
            return True
        return False

    def _forget_event_keys(self) -> None:
        """刪除事件後重建判重索引。

        少了這一步,刪掉的事件仍留在索引裡,之後重新新增同一筆會拿回一個
        已經不存在的 id —— 呼叫端拿它去 set_event_excluded 會靜默失敗。
        """
        self._event_key = {
            (e["sow_id"], e["event_type"], e["event_date"],
             _detail_key(e["detail"]), e.get("seq", 0)): e["id"]
            for e in self.sow_events
        }

    def set_event_excluded(self, farm_id, event_id, excluded) -> bool:
        rows = self._owned(self.sow_events, farm_id, id=event_id)
        if not rows:
            return False
        rows[0]["excluded"] = bool(excluded)
        return True

    def add_custom_task(self, farm_id, name, start_date, repeat_rule="once") -> int:
        task_id = self._new_id("task")
        self.custom_tasks.append({
            "id": task_id, "farm_id": farm_id, "name": name,
            "start_date": start_date, "repeat_rule": repeat_rule,
        })
        return task_id

    def list_custom_tasks(self, farm_id):
        return [dict(t) for t in self._owned(self.custom_tasks, farm_id)]

    def delete_custom_task(self, farm_id, task_id) -> bool:
        before = len(self.custom_tasks)
        self.custom_tasks = [t for t in self.custom_tasks
                             if not (t["id"] == task_id and t["farm_id"] == farm_id)]
        if len(self.custom_tasks) < before:
            self.task_done = [d for d in self.task_done if d["task_id"] != task_id]
            return True
        return False

    def mark_task_done(self, farm_id, task_id, due_date) -> bool:
        if not self._owned(self.custom_tasks, farm_id, id=task_id):
            return False
        if any(d["task_id"] == task_id and d["due_date"] == due_date
               for d in self.task_done):
            return True                   # 冪等
        self.task_done.append({
            "id": self._new_id("done"), "farm_id": farm_id,
            "task_id": task_id, "due_date": due_date,
        })
        return True

    def unmark_task_done(self, farm_id, task_id, due_date) -> bool:
        before = len(self.task_done)
        self.task_done = [
            d for d in self.task_done
            if not (d["task_id"] == task_id and d["due_date"] == due_date
                    and d["farm_id"] == farm_id)
        ]
        return len(self.task_done) < before

    def list_task_done(self, farm_id, since, until):
        rows = [d for d in self._owned(self.task_done, farm_id)
                if since <= d["due_date"] <= until]
        return [dict(d) for d in rows]


class PostgresStore(Store):
    """正式環境。SQL 一律用參數化查詢(%s),不做字串拼接 —— 拼接就是
    SQL injection 的入口,而使用者名稱正是使用者可控的輸入。
    """

    def __init__(self, dsn: str):
        if psycopg is None:
            raise RuntimeError("需要 psycopg 才能使用 PostgresStore,請安裝 requirements.txt")
        self.dsn = dsn

    def _connect(self):
        return psycopg.connect(self.dsn)

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(SCHEMA)
            self._backfill_farms(conn)

    @staticmethod
    def _backfill_farms(conn) -> int:
        """替 v1 留下的使用者補上牧場,並把他們的健檢紀錄掛過去。

        正式站已經有真實資料,而 v2 把隔離單位從使用者改成牧場 —— 舊資料
        沒有 farm_id,不補的話那些人一登入就看不到自己的健檢紀錄。

        只處理 farm_id IS NULL 的列,因此可以重複執行(ensure_schema 每次
        啟動都會跑)。一人一場,日後要合併成同一場再由使用者自己決定。
        """
        rows = conn.execute(
            "SELECT id, username FROM users WHERE farm_id IS NULL"
        ).fetchall()
        for user_id, username in rows:
            name = f"{username} 的牧場" if username else "我的牧場"
            farm_id = conn.execute(
                "INSERT INTO farms (name) VALUES (%s) RETURNING id", (name,)
            ).fetchone()[0]
            conn.execute("UPDATE users SET farm_id = %s WHERE id = %s",
                         (farm_id, user_id))
            conn.execute(
                "UPDATE health_checks SET farm_id = %s"
                " WHERE user_id = %s AND farm_id IS NULL",
                (farm_id, user_id))
        return len(rows)

    def create_user(self, username, password_hash, is_guest=False) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO users (username, password_hash, is_guest)"
                " VALUES (%s, %s, %s) RETURNING id",
                (username, password_hash, is_guest),
            ).fetchone()
            return row[0]

    def get_user_by_id(self, user_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, is_guest FROM users WHERE id = %s",
                (user_id,),
            ).fetchone()
        return self._user_row(row)

    def get_user_by_username(self, username):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, is_guest FROM users WHERE username = %s",
                (username,),
            ).fetchone()
        return self._user_row(row)

    @staticmethod
    def _user_row(row):
        if not row:
            return None
        return {"id": row[0], "username": row[1], "password_hash": row[2], "is_guest": row[3]}

    def promote_guest(self, user_id, username, password_hash) -> bool:
        # WHERE is_guest 這個條件同時擋掉兩件事:重複升級,以及拿別人的
        # 正式帳號 id 來覆寫密碼。少了它就是一個帳號接管漏洞。
        with self._connect() as conn:
            row = conn.execute(
                "UPDATE users SET username = %s, password_hash = %s, is_guest = false"
                " WHERE id = %s AND is_guest = true RETURNING id",
                (username, password_hash, user_id),
            ).fetchone()
            return row is not None

    def create_session(self, token_hash, user_id, expires_at):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (token, user_id, expires_at) VALUES (%s, %s, %s)",
                (token_hash, user_id, expires_at),
            )

    def get_session_user_id(self, token_hash, now):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id FROM sessions WHERE token = %s AND expires_at > %s",
                (token_hash, now),
            ).fetchone()
        return row[0] if row else None

    def delete_session(self, token_hash):
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token = %s", (token_hash,))

    def add_health_check(self, user_id, values) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO health_checks (user_id, values) VALUES (%s, %s) RETURNING id",
                (user_id, Jsonb(values)),
            ).fetchone()
            # 超過上限刪最舊的。用 IN (SELECT ... OFFSET) 一次做完,
            # 不必先撈回應用層再逐筆刪。
            conn.execute(
                "DELETE FROM health_checks WHERE id IN ("
                "  SELECT id FROM health_checks WHERE user_id = %s"
                "  ORDER BY created_at DESC, id DESC OFFSET %s"
                ")",
                (user_id, config.MAX_HEALTH_CHECKS_PER_USER),
            )
            return row[0]

    def list_health_checks(self, user_id):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, values, created_at FROM health_checks"
                " WHERE user_id = %s ORDER BY created_at DESC, id DESC",
                (user_id,),
            ).fetchall()
        return [
            {
                "id": r[0],
                "user_id": user_id,
                "values": r[1] if isinstance(r[1], dict) else json.loads(r[1]),
                "created_at": r[2],
            }
            for r in rows
        ]

    def delete_health_check(self, user_id, check_id) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "DELETE FROM health_checks WHERE id = %s AND user_id = %s RETURNING id",
                (check_id, user_id),
            ).fetchone()
            return row is not None

    def add_drug(self, user_id, name, dosage_note="", withdrawal_days=None,
                 active_ingredient="") -> int:
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO my_drugs"
                " (user_id, name, dosage_note, withdrawal_days, active_ingredient)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (user_id, name, dosage_note, withdrawal_days, active_ingredient),
            ).fetchone()
            return row[0]

    def list_drugs(self, user_id):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, dosage_note, withdrawal_days, active_ingredient"
                " FROM my_drugs WHERE user_id = %s ORDER BY created_at, id",
                (user_id,),
            ).fetchall()
        return [
            {"id": r[0], "user_id": user_id, "name": r[1],
             "dosage_note": r[2], "withdrawal_days": r[3],
             "active_ingredient": r[4]}
            for r in rows
        ]

    def delete_drug(self, user_id, drug_id) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "DELETE FROM my_drugs WHERE id = %s AND user_id = %s RETURNING id",
                (drug_id, user_id),
            ).fetchone()
            return row is not None

    # ── v2 ──
    #
    # 每一句 WHERE 都帶 farm_id。少一個條件就是一個讓 A 牧場讀到或刪掉
    # B 牧場資料的漏洞(憲法第十一條),沒有例外。

    SOW_COLS = ("id, farm_id, ear_tag, entry_date, birth_date, breed, sire_tag,"
                " dam_tag, parity, status, pen_id, photo_url")
    EVENT_COLS = ("id, farm_id, sow_id, event_type, event_date, detail,"
                  " seq, recorded_by, excluded")

    @staticmethod
    def _rows(cur, cols):
        names = [c.strip() for c in cols.split(",")]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def create_farm(self, name) -> int:
        with self._connect() as conn:
            return conn.execute("INSERT INTO farms (name) VALUES (%s) RETURNING id",
                                (name,)).fetchone()[0]

    def get_farm(self, farm_id):
        with self._connect() as conn:
            row = conn.execute("SELECT id, name FROM farms WHERE id = %s",
                               (farm_id,)).fetchone()
        return {"id": row[0], "name": row[1]} if row else None

    def get_farm_settings(self, farm_id) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT settings FROM farms WHERE id = %s",
                               (farm_id,)).fetchone()
        return dict(row[0]) if row and row[0] else {}

    def set_farm_settings(self, farm_id, settings) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE farms SET settings = %s WHERE id = %s",
                         (Jsonb(dict(settings)), farm_id))

    def set_user_farm(self, user_id, farm_id, role="owner") -> None:
        with self._connect() as conn:
            conn.execute("UPDATE users SET farm_id = %s, role = %s WHERE id = %s",
                         (farm_id, role, user_id))

    def add_pen(self, farm_id, name) -> int:
        with self._connect() as conn:
            return conn.execute(
                "INSERT INTO pens (farm_id, name) VALUES (%s, %s) RETURNING id",
                (farm_id, name)).fetchone()[0]

    def list_pens(self, farm_id):
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT id, farm_id, name FROM pens WHERE farm_id = %s ORDER BY name",
                (farm_id,))
            return self._rows(cur, "id, farm_id, name")

    def delete_pen(self, farm_id, pen_id) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "DELETE FROM pens WHERE id = %s AND farm_id = %s RETURNING id",
                (pen_id, farm_id)).fetchone()
            return row is not None

    def add_sow(self, farm_id, ear_tag, entry_date=None, birth_date=None,
                breed="", sire_tag="", dam_tag="", parity=0) -> int:
        with self._connect() as conn:
            return conn.execute(
                "INSERT INTO sows (farm_id, ear_tag, entry_date, birth_date, breed,"
                " sire_tag, dam_tag, parity) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
                " RETURNING id",
                (farm_id, ear_tag, entry_date, birth_date, breed,
                 sire_tag, dam_tag, parity)).fetchone()[0]

    def list_sows(self, farm_id, status=None):
        sql = f"SELECT {self.SOW_COLS} FROM sows WHERE farm_id = %s"
        args = [farm_id]
        if status is not None:
            sql += " AND status = %s"
            args.append(status)
        with self._connect() as conn:
            return self._rows(conn.execute(sql + " ORDER BY ear_tag", args),
                              self.SOW_COLS)

    def get_sow(self, farm_id, sow_id):
        with self._connect() as conn:
            rows = self._rows(conn.execute(
                f"SELECT {self.SOW_COLS} FROM sows WHERE id = %s AND farm_id = %s",
                (sow_id, farm_id)), self.SOW_COLS)
        return rows[0] if rows else None

    def find_sow_by_tag(self, farm_id, ear_tag):
        with self._connect() as conn:
            rows = self._rows(conn.execute(
                f"SELECT {self.SOW_COLS} FROM sows"
                " WHERE farm_id = %s AND ear_tag = %s AND status = 'active'",
                (farm_id, ear_tag)), self.SOW_COLS)
        return rows[0] if rows else None

    def update_sow(self, farm_id, sow_id, **fields) -> bool:
        allowed = {"ear_tag", "entry_date", "birth_date", "breed", "sire_tag",
                   "dam_tag", "parity", "status", "pen_id", "photo_url"}
        bad = set(fields) - allowed
        if bad:                       # 欄位名直接進 SQL,必須先過白名單
            raise ValueError(f"不允許更新的欄位:{sorted(bad)}")
        if not fields:
            return False
        sets = ", ".join(f"{k} = %s" for k in fields)
        with self._connect() as conn:
            row = conn.execute(
                f"UPDATE sows SET {sets} WHERE id = %s AND farm_id = %s RETURNING id",
                list(fields.values()) + [sow_id, farm_id]).fetchone()
            return row is not None

    def delete_sow(self, farm_id, sow_id) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "DELETE FROM sows WHERE id = %s AND farm_id = %s RETURNING id",
                (sow_id, farm_id)).fetchone()
            return row is not None

    def add_boar(self, farm_id, ear_tag, entry_date=None, breed="") -> int:
        with self._connect() as conn:
            return conn.execute(
                "INSERT INTO boars (farm_id, ear_tag, entry_date, breed)"
                " VALUES (%s,%s,%s,%s) RETURNING id",
                (farm_id, ear_tag, entry_date, breed)).fetchone()[0]

    BOAR_COLS = "id, farm_id, ear_tag, entry_date, breed, status"

    def list_boars(self, farm_id):
        with self._connect() as conn:
            return self._rows(conn.execute(
                f"SELECT {self.BOAR_COLS} FROM boars WHERE farm_id = %s ORDER BY ear_tag",
                (farm_id,)), self.BOAR_COLS)

    def find_boar_by_tag(self, farm_id, ear_tag):
        with self._connect() as conn:
            rows = self._rows(conn.execute(
                f"SELECT {self.BOAR_COLS} FROM boars WHERE farm_id = %s AND ear_tag = %s",
                (farm_id, ear_tag)), self.BOAR_COLS)
        return rows[0] if rows else None

    def add_sow_event(self, farm_id, sow_id, event_type, event_date,
                      detail=None, recorded_by=None, seq=0) -> int:
        # ON CONFLICT DO UPDATE(而非 DO NOTHING)才拿得回既有的 id,
        # 匯入重跑時呼叫端不必自己查一次。
        with self._connect() as conn:
            return conn.execute(
                "INSERT INTO sow_events (farm_id, sow_id, event_type, event_date,"
                " detail, seq, recorded_by) VALUES (%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (sow_id, event_type, event_date, detail, seq)"
                " DO UPDATE SET recorded_by = EXCLUDED.recorded_by RETURNING id",
                (farm_id, sow_id, event_type, event_date,
                 Jsonb(detail or {}), seq, recorded_by)).fetchone()[0]

    def list_sow_events(self, farm_id, sow_id=None, since=None, until=None):
        sql = f"SELECT {self.EVENT_COLS} FROM sow_events WHERE farm_id = %s"
        args = [farm_id]
        if sow_id is not None:
            sql += " AND sow_id = %s"; args.append(sow_id)
        if since is not None:
            sql += " AND event_date >= %s"; args.append(since)
        if until is not None:
            sql += " AND event_date <= %s"; args.append(until)
        with self._connect() as conn:
            rows = self._rows(conn.execute(sql + " ORDER BY event_date, id", args),
                              self.EVENT_COLS)
        for r in rows:
            if not isinstance(r["detail"], dict):
                r["detail"] = json.loads(r["detail"])
        return rows

    def delete_sow_event(self, farm_id, event_id) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "DELETE FROM sow_events WHERE id = %s AND farm_id = %s RETURNING id",
                (event_id, farm_id)).fetchone()
            return row is not None

    def set_event_excluded(self, farm_id, event_id, excluded) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "UPDATE sow_events SET excluded = %s"
                " WHERE id = %s AND farm_id = %s RETURNING id",
                (bool(excluded), event_id, farm_id)).fetchone()
            return row is not None

    TASK_COLS = "id, farm_id, name, start_date, repeat_rule"

    def add_custom_task(self, farm_id, name, start_date, repeat_rule="once") -> int:
        with self._connect() as conn:
            return conn.execute(
                "INSERT INTO custom_tasks (farm_id, name, start_date, repeat_rule)"
                " VALUES (%s,%s,%s,%s) RETURNING id",
                (farm_id, name, start_date, repeat_rule)).fetchone()[0]

    def list_custom_tasks(self, farm_id):
        with self._connect() as conn:
            return self._rows(conn.execute(
                f"SELECT {self.TASK_COLS} FROM custom_tasks WHERE farm_id = %s"
                " ORDER BY start_date", (farm_id,)), self.TASK_COLS)

    def delete_custom_task(self, farm_id, task_id) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "DELETE FROM custom_tasks WHERE id = %s AND farm_id = %s RETURNING id",
                (task_id, farm_id)).fetchone()
            return row is not None

    def mark_task_done(self, farm_id, task_id, due_date) -> bool:
        with self._connect() as conn:
            owns = conn.execute(
                "SELECT 1 FROM custom_tasks WHERE id = %s AND farm_id = %s",
                (task_id, farm_id)).fetchone()
            if not owns:
                return False
            conn.execute(
                "INSERT INTO custom_task_done (farm_id, task_id, due_date)"
                " VALUES (%s,%s,%s) ON CONFLICT (task_id, due_date) DO NOTHING",
                (farm_id, task_id, due_date))
            return True

    def unmark_task_done(self, farm_id, task_id, due_date) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "DELETE FROM custom_task_done"
                " WHERE task_id = %s AND due_date = %s AND farm_id = %s RETURNING id",
                (task_id, due_date, farm_id)).fetchone()
            return row is not None

    def list_task_done(self, farm_id, since, until):
        cols = "id, farm_id, task_id, due_date"
        with self._connect() as conn:
            return self._rows(conn.execute(
                f"SELECT {cols} FROM custom_task_done"
                " WHERE farm_id = %s AND due_date BETWEEN %s AND %s",
                (farm_id, since, until)), cols)


def select_store() -> Optional[Store]:
    """沒設 DATABASE_URL 就回 None —— 帳號功能整個關閉,網站退回
    「純工具、免帳號」模式。本機開發與 demo 不必為了跑起來而先去
    申請一個資料庫。

    例外是 DEV_MEMORY_DB:本機要驗 v2 的畫面就得有 store,否則工作清單、
    母豬卡、提醒全部停在「載入中…」。config 已經保證它只在沒有
    DATABASE_URL 時成立,不會蓋掉真的資料庫。
    """
    if not config.DATABASE_URL:
        if config.DEV_MEMORY_DB:
            # 印出來,免得有人以為自己連到了真的資料庫。
            # 訊息裡不放符號類字元:Windows 主控台是 cp950,遇到 ⚠ 之類的
            # 字元會直接 UnicodeEncodeError,伺服器連啟動都啟動不了。
            print("[DEV_MEMORY_DB] 資料存在記憶體,伺服器一關就全部消失。")
            return InMemoryStore()
        return None
    store = PostgresStore(config.DATABASE_URL)
    store.ensure_schema()
    return store
