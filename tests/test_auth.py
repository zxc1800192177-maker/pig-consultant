"""帳號與 session 測試。

安全性相關的行為要當成規格來鎖,不是「順便測一下」:
密碼不得明文可還原、失敗訊息不得洩露帳號是否存在、
一個使用者不得碰到另一個使用者的資料。
"""

from datetime import datetime, timedelta, timezone

import pytest

import config
from auth import (
    Auth,
    InvalidCredentials,
    NotGuest,
    UsernameTaken,
    ValidationError,
    hash_password,
    hash_token,
    normalize_username,
    validate_password,
    verify_password,
)
from db import InMemoryStore


@pytest.fixture
def store():
    return InMemoryStore()


@pytest.fixture
def auth(store):
    return Auth(store)


class TestPasswordHashing:
    def test_roundtrip(self):
        stored = hash_password("correct horse battery")
        assert verify_password("correct horse battery", stored) is True

    def test_wrong_password_rejected(self):
        stored = hash_password("correct horse battery")
        assert verify_password("wrong password", stored) is False

    def test_hash_never_contains_the_plaintext(self):
        """最基本的一條:雜湊值裡不該找得到原始密碼。"""
        secret = "unmistakable-plaintext-9021"
        assert secret not in hash_password(secret)

    def test_same_password_hashes_differently_each_time(self):
        """每次用新的 salt。否則相同密碼會有相同雜湊,一眼就能看出
        哪些帳號共用同一組密碼,也讓彩虹表可用。
        """
        assert hash_password("same-password") != hash_password("same-password")

    def test_stored_hash_carries_its_parameters(self):
        """參數要跟著存,日後調高成本因子時舊密碼才驗得起來。"""
        assert hash_password("x").startswith("scrypt$")

    def test_none_hash_is_rejected_not_crashed(self):
        """訪客沒有密碼(None)。有人拿訪客名稱嘗試登入時不能炸掉。"""
        assert verify_password("anything", None) is False

    def test_empty_password_rejected(self):
        assert verify_password("", hash_password("x")) is False

    def test_corrupted_hash_rejected_not_crashed(self):
        for broken in ("", "notascheme", "scrypt$bad", "scrypt$a$b$c$d$e"):
            assert verify_password("x", broken) is False


class TestUsernameNormalization:
    def test_strips_surrounding_whitespace(self):
        """不 trim 的話「ian」與「ian 」是兩個帳號,但畫面上看不出差別。"""
        assert normalize_username("  ian  ") == "ian"

    def test_rejects_too_short(self):
        with pytest.raises(ValidationError):
            normalize_username("a")

    def test_rejects_whitespace_only(self):
        with pytest.raises(ValidationError):
            normalize_username("      ")

    def test_rejects_too_long(self):
        with pytest.raises(ValidationError):
            normalize_username("x" * (config.MAX_USERNAME_CHARS + 1))

    def test_rejects_non_string(self):
        with pytest.raises(ValidationError):
            normalize_username({"a": 1})


class TestPasswordStrength:
    """實測發現的真實弱點:原本只檢查長度,「password」與「12345678」
    都能通過。現實中帳號被盜,絕大多數不是雜湊被破解,而是密碼太好猜。
    """

    def test_common_password_rejected(self):
        for weak in ("password", "12345678", "qwertyui", "iloveyou", "abcd1234"):
            with pytest.raises(ValidationError):
                validate_password(weak)

    def test_common_password_check_ignores_case(self):
        with pytest.raises(ValidationError):
            validate_password("PassWord")

    def test_all_digits_rejected(self):
        """生日、電話號碼是最常見的一類弱密碼。"""
        for digits in ("19900101", "0912345678", "24681357"):
            with pytest.raises(ValidationError):
                validate_password(digits)

    def test_single_repeated_character_rejected(self):
        with pytest.raises(ValidationError):
            validate_password("aaaaaaaa")

    def test_sequential_characters_rejected(self):
        for seq in ("abcdefgh", "87654321", "abcdefghij"):
            with pytest.raises(ValidationError):
                validate_password(seq)

    def test_password_equal_to_username_rejected(self):
        with pytest.raises(ValidationError):
            validate_password("pigfarmer", username="pigfarmer")

    def test_password_equal_to_username_ignores_case_and_space(self):
        with pytest.raises(ValidationError):
            validate_password("PigFarmer", username="  pigfarmer  ")

    def test_reasonable_password_accepted(self):
        """規則不能嚴到讓正常人選不出密碼 —— 那會逼人寫在紙上。"""
        for good in ("pig-barn-2026", "muddyBoots7", "correct horse battery"):
            assert validate_password(good) == good

    def test_short_password_still_rejected(self):
        with pytest.raises(ValidationError):
            validate_password("pig7")


