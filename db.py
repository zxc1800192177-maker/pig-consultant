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
import contextlib
import json
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

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

-- 救援碼(忘記密碼用)。後來才加的,所以既有帳號是 NULL ——
-- 「還沒設定」與「設定了」必須分得出來,不能用空字串當預設值。
--
-- 存的是雜湊,不是救援碼本身。理由跟密碼一樣:救援碼可以直接拿來重設
-- 密碼,等於第二把鑰匙,資料庫外洩時不能讓人直接撿去用。
ALTER TABLE users ADD COLUMN IF NOT EXISTS recovery_hash TEXT;

-- email 目前**沒有任何程式在用**。原本要做信箱重設密碼,評估後決定只用
-- 救援碼(寄信要接第三方服務,而且信可能進垃圾桶、農民也不一定會收)。
-- 欄位留著不刪:它已經建在正式資料庫上了,是空的、不影響任何查詢,
-- 而砍掉正式環境的欄位是有風險的動作,換不到任何好處。
ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT;

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

-- 三個區域各自有編號的欄位:配種區(mating)、待產區(gestation)、
-- 產房(farrowing)。母豬依生產週期在三區之間搬動,搬到哪個編號由
-- 牧場主自己選(見 server.py _add_event 對 MV 事件的處理)。
CREATE TABLE IF NOT EXISTS pens (
  id SERIAL PRIMARY KEY,
  farm_id INTEGER NOT NULL REFERENCES farms(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  zone TEXT NOT NULL DEFAULT 'farrowing',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pens_farm_idx ON pens (farm_id, zone, name);

-- 既有資料庫沒有這一欄,建表語句對它們不生效。
ALTER TABLE pens ADD COLUMN IF NOT EXISTS zone TEXT NOT NULL DEFAULT 'farrowing';

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
  sire_tag TEXT NOT NULL DEFAULT '',
  dam_tag TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS boars_tag_unique ON boars (farm_id, ear_tag, entry_date);

-- 既有資料庫沒有這兩欄,建表語句對它們不生效。
ALTER TABLE boars ADD COLUMN IF NOT EXISTS sire_tag TEXT NOT NULL DEFAULT '';
ALTER TABLE boars ADD COLUMN IF NOT EXISTS dam_tag TEXT NOT NULL DEFAULT '';

-- 哪個使用者新增的這筆進場記錄,「收回種豬進場」要靠這個判斷能不能收回
-- (比照 sow_events/boar_events 的 recorded_by)。舊資料沒有這欄,一律是
-- NULL,效果等同「只有牧場主能收回」——這對匯入或早期資料是對的:
-- 那些本來就不該被員工用這個快速收回按鈕動到。
ALTER TABLE sows ADD COLUMN IF NOT EXISTS created_by INTEGER REFERENCES users(id);
ALTER TABLE boars ADD COLUMN IF NOT EXISTS created_by INTEGER REFERENCES users(id);

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

-- 肉豬(育肥豬)死亡:不掛在任何一頭母豬或公豬身上 —— 肉豬本來就不是
-- 這個系統追蹤身分的對象(沒有耳號進場記錄,母豬/公豬表也不是為牠們
-- 設計的),硬塞進 sow_events/boar_events 得先假造一個不存在的動物身分。
-- 獨立一張表,只記使用者要的三件事:什麼時候、為什麼、幾公斤。
CREATE TABLE IF NOT EXISTS market_deaths (
  id SERIAL PRIMARY KEY,
  farm_id INTEGER NOT NULL REFERENCES farms(id) ON DELETE CASCADE,
  event_date DATE NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  weight_kg NUMERIC,
  recorded_by INTEGER REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS market_deaths_farm_idx ON market_deaths (farm_id, event_date DESC);

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

    def batch(self):
        """連續呼叫大量寫入方法時用(目前只有 importer.import_into 用到)。

        PostgresStore 預設每個方法各自連線、各自送出 —— 對單一請求
        來說沒有問題,但匯入一次要寫上萬筆,等於一個請求裡開上萬條
        連線,實測 300 行/198 筆寫入要 17.9 秒,推算整份 3.5 萬行的檔案
        要 50 分鐘,遠遠超過使用者能等的時間(而且大概率會被逾時砍斷,
        留下寫到一半的資料)。`with store.batch():` 讓 PostgresStore
        借同一條連線重複用、最後一次 commit;InMemoryStore 沒有連線
        可省,直接把自己借出去。
        """
        raise NotImplementedError

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

    def set_recovery_hash(self, user_id: int, recovery_hash: Optional[str]) -> None:
        """設定(或清掉)救援碼的雜湊。

        救援碼是**一次性**的:用掉之後一定要換一組新的,否則同一組碼可以
        被重複拿來重設密碼,等於一把永遠有效的備用鑰匙。
        """
        raise NotImplementedError

    def set_password_hash(self, user_id: int, password_hash: str) -> None:
        raise NotImplementedError

    def delete_sessions_for_user(self, user_id: int) -> int:
        """把這個人所有的登入狀態清掉,回傳清掉幾個。

        重設密碼後一定要做(OWASP)—— 會走到重設,常常正是因為擔心帳號
        被別人拿走,只換密碼而讓對方原本那張 cookie 繼續有效等於沒趕人。
        """
        raise NotImplementedError

    def delete_account(self, user_id: int) -> bool:
        """永久刪除一個帳號,連同他獨有的牧場資料。找不到人時回 False。

        **牧場裡還有別人時,只刪這個人,牧場留著。** 牧場是共同財產,
        一位員工離職不該把整場的母豬記錄一起帶走。只有當他是最後一個
        人時,牧場才跟著消失。

        刪除順序是有講究的,不能照直覺來:
          1. 事件的 recorded_by 指向使用者,而且沒有連帶刪除規則,
             不先清成 NULL 的話資料庫會擋下整個刪除。
          2. health_checks 同時指向使用者與牧場,不先刪掉的話會擋住
             牧場那一步。
          3. users.farm_id 指向牧場,所以要先把自己的 farm_id 清掉,
             才刪得動牧場。
        牧場一旦刪掉,底下的欄位/母豬/公豬/事件/自訂工作都是
        ON DELETE CASCADE,會一起消失。
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

    # --- 產房欄位(配種區/待產區/產房三個區域) ---
    def add_pen(self, farm_id: int, name: str, zone: str = "farrowing") -> int:
        raise NotImplementedError

    def list_pens(self, farm_id: int, zone: Optional[str] = None) -> List[dict]:
        raise NotImplementedError

    def delete_pen(self, farm_id: int, pen_id: int) -> bool:
        raise NotImplementedError

    # --- 母豬 ---
    def add_sow(self, farm_id, ear_tag, entry_date=None, birth_date=None,
                breed="", sire_tag="", dam_tag="", parity=0, created_by=None) -> int:
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
    def add_boar(self, farm_id, ear_tag, entry_date=None, breed="",
                 sire_tag="", dam_tag="", created_by=None) -> int:
        raise NotImplementedError

    def list_boars(self, farm_id: int, status: Optional[str] = None) -> List[dict]:
        raise NotImplementedError

    def get_boar(self, farm_id: int, boar_id: int) -> Optional[dict]:
        raise NotImplementedError

    def find_boar_by_tag(self, farm_id: int, ear_tag: str) -> Optional[dict]:
        raise NotImplementedError

    def update_boar(self, farm_id: int, boar_id: int, **fields) -> bool:
        raise NotImplementedError

    def delete_boar(self, farm_id: int, boar_id: int) -> bool:
        raise NotImplementedError

    # --- 公豬事件 ---
    def add_boar_event(self, farm_id, boar_id, event_type, event_date,
                       detail=None, recorded_by=None) -> int:
        raise NotImplementedError

    def list_boar_events(self, farm_id: int, boar_id: Optional[int] = None) -> List[dict]:
        raise NotImplementedError

    def delete_boar_event(self, farm_id: int, event_id: int) -> bool:
        raise NotImplementedError

    # --- 肉豬死亡 ---
    def add_market_death(self, farm_id, event_date, reason="", weight_kg=None,
                         recorded_by=None) -> int:
        raise NotImplementedError

    def list_market_deaths(self, farm_id: int) -> List[dict]:
        raise NotImplementedError

    def delete_market_death(self, farm_id: int, death_id: int) -> bool:
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
        self.market_deaths = []
        self.custom_tasks = []
        self.task_done = []
        self._next_user_id = 1
        self._next_check_id = 1
        self._next_drug_id = 1
        self._next = collections.Counter()
        # (sow_id, event_type, event_date) → event_id。對應 PostgresStore 的
        # sow_events_dedupe 唯一索引 —— 沒有它,匯入時的判重是 O(n²)。
        self._event_key = {}

    @contextlib.contextmanager
    def batch(self):
        """沒有連線可省,直接把自己借出去用(見 Store.batch 的說明)。"""
        yield self

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
            "recovery_hash": None,
            "email": None,
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

    def set_recovery_hash(self, user_id, recovery_hash) -> None:
        user = self.users.get(user_id)
        if user is not None:
            user["recovery_hash"] = recovery_hash

    def set_password_hash(self, user_id, password_hash) -> None:
        user = self.users.get(user_id)
        if user is not None:
            user["password_hash"] = password_hash

    def delete_sessions_for_user(self, user_id) -> int:
        before = len(self.sessions)
        self.sessions = {t: s for t, s in self.sessions.items()
                         if s["user_id"] != user_id}
        return before - len(self.sessions)

    def promote_guest(self, user_id, username, password_hash) -> bool:
        user = self.users.get(user_id)
        if not user or not user["is_guest"]:
            return False
        if self.get_user_by_username(username):
            raise ValueError("username 已存在")
        user.update(username=username, password_hash=password_hash, is_guest=False)
        return True

    def delete_account(self, user_id) -> bool:
        user = self.users.get(user_id)
        if user is None:
            return False
        farm_id = user.get("farm_id")
        alone = farm_id is not None and not any(
            u["id"] != user_id and u.get("farm_id") == farm_id
            for u in self.users.values()
        )

        for events in (self.sow_events, self.boar_events):
            for e in events:
                if e.get("recorded_by") == user_id:
                    e["recorded_by"] = None

        self.health_checks = [h for h in self.health_checks if h["user_id"] != user_id]
        self.drugs = [d for d in self.drugs if d["user_id"] != user_id]
        self.sessions = {t: s for t, s in self.sessions.items()
                         if s["user_id"] != user_id}

        if alone:
            # 比照 ON DELETE CASCADE:牧場沒了,底下的東西都不該留著
            self.pens = [p for p in self.pens if p["farm_id"] != farm_id]
            self.sows = [s for s in self.sows if s["farm_id"] != farm_id]
            self.boars = [b for b in self.boars if b["farm_id"] != farm_id]
            self.sow_events = [e for e in self.sow_events if e["farm_id"] != farm_id]
            self.boar_events = [e for e in self.boar_events if e["farm_id"] != farm_id]
            self.custom_tasks = [t for t in self.custom_tasks if t["farm_id"] != farm_id]
            self.task_done = [d for d in self.task_done if d["farm_id"] != farm_id]
            self.health_checks = [h for h in self.health_checks
                                  if h.get("farm_id") != farm_id]
            self.farms.pop(farm_id, None)
            self.farm_settings.pop(farm_id, None)

        del self.users[user_id]
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

    def add_pen(self, farm_id, name, zone="farrowing") -> int:
        pen_id = self._new_id("pen")
        self.pens.append({"id": pen_id, "farm_id": farm_id, "name": name, "zone": zone})
        return pen_id

    def list_pens(self, farm_id, zone=None):
        rows = self._owned(self.pens, farm_id)
        if zone is not None:
            rows = [p for p in rows if p["zone"] == zone]
        return [dict(p) for p in rows]

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
                breed="", sire_tag="", dam_tag="", parity=0, created_by=None) -> int:
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
            "created_by": created_by,
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

    def add_boar(self, farm_id, ear_tag, entry_date=None, breed="",
                 sire_tag="", dam_tag="", created_by=None) -> int:
        boar_id = self._new_id("boar")
        self.boars.append({
            "id": boar_id, "farm_id": farm_id, "ear_tag": ear_tag,
            "entry_date": entry_date, "breed": breed,
            "sire_tag": sire_tag, "dam_tag": dam_tag, "status": "active",
            "created_by": created_by,
        })
        return boar_id

    def list_boars(self, farm_id, status=None):
        rows = self._owned(self.boars, farm_id)
        if status is not None:
            rows = [r for r in rows if r["status"] == status]
        return [dict(r) for r in rows]

    def get_boar(self, farm_id, boar_id):
        rows = self._owned(self.boars, farm_id, id=boar_id)
        return dict(rows[0]) if rows else None

    def find_boar_by_tag(self, farm_id, ear_tag):
        rows = self._owned(self.boars, farm_id, ear_tag=ear_tag)
        return dict(rows[0]) if rows else None

    def update_boar(self, farm_id, boar_id, **fields) -> bool:
        rows = self._owned(self.boars, farm_id, id=boar_id)
        if not rows:
            return False
        rows[0].update(fields)
        return True

    def delete_boar(self, farm_id, boar_id) -> bool:
        before = len(self.boars)
        self.boars = [b for b in self.boars
                     if not (b["id"] == boar_id and b["farm_id"] == farm_id)]
        if len(self.boars) < before:
            self.boar_events = [e for e in self.boar_events if e["boar_id"] != boar_id]
            return True
        return False

    def add_boar_event(self, farm_id, boar_id, event_type, event_date,
                       detail=None, recorded_by=None) -> int:
        event_id = self._new_id("boar_event")
        self.boar_events.append({
            "id": event_id, "farm_id": farm_id, "boar_id": boar_id,
            "event_type": event_type, "event_date": event_date,
            "detail": dict(detail or {}), "recorded_by": recorded_by, "excluded": False,
        })
        return event_id

    def list_boar_events(self, farm_id, boar_id=None):
        rows = self._owned(self.boar_events, farm_id)
        if boar_id is not None:
            rows = [r for r in rows if r["boar_id"] == boar_id]
        rows.sort(key=lambda r: (r["event_date"], r["id"]))
        return [dict(r) for r in rows]

    def delete_boar_event(self, farm_id, event_id) -> bool:
        before = len(self.boar_events)
        self.boar_events = [e for e in self.boar_events
                            if not (e["id"] == event_id and e["farm_id"] == farm_id)]
        return len(self.boar_events) < before

    def add_market_death(self, farm_id, event_date, reason="", weight_kg=None,
                         recorded_by=None) -> int:
        death_id = self._new_id("market_death")
        self.market_deaths.append({
            "id": death_id, "farm_id": farm_id, "event_date": event_date,
            "reason": reason, "weight_kg": weight_kg, "recorded_by": recorded_by,
        })
        return death_id

    def list_market_deaths(self, farm_id):
        rows = self._owned(self.market_deaths, farm_id)
        rows.sort(key=lambda r: (r["event_date"], r["id"]))
        return [dict(r) for r in rows]

    def delete_market_death(self, farm_id, death_id) -> bool:
        before = len(self.market_deaths)
        self.market_deaths = [d for d in self.market_deaths
                              if not (d["id"] == death_id and d["farm_id"] == farm_id)]
        return len(self.market_deaths) < before

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

    # list_sow_events(farm_id) 不帶 sow_id/since/until 時,是「整個牧場
    # 全部事件」——目前有 8 個呼叫端各自這樣呼叫(工作清單、提醒、值得
    # 檢視、生產月報、已記錄……),而前端登入或整頁重整時會一次平行打出
    # 這些請求。32,000+ 筆的牧場等於同一瞬間疊出好幾份幾乎一樣的資料庫
    # 查詢結果,各自佔一份記憶體——這是免費方案 512MB 被瞬間衝爆、
    # 觸發 status 137 被砍掉的可疑原因(使用者回報後查出來的)。
    #
    # 快取幾秒鐘,讓同一批平行請求共用同一份資料,不必各自查一次資料庫、
    # 各自建一份 Python 物件。TTL 選短(3 秒):要夠讓同一次頁面載入的
    # 平行請求命中,但不能久到讓「記錄完馬上重新整理」看到舊資料——
    # 所以寫入一律主動清快取,不是單純等它過期,3 秒只是防呆用的上限。
    _EVENT_CACHE_TTL = 3.0

    def __init__(self, dsn: str):
        if psycopg is None:
            raise RuntimeError("需要 psycopg 才能使用 PostgresStore,請安裝 requirements.txt")
        self.dsn = dsn
        # 每條處理請求的執行緒各自的「目前批次連線」—— 用 thread-local
        # 而不是一般的實例屬性,因為這個 PostgresStore 是所有請求共用
        # 的同一個物件(見 server.py 的 APP = Application(...))。用一般
        # 屬性的話,兩個請求同時匯入就會互相搶對方的連線,造成資料寫錯
        # 請求或連線被兩個執行緒同時使用(psycopg 的連線不是執行緒安全的)。
        self._local = threading.local()
        # 事件快取刻意是一般屬性不是 thread-local —— 要在所有執行緒之間
        # 共用同一份,平行請求才收得到彼此的快取,不然每個執行緒各自快取
        # 就跟沒快取一樣。用鎖保護,因為 psycopg 連線不是執行緒安全的
        # 這件事不適用在一個 dict + float 上,但仍要避免兩條執行緒同時
        # 讀寫同一個 farm_id 的快取項目而互相踩到。
        self._event_cache: Dict[int, Tuple[float, List[dict]]] = {}
        self._event_cache_lock = threading.Lock()
        # 每個牧場一個版本號,只在 _invalidate 時遞增。查詢開始前先記下
        # 當時的版本號,查完準備寫入快取時比對版本號有沒有變——見
        # _maybe_cache_farm_events 的說明,這是修競態條件用的。
        self._event_cache_version: Dict[int, int] = {}

    def _cached_farm_events(self, farm_id: int) -> Optional[List[dict]]:
        with self._event_cache_lock:
            entry = self._event_cache.get(farm_id)
        if entry is None:
            return None
        cached_at, events = entry
        if time.monotonic() - cached_at >= self._EVENT_CACHE_TTL:
            return None
        return events

    def _farm_events_cache_version(self, farm_id: int) -> int:
        with self._event_cache_lock:
            return self._event_cache_version.get(farm_id, 0)

    def _maybe_cache_farm_events(self, farm_id: int, version_before: int,
                                 rows: List[dict]) -> None:
        # 查詢開始前記下的版本號,如果跟現在不一樣,代表查詢執行期間
        # 有別的請求寫入並清過快取——這份查詢結果其實是舊的(沒包含那筆
        # 新寫入),寫進快取會讓「記錄完馬上重新整理」的人看到舊資料,
        # 而且會維持到 TTL 過期為止。版本不符就乾脆不快取,下一次查詢
        # 自然會拿到新資料再快取一次。
        #
        # 這個洞是真的:使用者回報過「記錄配種後,沒出現在已記錄欄位」,
        # 追出來就是這裡——單純 pop 快取(舊寫法)在「查詢開始時快取剛好
        # 是空的」這個情況下等於沒清,慢查詢事後把舊資料寫回去照樣蓋過。
        with self._event_cache_lock:
            if self._event_cache_version.get(farm_id, 0) == version_before:
                self._event_cache[farm_id] = (time.monotonic(), rows)

    def _invalidate_farm_events_cache(self, farm_id: int) -> None:
        with self._event_cache_lock:
            self._event_cache.pop(farm_id, None)
            self._event_cache_version[farm_id] = (
                self._event_cache_version.get(farm_id, 0) + 1)

    def _connect(self):
        conn = getattr(self._local, "batch_conn", None)
        if conn is not None:
            # 借出去用,不能讓 `with ... as conn:` 提前把它 commit/關掉 ——
            # 那要留給 batch() 結束時做一次。
            return contextlib.nullcontext(conn)
        return psycopg.connect(self.dsn)

    @contextlib.contextmanager
    def batch(self):
        """見 Store.batch 的說明。開一條連線重複借給接下來所有的
        self._connect() 呼叫用,結束時才一次 commit、關閉。

        巢狀呼叫(理論上不會發生,防呆用)沿用最外層那條連線,不重開。
        """
        if getattr(self._local, "batch_conn", None) is not None:
            yield self
            return
        with psycopg.connect(self.dsn) as conn:
            self._local.batch_conn = conn
            try:
                yield self
            finally:
                self._local.batch_conn = None

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

    # 欄位清單只寫一次。分散在各個 SELECT 裡的話,加了欄位卻漏改其中一個
    # 就會讓那條路徑安靜地少拿到資料 —— farm_id 就是這樣漏掉,害每個帳號
    # 都被判成「還沒有對應的牧場」。
    USER_COLS = "id, username, password_hash, is_guest, farm_id, role, recovery_hash, email"

    def get_user_by_id(self, user_id):
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {self.USER_COLS} FROM users WHERE id = %s", (user_id,)
            ).fetchone()
        return self._user_row(row)

    def get_user_by_username(self, username):
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {self.USER_COLS} FROM users WHERE username = %s", (username,)
            ).fetchone()
        return self._user_row(row)

    def set_recovery_hash(self, user_id, recovery_hash) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE users SET recovery_hash = %s WHERE id = %s",
                         (recovery_hash, user_id))

    def set_password_hash(self, user_id, password_hash) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                         (password_hash, user_id))

    def delete_sessions_for_user(self, user_id) -> int:
        with self._connect() as conn:
            rows = conn.execute(
                "DELETE FROM sessions WHERE user_id = %s RETURNING token", (user_id,)
            ).fetchall()
        return len(rows)

    @staticmethod
    def _user_row(row):
        if not row:
            return None
        return {"id": row[0], "username": row[1], "password_hash": row[2], "is_guest": row[3],
                "farm_id": row[4], "role": row[5], "recovery_hash": row[6], "email": row[7]}

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

    def delete_account(self, user_id) -> bool:
        # 整段在同一條連線/交易裡:中途失敗會留下一個刪到一半的帳號
        # (牧場沒了但人還在,或反過來),那比沒刪還糟。
        with self._connect() as conn:
            row = conn.execute("SELECT farm_id FROM users WHERE id = %s",
                               (user_id,)).fetchone()
            if row is None:
                return False
            farm_id = row[0]

            alone = False
            if farm_id is not None:
                alone = conn.execute(
                    "SELECT count(*) FROM users WHERE farm_id = %s AND id <> %s",
                    (farm_id, user_id)).fetchone()[0] == 0

            # recorded_by 沒有連帶刪除規則,留著會擋下刪除使用者那一步。
            # 設成 NULL 而不是刪掉事件 —— 牧場還在的話,那些記錄是全場的
            # 共同財產,不該因為記錄的人離開就消失。
            conn.execute("UPDATE sow_events SET recorded_by = NULL WHERE recorded_by = %s",
                         (user_id,))
            conn.execute("UPDATE boar_events SET recorded_by = NULL WHERE recorded_by = %s",
                         (user_id,))
            conn.execute("DELETE FROM health_checks WHERE user_id = %s", (user_id,))

            if alone:
                # health_checks.farm_id 與 users.farm_id 都會擋住刪牧場,
                # 兩個都要先讓開。
                conn.execute("DELETE FROM health_checks WHERE farm_id = %s", (farm_id,))
                conn.execute("UPDATE users SET farm_id = NULL WHERE id = %s", (user_id,))
                conn.execute("DELETE FROM farms WHERE id = %s", (farm_id,))

            conn.execute("DELETE FROM users WHERE id = %s", (user_id,))

        # 上面動過 sow_events.recorded_by(留人不留名),牧場整個被刪掉時
        # 事件也跟著沒了 —— 兩種情形快取都過期了。放在 with 外面,確定
        # 交易真的提交了才清,免得回滾後反而把有效的快取清掉。
        if farm_id is not None:
            self._invalidate_farm_events_cache(farm_id)
        return True

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
                " dam_tag, parity, status, pen_id, photo_url, created_by")
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

    def add_pen(self, farm_id, name, zone="farrowing") -> int:
        with self._connect() as conn:
            return conn.execute(
                "INSERT INTO pens (farm_id, name, zone) VALUES (%s, %s, %s) RETURNING id",
                (farm_id, name, zone)).fetchone()[0]

    PEN_COLS = "id, farm_id, name, zone"

    def list_pens(self, farm_id, zone=None):
        sql = f"SELECT {self.PEN_COLS} FROM pens WHERE farm_id = %s"
        args = [farm_id]
        if zone is not None:
            sql += " AND zone = %s"
            args.append(zone)
        with self._connect() as conn:
            return self._rows(conn.execute(sql + " ORDER BY zone, name", args), self.PEN_COLS)

    def delete_pen(self, farm_id, pen_id) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "DELETE FROM pens WHERE id = %s AND farm_id = %s RETURNING id",
                (pen_id, farm_id)).fetchone()
            return row is not None

    def add_sow(self, farm_id, ear_tag, entry_date=None, birth_date=None,
                breed="", sire_tag="", dam_tag="", parity=0, created_by=None) -> int:
        with self._connect() as conn:
            return conn.execute(
                "INSERT INTO sows (farm_id, ear_tag, entry_date, birth_date, breed,"
                " sire_tag, dam_tag, parity, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " RETURNING id",
                (farm_id, ear_tag, entry_date, birth_date, breed,
                 sire_tag, dam_tag, parity, created_by)).fetchone()[0]

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
        ok = row is not None
        if ok:
            # sow_events 對 sow_id 是 ON DELETE CASCADE,刪一頭豬等於連她的
            # 事件一起消失 —— 快取不知道這件事,要主動清掉。目前唯一的呼叫
            # 端(收回種豬進場)只在她完全沒有事件時才准刪,所以實際上清的
            # 是一份沒變的資料;但這條規則哪天放寬,這裡就是會出事的地方。
            self._invalidate_farm_events_cache(farm_id)
        return ok

    def add_boar(self, farm_id, ear_tag, entry_date=None, breed="",
                 sire_tag="", dam_tag="", created_by=None) -> int:
        with self._connect() as conn:
            return conn.execute(
                "INSERT INTO boars (farm_id, ear_tag, entry_date, breed, sire_tag, dam_tag,"
                " created_by) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (farm_id, ear_tag, entry_date, breed, sire_tag, dam_tag,
                 created_by)).fetchone()[0]

    BOAR_COLS = ("id, farm_id, ear_tag, entry_date, breed, sire_tag, dam_tag, status,"
                 " created_by")

    def list_boars(self, farm_id, status=None):
        sql = f"SELECT {self.BOAR_COLS} FROM boars WHERE farm_id = %s"
        args = [farm_id]
        if status is not None:
            sql += " AND status = %s"
            args.append(status)
        with self._connect() as conn:
            return self._rows(conn.execute(sql + " ORDER BY ear_tag", args), self.BOAR_COLS)

    def get_boar(self, farm_id, boar_id):
        with self._connect() as conn:
            rows = self._rows(conn.execute(
                f"SELECT {self.BOAR_COLS} FROM boars WHERE farm_id = %s AND id = %s",
                (farm_id, boar_id)), self.BOAR_COLS)
        return rows[0] if rows else None

    def find_boar_by_tag(self, farm_id, ear_tag):
        with self._connect() as conn:
            rows = self._rows(conn.execute(
                f"SELECT {self.BOAR_COLS} FROM boars WHERE farm_id = %s AND ear_tag = %s",
                (farm_id, ear_tag)), self.BOAR_COLS)
        return rows[0] if rows else None

    def update_boar(self, farm_id, boar_id, **fields) -> bool:
        allowed = {"ear_tag", "entry_date", "breed", "sire_tag", "dam_tag", "status"}
        bad = set(fields) - allowed
        if bad:                       # 欄位名直接進 SQL,必須先過白名單
            raise ValueError(f"不允許更新的欄位:{sorted(bad)}")
        if not fields:
            return False
        sets = ", ".join(f"{k} = %s" for k in fields)
        with self._connect() as conn:
            row = conn.execute(
                f"UPDATE boars SET {sets} WHERE id = %s AND farm_id = %s RETURNING id",
                list(fields.values()) + [boar_id, farm_id]).fetchone()
            return row is not None

    def delete_boar(self, farm_id, boar_id) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "DELETE FROM boars WHERE id = %s AND farm_id = %s RETURNING id",
                (boar_id, farm_id)).fetchone()
            return row is not None

    BOAR_EVENT_COLS = ("id, farm_id, boar_id, event_type, event_date, detail,"
                       " recorded_by, excluded")

    def add_boar_event(self, farm_id, boar_id, event_type, event_date,
                       detail=None, recorded_by=None) -> int:
        with self._connect() as conn:
            return conn.execute(
                "INSERT INTO boar_events (farm_id, boar_id, event_type, event_date,"
                " detail, recorded_by) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (farm_id, boar_id, event_type, event_date,
                 Jsonb(detail or {}), recorded_by)).fetchone()[0]

    def list_boar_events(self, farm_id, boar_id=None):
        sql = f"SELECT {self.BOAR_EVENT_COLS} FROM boar_events WHERE farm_id = %s"
        args = [farm_id]
        if boar_id is not None:
            sql += " AND boar_id = %s"; args.append(boar_id)
        with self._connect() as conn:
            rows = self._rows(conn.execute(sql + " ORDER BY event_date, id", args),
                              self.BOAR_EVENT_COLS)
        for r in rows:
            if not isinstance(r["detail"], dict):
                r["detail"] = json.loads(r["detail"])
        return rows

    def delete_boar_event(self, farm_id, event_id) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "DELETE FROM boar_events WHERE id = %s AND farm_id = %s RETURNING id",
                (event_id, farm_id)).fetchone()
            return row is not None

    MARKET_DEATH_COLS = "id, farm_id, event_date, reason, weight_kg, recorded_by"

    def add_market_death(self, farm_id, event_date, reason="", weight_kg=None,
                         recorded_by=None) -> int:
        with self._connect() as conn:
            return conn.execute(
                "INSERT INTO market_deaths (farm_id, event_date, reason, weight_kg,"
                " recorded_by) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (farm_id, event_date, reason, weight_kg, recorded_by)).fetchone()[0]

    def list_market_deaths(self, farm_id):
        with self._connect() as conn:
            return self._rows(conn.execute(
                f"SELECT {self.MARKET_DEATH_COLS} FROM market_deaths WHERE farm_id = %s"
                " ORDER BY event_date, id", (farm_id,)), self.MARKET_DEATH_COLS)

    def delete_market_death(self, farm_id, death_id) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "DELETE FROM market_deaths WHERE id = %s AND farm_id = %s RETURNING id",
                (death_id, farm_id)).fetchone()
            return row is not None

    def add_sow_event(self, farm_id, sow_id, event_type, event_date,
                      detail=None, recorded_by=None, seq=0) -> int:
        # ON CONFLICT DO UPDATE(而非 DO NOTHING)才拿得回既有的 id,
        # 匯入重跑時呼叫端不必自己查一次。
        with self._connect() as conn:
            event_id = conn.execute(
                "INSERT INTO sow_events (farm_id, sow_id, event_type, event_date,"
                " detail, seq, recorded_by) VALUES (%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (sow_id, event_type, event_date, detail, seq)"
                " DO UPDATE SET recorded_by = EXCLUDED.recorded_by RETURNING id",
                (farm_id, sow_id, event_type, event_date,
                 Jsonb(detail or {}), seq, recorded_by)).fetchone()[0]
        self._invalidate_farm_events_cache(farm_id)
        return event_id

    def list_sow_events(self, farm_id, sow_id=None, since=None, until=None):
        # 只快取「查全場」這個最貴、也最常被平行打的情形 —— 查單一頭豬
        # 的事件本來就便宜,快取反而多一層維護成本換不到什麼。
        whole_farm = sow_id is None and since is None and until is None
        if whole_farm:
            cached = self._cached_farm_events(farm_id)
            if cached is not None:
                return [dict(e) for e in cached]
            version_before = self._farm_events_cache_version(farm_id)

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

        if whole_farm:
            self._maybe_cache_farm_events(farm_id, version_before, rows)
            return [dict(e) for e in rows]
        return rows

    def delete_sow_event(self, farm_id, event_id) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "DELETE FROM sow_events WHERE id = %s AND farm_id = %s RETURNING id",
                (event_id, farm_id)).fetchone()
        ok = row is not None
        if ok:
            self._invalidate_farm_events_cache(farm_id)
        return ok

    def set_event_excluded(self, farm_id, event_id, excluded) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "UPDATE sow_events SET excluded = %s"
                " WHERE id = %s AND farm_id = %s RETURNING id",
                (bool(excluded), event_id, farm_id)).fetchone()
        ok = row is not None
        if ok:
            self._invalidate_farm_events_cache(farm_id)
        return ok

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
