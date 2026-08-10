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
CREATE TABLE IF NOT EXISTS users (
  id SERIAL PRIMARY KEY,
  username TEXT,
  password_hash TEXT,
  is_guest BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

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

CREATE TABLE IF NOT EXISTS my_drugs (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  active_ingredient TEXT NOT NULL DEFAULT '',
  dosage_note TEXT NOT NULL DEFAULT '',
  withdrawal_days INTEGER,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS my_drugs_user_idx ON my_drugs (user_id, created_at);

-- active_ingredient 是後來才加的欄位。上面的 CREATE TABLE IF NOT EXISTS
-- 對「已經存在的資料表」完全不做事,所以正式站那些既有的 my_drugs
-- 不會因為改了上面的定義就長出新欄位 —— 必須另外補這一句。
-- ADD COLUMN IF NOT EXISTS 讓它跟這個檔案其他語句一樣可以重複執行,
-- 每次啟動都跑一遍也不會出錯(見 ensure_schema)。
ALTER TABLE my_drugs ADD COLUMN IF NOT EXISTS active_ingredient TEXT NOT NULL DEFAULT '';
"""


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


class InMemoryStore(Store):
    """測試用。行為必須跟 PostgresStore 一致,尤其是 user_id 隔離 ——
    如果這裡漏掉隔離,測試會通過但正式環境會外洩,那比沒有測試更糟。
    """

    def __init__(self):
        self.users = {}
        self.sessions = {}
        self.health_checks = []
        self.drugs = []
        self._next_user_id = 1
        self._next_check_id = 1
        self._next_drug_id = 1

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


def select_store() -> Optional[Store]:
    """沒設 DATABASE_URL 就回 None —— 帳號功能整個關閉,網站退回
    「純工具、免帳號」模式。本機開發與 demo 不必為了跑起來而先去
    申請一個資料庫。
    """
    if not config.DATABASE_URL:
        return None
    store = PostgresStore(config.DATABASE_URL)
    store.ensure_schema()
    return store