class TestChinesePasswordLength:
    """使用者是台灣豬農,中文密碼很自然。

    英文字母只有 26 種可能,常用漢字有數千種 —— 用同一個字元數下限對
    中文並不合理,只會逼人改用「pig12345」這種更好猜的組合。
    """

    def test_chinese_passphrase_accepted(self):
        assert validate_password("我家的豬很健康") == "我家的豬很健康"

    def test_four_chinese_characters_is_enough(self):
        assert validate_password("健康的豬") == "健康的豬"

    def test_too_few_chinese_characters_still_rejected(self):
        with pytest.raises(ValidationError):
            validate_password("小豬")

    def test_repeated_chinese_character_still_rejected(self):
        """放寬長度不代表放行結構性的弱密碼。"""
        with pytest.raises(ValidationError):
            validate_password("豬豬豬豬豬")

    def test_mixed_chinese_and_ascii_counts_both(self):
        assert validate_password("豬farm22") == "豬farm22"

    def test_error_message_explains_the_chinese_allowance(self):
        """使用者看到「至少 8 個字」卻打了 5 個中文被擋,會以為系統壞了。"""
        with pytest.raises(ValidationError) as e:
            validate_password("小豬")
        assert "中文" in str(e.value)

    def test_registration_enforces_strength(self, auth):
        with pytest.raises(ValidationError):
            auth.register("farmer", "password")

    def test_claim_enforces_strength(self, auth):
        guest = auth.guest_login()
        with pytest.raises(ValidationError):
            auth.claim(guest.token, "farmer", "12345678")


class TestSessionTokenStorage:
    """token 存進資料庫前要先雜湊。

    密碼有雜湊保護,token 若沒有,資料庫外洩時攻擊者拿到的是**可以直接
    使用的登入憑證** —— 不需要破解任何東西,在到期前都能冒用身分。
    """

    def test_raw_token_is_not_stored(self, auth, store):
        result = auth.register("farmer", "hunter2hunter2")
        assert result.token not in store.sessions
        assert result.token not in str(store.sessions)

    def test_hashed_token_is_what_gets_stored(self, auth, store):
        result = auth.register("farmer", "hunter2hunter2")
        assert hash_token(result.token) in store.sessions

    def test_session_still_resolves_with_the_raw_token(self, auth):
        """雜湊儲存不能影響正常使用 —— cookie 裡放的仍是原始 token。"""
        result = auth.register("farmer", "hunter2hunter2")
        assert auth.resolve_session(result.token).username == "farmer"

    def test_stolen_database_value_cannot_be_used_as_a_token(self, auth, store):
        """關鍵的一條:直接拿資料庫裡的值當 cookie 用,必須無效。"""
        result = auth.register("farmer", "hunter2hunter2")
        stored_value = next(iter(store.sessions))
        assert auth.resolve_session(stored_value) is None

    def test_logout_removes_the_hashed_entry(self, auth, store):
        result = auth.register("farmer", "hunter2hunter2")
        auth.logout(result.token)
        assert store.sessions == {}

    def test_hash_is_deterministic(self):
        assert hash_token("abc") == hash_token("abc")
        assert hash_token("abc") != hash_token("abd")


