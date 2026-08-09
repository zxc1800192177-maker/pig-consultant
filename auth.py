"""帳號與 session —— 密碼怎麼存、身分怎麼驗。

不碰 HTTP(cookie、狀態碼那些在 server.py),也不碰 SQL(在 db.py)。
這裡只有規則本身,所以測試不需要資料庫也不需要開 socket。

**密碼用 hashlib.scrypt,不是 sha256。** 一般雜湊為「算得快」而設計,
正好是密碼儲存最不該有的性質 —— 外洩後攻擊者每秒可以試上億組。
scrypt 是刻意設計成又慢又吃記憶體的金鑰衍生函式,同樣的硬體一秒只能
試幾千組。Python 3.6 起就內建,不必為此多裝一個套件。
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional

import config
from db import Store, new_token

# scrypt 參數。n 是成本因子,調高會等比變慢(對攻擊者與對我們都是)。
# 2**14 在一般伺服器上約數十毫秒,對登入來說感覺不到,對暴力破解已經
# 是很重的負擔。
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16


class AuthError(Exception):
    """帳號相關錯誤的共同基底,方便 server.py 一次接住。"""


class ValidationError(AuthError):
    """使用者名稱或密碼不符合規則。訊息會直接顯示給使用者看。"""


class UsernameTaken(AuthError):
    """使用者名稱已被註冊。"""


class InvalidCredentials(AuthError):
    """帳號或密碼錯誤。刻意不分辨是哪一個 —— 分辨了等於告訴嘗試者
    「這個帳號存在,繼續猜密碼就好」。
    """


class NotGuest(AuthError):
    """想把已經是正式帳號的身分再「升級」一次。"""


class User(NamedTuple):
    id: int
    username: Optional[str]
    is_guest: bool


class Authenticated(NamedTuple):
    user: User
    token: str


def hash_password(password: str) -> str:
    """回傳可直接存進資料庫的字串,salt 與參數都包在裡面。

    參數一起存,是為了日後調高成本因子時,舊密碼仍然驗得起來 ——
    只寫死在程式碼裡的話,一改參數所有既有使用者就再也登不進來。
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    """stored 為 None(訪客帳號沒有密碼)時回 False,不拋例外 ——
    否則有人拿訪客的使用者名稱嘗試登入就會炸掉整個請求。
    """
    if not stored or not password:
        return False
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p),
        )
    except (ValueError, TypeError):
        return False
    # compare_digest 而非 == :後者一發現不同就回傳,比對耗時會隨著
    # 「猜對了幾個字元」而變化,可被用來一個字元一個字元地推出雜湊值。
    return secrets.compare_digest(expected.hex(), digest_hex)


def normalize_username(raw) -> str:
    """去頭尾空白並檢查長度。

    不 trim 的話,「ian」跟「ian 」會是兩個不同帳號,但畫面上長得
    一模一樣 —— 使用者只會覺得自己密碼打錯了。
    """
    if not isinstance(raw, str):
        raise ValidationError("使用者名稱必須是文字")
    name = raw.strip()
    if len(name) < config.MIN_USERNAME_CHARS:
        raise ValidationError(f"使用者名稱至少 {config.MIN_USERNAME_CHARS} 個字")
    if len(name) > config.MAX_USERNAME_CHARS:
        raise ValidationError(f"使用者名稱最多 {config.MAX_USERNAME_CHARS} 個字")
    return name


def validate_password(raw) -> str:
    if not isinstance(raw, str):
        raise ValidationError("密碼必須是文字")
    if len(raw) < config.MIN_PASSWORD_CHARS:
        raise ValidationError(f"密碼至少 {config.MIN_PASSWORD_CHARS} 個字元")
    return raw


class Auth:
    def __init__(self, store: Store):
        self.store = store

    # --- session ---

    def _issue_session(self, user_id: int) -> str:
        token = new_token()
        expires = datetime.now(timezone.utc) + timedelta(days=config.SESSION_TTL_DAYS)
        self.store.create_session(token, user_id, expires)
        return token

    def resolve_session(self, token: Optional[str]) -> Optional[User]:
        """token 對應的使用者;無效或過期回 None。"""
        if not token:
            return None
        user_id = self.store.get_session_user_id(token, datetime.now(timezone.utc))
        if user_id is None:
            return None
        row = self.store.get_user_by_id(user_id)
        if not row:
            return None
        return User(id=row["id"], username=row["username"], is_guest=row["is_guest"])

    def logout(self, token: Optional[str]) -> None:
        if token:
            self.store.delete_session(token)

    # --- 註冊與登入 ---

    def register(self, username, password) -> Authenticated:
        name = normalize_username(username)
        pw = validate_password(password)
        if self.store.get_user_by_username(name):
            raise UsernameTaken("這個使用者名稱已經有人用了")
        user_id = self.store.create_user(name, hash_password(pw), is_guest=False)
        return Authenticated(User(user_id, name, False), self._issue_session(user_id))

    def login(self, username, password) -> Authenticated:
        try:
            name = normalize_username(username)
        except ValidationError:
            # 格式就不對時也走同一條失敗路徑,不讓錯誤訊息透露
            # 「這個名字格式合法但密碼錯」跟「名字根本不合法」的差別。
            raise InvalidCredentials("帳號或密碼錯誤")

        row = self.store.get_user_by_username(name)
        if not row or row["is_guest"]:
            # 帳號不存在時仍然做一次雜湊運算再失敗 —— 直接回傳的話,
            # 「不存在」比「密碼錯」快得多,可以用回應時間掃出哪些
            # 帳號真的存在。
            verify_password(str(password), hash_password("dummy"))
            raise InvalidCredentials("帳號或密碼錯誤")

        if not verify_password(str(password), row["password_hash"]):
            raise InvalidCredentials("帳號或密碼錯誤")

        user = User(row["id"], row["username"], row["is_guest"])
        return Authenticated(user, self._issue_session(row["id"]))

    # --- 訪客 ---

    def guest_login(self) -> Authenticated:
        """免帳密的身分。資料一樣存在資料庫,但只有這張 cookie 能存取 ——
        沒有密碼可以在別台裝置登回來,這點必須讓使用者知道(見前端提示)。
        """
        user_id = self.store.create_user(None, None, is_guest=True)
        return Authenticated(User(user_id, None, True), self._issue_session(user_id))

    def claim(self, token, username, password) -> Authenticated:
        """訪客設定帳密,升級為正式帳號。

        關鍵:沿用同一個 user_id,所以既有的健檢紀錄與藥品庫都還在。
        另外開一個新帳號再搬資料的做法,只要中途失敗就會兩邊都不完整。
        """
        current = self.resolve_session(token)
        if current is None:
            raise InvalidCredentials("尚未登入")
        if not current.is_guest:
            raise NotGuest("這個帳號已經設定過帳號密碼了")

        name = normalize_username(username)
        pw = validate_password(password)
        if self.store.get_user_by_username(name):
            raise UsernameTaken("這個使用者名稱已經有人用了")

        if not self.store.promote_guest(current.id, name, hash_password(pw)):
            # promote_guest 內含 is_guest 條件,回 False 代表狀態在
            # 檢查之後被改掉了(例如同時開兩個分頁各按一次升級)。
            raise NotGuest("這個帳號已經設定過帳號密碼了")

        return Authenticated(User(current.id, name, False), token)
