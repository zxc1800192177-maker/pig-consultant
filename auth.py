"""帳號與 session —— 密碼怎麼存、身分怎麼驗。

不碰 HTTP(cookie、狀態碼那些在 server.py),也不碰 SQL(在 db.py)。
這裡只有規則本身,所以測試不需要資料庫也不需要開 socket。

**密碼用 hashlib.scrypt,不是 sha256。** 一般雜湊為「算得快」而設計,
正好是密碼儲存最不該有的性質 —— 外洩後攻擊者每秒可以試上億組。
scrypt 是刻意設計成又慢又吃記憶體的金鑰衍生函式,同樣的硬體一秒只能
試幾千組。Python 3.6 起就內建,不必為此多裝一個套件。
"""

import hashlib
import pathlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional

import config
from db import Store, new_token

COMMON_PASSWORDS_PATH = (
    pathlib.Path(__file__).parent / "data" / "common_passwords.txt"
)


def _load_common_passwords() -> frozenset:
    """常見弱密碼清單。檔案不存在時回空集合而不是壞掉 ——
    少了這道檢查仍有其他規則把關,但整個註冊功能不該因此癱瘓。
    """
    try:
        lines = COMMON_PASSWORDS_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()
    return frozenset(
        line.strip().lower() for line in lines
        if line.strip() and not line.startswith("#")
    )


COMMON_PASSWORDS = _load_common_passwords()

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
    # v2:資料屬於牧場而非個人(憲法第十一條)。放進 User 而不是讓每個
    # API 各自再查一次 —— 少查一次就少一個忘記帶 farm_id 的機會。
    farm_id: Optional[int] = None
    role: str = "owner"

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"


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


def _is_sequential(text: str) -> bool:
    """整串都是連續遞增或遞減的字元(12345678、abcdefgh、87654321)。"""
    if len(text) < 3:
        return False
    deltas = {ord(b) - ord(a) for a, b in zip(text, text[1:])}
    return deltas in ({1}, {-1})


def password_strength_units(text: str) -> int:
    """密碼長度的「有效強度」,非 ASCII 字元算兩個單位。

    使用者是台灣豬農,中文密碼很自然。但用同一個字元數下限對中文並不
    合理:英文字母只有 26 種可能,常用漢字有數千種 —— 4 個中文字的
    猜測空間已經遠大於 8 個英文字母。硬性要求 8 個中文字,只會讓人
    放棄改用「pig12345」這種更好猜的組合。

    權重取 2 是刻意保守的(實際熵比大得多),寧可要求嚴一點。
    """
    return sum(2 if ord(ch) > 127 else 1 for ch in text)


def validate_password(raw, username: Optional[str] = None) -> str:
    """密碼強度檢查。

    現實中帳號被盜,絕大多數不是因為雜湊被破解,而是因為密碼太弱或
    與其他網站重複 —— 所以這裡的規則比雜湊演算法的選擇更能決定實際安全。

    規則刻意只擋「結構性的弱」,不要求大小寫符號混用:那種規則會逼使用者
    寫在紙上或用 Password1! 這種一樣好猜的組合,實務上反而更糟。
    """
    if not isinstance(raw, str):
        raise ValidationError("密碼必須是文字")
    if password_strength_units(raw) < config.MIN_PASSWORD_CHARS:
        raise ValidationError(
            f"密碼太短,請用至少 {config.MIN_PASSWORD_CHARS} 個英數字元"
            f"(中文字算兩個,所以 {config.MIN_PASSWORD_CHARS // 2} 個中文字也可以)"
        )

    lowered = raw.lower()

    if lowered in COMMON_PASSWORDS:
        raise ValidationError("這組密碼太常見了,很容易被猜中,請換一組")
    if raw.isdigit():
        raise ValidationError("密碼不能全部都是數字,請加入英文字母")
    if len(set(raw)) == 1:
        raise ValidationError("密碼不能只由同一個字元重複組成")
    if _is_sequential(lowered):
        raise ValidationError("密碼不能是連續的字元(例如 12345678),請換一組")
    if username and lowered == str(username).strip().lower():
        raise ValidationError("密碼不能與使用者名稱相同")

    return raw