class TestRegister:
    def test_creates_account_and_session(self, auth):
        result = auth.register("farmer", "hunter2hunter2")
        assert result.user.username == "farmer"
        assert result.user.is_guest is False
        assert result.token

    def test_session_resolves_back_to_the_user(self, auth):
        result = auth.register("farmer", "hunter2hunter2")
        assert auth.resolve_session(result.token).id == result.user.id

    def test_duplicate_username_rejected(self, auth):
        auth.register("farmer", "hunter2hunter2")
        with pytest.raises(UsernameTaken):
            auth.register("farmer", "different-password")

    def test_duplicate_check_ignores_surrounding_whitespace(self, auth):
        auth.register("farmer", "hunter2hunter2")
        with pytest.raises(UsernameTaken):
            auth.register("  farmer  ", "different-password")

    def test_short_password_rejected(self, auth):
        with pytest.raises(ValidationError):
            auth.register("farmer", "short")

    def test_password_not_stored_in_plaintext(self, auth, store):
        auth.register("farmer", "hunter2hunter2")
        assert "hunter2hunter2" not in str(store.users)


class TestLogin:
    def test_correct_credentials(self, auth):
        auth.register("farmer", "hunter2hunter2")
        assert auth.login("farmer", "hunter2hunter2").user.username == "farmer"

    def test_wrong_password_rejected(self, auth):
        auth.register("farmer", "hunter2hunter2")
        with pytest.raises(InvalidCredentials):
            auth.login("farmer", "wrong-password")

    def test_unknown_user_rejected(self, auth):
        with pytest.raises(InvalidCredentials):
            auth.login("nobody", "hunter2hunter2")

    def test_same_error_for_unknown_user_and_wrong_password(self, auth):
        """訊息若有差別,等於告訴嘗試者「這個帳號存在,繼續猜密碼」。"""
        auth.register("farmer", "hunter2hunter2")

        with pytest.raises(InvalidCredentials) as wrong_pw:
            auth.login("farmer", "wrong-password")
        with pytest.raises(InvalidCredentials) as no_such_user:
            auth.login("ghost", "wrong-password")

        assert str(wrong_pw.value) == str(no_such_user.value)

    def test_malformed_username_gives_same_error_not_validation_error(self, auth):
        """格式不合法也要走同一條失敗路徑,不然錯誤型別本身就是線索。"""
        with pytest.raises(InvalidCredentials):
            auth.login("a", "hunter2hunter2")

    def test_login_issues_a_new_session_each_time(self, auth):
        auth.register("farmer", "hunter2hunter2")
        first = auth.login("farmer", "hunter2hunter2").token
        second = auth.login("farmer", "hunter2hunter2").token
        assert first != second

    def test_old_session_still_valid_after_new_login(self, auth):
        """在手機登入不該把電腦上的登入狀態踢掉。"""
        auth.register("farmer", "hunter2hunter2")
        first = auth.login("farmer", "hunter2hunter2").token
        auth.login("farmer", "hunter2hunter2")
        assert auth.resolve_session(first) is not None


class TestSession:
    def test_unknown_token_resolves_to_nothing(self, auth):
        assert auth.resolve_session("not-a-real-token") is None

    def test_empty_token_resolves_to_nothing(self, auth):
        assert auth.resolve_session("") is None
        assert auth.resolve_session(None) is None

    def test_expired_session_rejected(self, auth, store):
        result = auth.register("farmer", "hunter2hunter2")
        # 資料庫的鍵是雜湊後的值,不是原始 token
        store.sessions[hash_token(result.token)]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        assert auth.resolve_session(result.token) is None

    def test_logout_invalidates_the_session(self, auth):
        result = auth.register("farmer", "hunter2hunter2")
        auth.logout(result.token)
        assert auth.resolve_session(result.token) is None

    def test_logout_tolerates_missing_token(self, auth):
        auth.logout(None)      # 不該拋例外
        auth.logout("nope")

    def test_tokens_are_unpredictable(self, auth):
        """可預測的 token 等於任何人都能算出別人的身分。"""
        tokens = {auth.guest_login().token for _ in range(20)}
        assert len(tokens) == 20
        assert all(len(t) > 20 for t in tokens)


class TestGuestLogin:
    def test_creates_a_usable_identity_without_credentials(self, auth):
        result = auth.guest_login()
        assert result.user.is_guest is True
        assert result.user.username is None
        assert auth.resolve_session(result.token).id == result.user.id

    def test_each_guest_is_a_separate_identity(self, auth):
        assert auth.guest_login().user.id != auth.guest_login().user.id

    def test_guest_cannot_be_logged_into_by_name(self, auth):
        """訪客沒有 username(None)。查詢時不得比對到他們 ——
        SQL 的 NULL 比對天生不成立,Python 的 None == None 卻為真。
        """
        auth.guest_login()
        with pytest.raises(InvalidCredentials):
            auth.login(None, "anything")


class TestClaimGuestAccount:
    """訪客升級為正式帳號。重點是資料要延續,不是開一個新的空帳號。"""

    def test_keeps_the_same_user_id(self, auth):
        guest = auth.guest_login()
        claimed = auth.claim(guest.token, "farmer", "hunter2hunter2")
        assert claimed.user.id == guest.user.id

    def test_existing_data_survives_the_upgrade(self, auth, store):
        guest = auth.guest_login()
        store.add_health_check(guest.user.id, {"psy": 20.63})
        store.add_drug(guest.user.id, "阿莫西林")

        claimed = auth.claim(guest.token, "farmer", "hunter2hunter2")

        assert len(store.list_health_checks(claimed.user.id)) == 1
        assert len(store.list_drugs(claimed.user.id)) == 1

    def test_no_longer_a_guest_afterwards(self, auth):
        guest = auth.guest_login()
        claimed = auth.claim(guest.token, "farmer", "hunter2hunter2")
        assert claimed.user.is_guest is False
        assert auth.resolve_session(claimed.token).is_guest is False

    def test_can_log_in_with_the_new_credentials(self, auth):
        guest = auth.guest_login()
        auth.claim(guest.token, "farmer", "hunter2hunter2")
        assert auth.login("farmer", "hunter2hunter2").user.id == guest.user.id

    def test_session_stays_valid_after_claiming(self, auth):
        """升級不該把人踢出去要求重新登入。"""
        guest = auth.guest_login()
        claimed = auth.claim(guest.token, "farmer", "hunter2hunter2")
        assert claimed.token == guest.token
        assert auth.resolve_session(guest.token) is not None

    def test_taken_username_rejected(self, auth):
        auth.register("farmer", "hunter2hunter2")
        guest = auth.guest_login()
        with pytest.raises(UsernameTaken):
            auth.claim(guest.token, "farmer", "another-password")

    def test_guest_stays_a_guest_when_claim_fails(self, auth):
        """失敗後不能留下半升級的狀態,否則使用者既沒帳密又不是訪客。"""
        auth.register("farmer", "hunter2hunter2")
        guest = auth.guest_login()
        with pytest.raises(UsernameTaken):
            auth.claim(guest.token, "farmer", "another-password")
        assert auth.resolve_session(guest.token).is_guest is True

    def test_already_registered_account_cannot_be_reclaimed(self, auth):
        """否則等於提供一條「改掉別人帳號密碼」的路徑。"""
        registered = auth.register("farmer", "hunter2hunter2")
        with pytest.raises(NotGuest):
            auth.claim(registered.token, "newname", "another-password")

    def test_requires_a_valid_session(self, auth):
        with pytest.raises(InvalidCredentials):
            auth.claim("not-a-real-token", "farmer", "hunter2hunter2")

    def test_weak_password_rejected(self, auth):
        guest = auth.guest_login()
        with pytest.raises(ValidationError):
            auth.claim(guest.token, "farmer", "short")


class TestUserDataIsolation:
    """跨帳號隔離。這一組若有任何一條失敗,就是資料外洩。"""

    def test_health_checks_are_not_visible_across_users(self, auth, store):
        a = auth.register("alice", "hunter2hunter2").user
        b = auth.register("bob", "hunter2hunter2").user
        store.add_health_check(a.id, {"psy": 20.63})

        assert len(store.list_health_checks(a.id)) == 1
        assert store.list_health_checks(b.id) == []

    def test_drugs_are_not_visible_across_users(self, auth, store):
        a = auth.register("alice", "hunter2hunter2").user
        b = auth.register("bob", "hunter2hunter2").user
        store.add_drug(a.id, "阿莫西林")

        assert len(store.list_drugs(a.id)) == 1
        assert store.list_drugs(b.id) == []

    def test_cannot_delete_another_users_drug(self, auth, store):
        a = auth.register("alice", "hunter2hunter2").user
        b = auth.register("bob", "hunter2hunter2").user
        drug_id = store.add_drug(a.id, "阿莫西林")

        assert store.delete_drug(b.id, drug_id) is False
        assert len(store.list_drugs(a.id)) == 1

    def test_cannot_delete_another_users_health_check(self, auth, store):
        a = auth.register("alice", "hunter2hunter2").user
        b = auth.register("bob", "hunter2hunter2").user
        check_id = store.add_health_check(a.id, {"psy": 20.63})

        assert store.delete_health_check(b.id, check_id) is False
        assert len(store.list_health_checks(a.id)) == 1