def hash_token(token: str) -> str:
    """session token 存進資料庫前先雜湊。

    密碼與 token 用不同的演算法,理由是威脅模型不同:
    - 密碼是人選的,熵很低,可以用字典猜 —— 所以要用刻意很慢的 scrypt,
      把每次嘗試的成本拉高。
    - token 是 256 位元的密碼學亂數,猜不到,不需要防字典攻擊 ——
      這裡要防的只是「資料庫外洩後,裡面的值可以直接拿來冒用身分」。
      sha256 就足夠達成這件事,而且快到不會拖慢每一個請求。
      若這裡也用 scrypt,每個已登入的請求都要多花數十毫秒。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Auth:
    def __init__(self, store: Store):
        self.store = store

    # --- session ---

    def _issue_session(self, user_id: int) -> str:
        """回傳原始 token(要放進 cookie),資料庫只留雜湊值。"""
        token = new_token()
        expires = datetime.now(timezone.utc) + timedelta(days=config.SESSION_TTL_DAYS)
        self.store.create_session(hash_token(token), user_id, expires)
        return token

    def _ensure_farm(self, user_id: int, name: str) -> int:
        """每個使用者建立時就有一座牧場。

        介面上先做單人(不建邀請功能),但資料一開始就掛在牧場底下 ——
        日後開放共用時零遷移(見 specs/v2-facts.md)。
        """
        farm_id = self.store.create_farm(name)
        self.store.set_user_farm(user_id, farm_id, "owner")
        return farm_id

    def resolve_session(self, token: Optional[str]) -> Optional[User]:
        """token 對應的使用者;無效或過期回 None。"""
        if not token:
            return None
        user_id = self.store.get_session_user_id(
            hash_token(token), datetime.now(timezone.utc)
        )
        if user_id is None:
            return None
        row = self.store.get_user_by_id(user_id)
        if not row:
            return None

        farm_id = row.get("farm_id")
        role = row.get("role") or "owner"
        if farm_id is None:
            # v2 上線前就存在的帳號:當時 users 表還沒有 farm_id 欄位,
            # ALTER TABLE 加欄位不會幫舊資料補值。這裡補建一次,不然
            # 這種帳號會永遠卡在「這個帳號還沒有對應的牧場」,連匯入
            # 資料都做不到 —— 這是實際發生過的問題,不是假設。
            farm_id = self._ensure_farm(user_id, f"{row['username'] or '我'} 的牧場")
            role = "owner"
        return User(id=row["id"], username=row["username"], is_guest=row["is_guest"],
                    farm_id=farm_id, role=role)

    def logout(self, token: Optional[str]) -> None:
        if token:
            self.store.delete_session(hash_token(token))

    # --- 註冊與登入 ---

    def register(self, username, password) -> Authenticated:
        name = normalize_username(username)
        pw = validate_password(password, username=name)
        if self.store.get_user_by_username(name):
            raise UsernameTaken("這個使用者名稱已經有人用了")
        user_id = self.store.create_user(name, hash_password(pw), is_guest=False)
        self._ensure_farm(user_id, f"{name} 的牧場")
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
        self._ensure_farm(user_id, "試用牧場")
        return Authenticated(User(user_id, None, True), self._issue_session(user_id))

    def delete_account(self, token, password) -> None:
        """永久刪除目前登入的帳號。無法復原。

        **一定要重新驗一次密碼。** 這是不可逆的破壞性動作,而登入狀態
        可能是幾天前留下的 cookie —— 借到別人沒鎖的手機就能把整座牧場
        的記錄清光,那個代價太大。

        訪客沒有密碼可驗(password_hash 是 NULL),對他們而言那張 cookie
        本身就是唯一憑證,拿得到 cookie 就已經等同於本人。
        """
        user = self.resolve_session(token)
        if user is None:
            raise InvalidCredentials("尚未登入")
        if not user.is_guest:
            row = self.store.get_user_by_id(user.id)
            if not row or not verify_password(str(password), row["password_hash"]):
                raise InvalidCredentials("密碼錯誤,帳號沒有被刪除")
        self.store.delete_account(user.id)

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
        pw = validate_password(password, username=name)
        if self.store.get_user_by_username(name):
            raise UsernameTaken("這個使用者名稱已經有人用了")

        if not self.store.promote_guest(current.id, name, hash_password(pw)):
            # promote_guest 內含 is_guest 條件,回 False 代表狀態在
            # 檢查之後被改掉了(例如同時開兩個分頁各按一次升級)。
            raise NotGuest("這個帳號已經設定過帳號密碼了")

        return Authenticated(User(current.id, name, False), token)