class TestHealthCheckRetention:
    """免費方案的資料庫容量有限,單一帳號不能無限寫入。"""

    def test_keeps_only_the_most_recent(self, auth, store):
        user = auth.register("farmer", "hunter2hunter2").user
        for i in range(config.MAX_HEALTH_CHECKS_PER_USER + 5):
            store.add_health_check(user.id, {"psy": float(i)})

        kept = store.list_health_checks(user.id)
        assert len(kept) == config.MAX_HEALTH_CHECKS_PER_USER

    def test_the_newest_record_survives_trimming(self, auth, store):
        user = auth.register("farmer", "hunter2hunter2").user
        for i in range(config.MAX_HEALTH_CHECKS_PER_USER + 5):
            store.add_health_check(user.id, {"psy": float(i)})

        newest = store.list_health_checks(user.id)[0]
        assert newest["values"]["psy"] == float(config.MAX_HEALTH_CHECKS_PER_USER + 4)

    def test_trimming_does_not_touch_other_users(self, auth, store):
        a = auth.register("alice", "hunter2hunter2").user
        b = auth.register("bob", "hunter2hunter2").user
        store.add_health_check(b.id, {"psy": 1.0})

        for i in range(config.MAX_HEALTH_CHECKS_PER_USER + 5):
            store.add_health_check(a.id, {"psy": float(i)})

        assert len(store.list_health_checks(b.id)) == 1


class TestLegacyAccountsGetABackfilledFarm:
    """v2 上線前(users 表還沒有 farm_id 欄位時)就存在的帳號,ALTER TABLE
    加欄位不會幫舊資料補值,所以這些帳號的 farm_id 是 NULL。register()
    與 guest_login() 都會在建立當下呼叫 _ensure_farm(),但這些帳號當初是
    直接 INSERT 進 users 表的,從來沒經過這條路徑。

    resolve_session() 在每個請求都會被呼叫,所以在這裡補一次是最不會
    漏掉任何人的地方 —— 不然這些帳號會永遠卡在「這個帳號還沒有對應的
    牧場」,連匯入資料都做不到(這正是實際發生過的問題)。
    """

    def _legacy_user_id(self, store):
        """直接寫進 users 表,不經過 register()/_ensure_farm() ——
        模擬 v2 上線前就存在、farm_id 從沒被設定過的帳號。
        """
        return store.create_user("oldtimer", hash_password("hunter2hunter2"))

    def test_gets_a_farm_on_first_resolve(self, auth, store):
        user_id = self._legacy_user_id(store)
        token = auth._issue_session(user_id)

        user = auth.resolve_session(token)

        assert user.farm_id is not None
        assert user.is_owner

    def test_same_farm_on_every_later_request(self, auth, store):
        """每次都補一座新牧場的話,舊帳號的資料就散在好幾座牧場裡看不全。"""
        user_id = self._legacy_user_id(store)
        token = auth._issue_session(user_id)

        first = auth.resolve_session(token).farm_id
        second = auth.resolve_session(token).farm_id

        assert first == second

    def test_backfilled_farm_is_a_real_usable_farm(self, auth, store):
        user_id = self._legacy_user_id(store)
        token = auth._issue_session(user_id)

        farm_id = auth.resolve_session(token).farm_id

        assert store.list_sows(farm_id, None) == []

    def test_accounts_that_already_have_a_farm_are_left_alone(self, auth, store):
        """已經補過的帳號(或本來就是新帳號)不該被重新分配。"""
        registered = auth.register("farmer", "hunter2hunter2")
        original_farm_id = auth.resolve_session(registered.token).farm_id

        assert auth.resolve_session(registered.token).farm_id == original_farm_id
