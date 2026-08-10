"""藥品標示辨識結果的解析。

這一層要能吃下模型各種不聽話的輸出形式而不炸掉 —— 照片糊掉、模型
講廢話都是預期中會發生的事,不是程式錯誤。解析不出來就四個欄位全 None,
呼叫端據此請使用者重拍。

**休藥期的測試特別多**:那是唯一一個讀錯會讓帶藥豬肉上市的欄位,
寧可回 None 讓人自己填,也不要放一個看起來合理的數字進去。
"""

import config
from ai.label import parse_label


class TestCleanOutput:
    def test_reads_all_four_fields(self):
        result = parse_label(
            '{"name": "阿莫西林可溶性粉", "activeIngredient": "Amoxicillin 10%",'
            ' "dosageNote": "每公斤10mg", "withdrawalDays": 7}'
        )
        assert result["name"] == "阿莫西林可溶性粉"
        assert result["activeIngredient"] == "Amoxicillin 10%"
        assert result["dosageNote"] == "每公斤10mg"
        assert result["withdrawalDays"] == 7

    def test_missing_fields_become_none(self):
        result = parse_label('{"name": "某藥"}')
        assert result["name"] == "某藥"
        assert result["activeIngredient"] is None
        assert result["dosageNote"] is None
        assert result["withdrawalDays"] is None

    def test_explicit_nulls_stay_none(self):
        result = parse_label('{"name": "某藥", "withdrawalDays": null}')
        assert result["withdrawalDays"] is None


class TestModelDisobedience:
    """提示詞叫它只輸出 JSON,但模型不一定照做。提示詞管不動的事,
    程式碼要管得動 —— 為了這種小事叫使用者重拍一次很浪費。
    """

    def test_strips_json_fence(self):
        result = parse_label('```json\n{"name": "某藥"}\n```')
        assert result["name"] == "某藥"

    def test_strips_bare_fence(self):
        result = parse_label('```\n{"name": "某藥"}\n```')
        assert result["name"] == "某藥"

    def test_tolerates_preamble(self):
        result = parse_label('以下是辨識結果:\n{"name": "某藥"}')
        assert result["name"] == "某藥"

    def test_tolerates_trailing_text(self):
        result = parse_label('{"name": "某藥"}\n希望對你有幫助!')
        assert result["name"] == "某藥"

    def test_braces_inside_strings_do_not_break_matching(self):
        result = parse_label('{"name": "怪藥{名", "dosageNote": "備註}"}')
        assert result["name"] == "怪藥{名"
        assert result["dosageNote"] == "備註}"

    def test_object_wrapped_in_array_is_unwrapped(self):
        """模型偶爾會把單一結果包成陣列。取出裡面的物件比要求使用者
        重拍一次有用 —— 反正結果一律要人核對過才存得進去。

        代價:照片裡真的有多種藥時只會取第一個。這次的範圍就是單張
        單一藥品(見計畫的「不在範圍內」),而使用者按新增之前會看到
        填進表單的是哪一種藥。
        """
        assert parse_label('[{"name": "某藥"}]')["name"] == "某藥"


class TestUnreadable:
    """讀不出來是正常結果,不是錯誤 —— 一律回全 None,不拋例外。"""

    def test_pure_prose_gives_nothing(self):
        assert parse_label("這張照片太模糊了,看不清楚。")["name"] is None

    def test_broken_json_gives_nothing(self):
        assert parse_label('{"name": "某藥"')["name"] is None

    def test_empty_string_gives_nothing(self):
        assert parse_label("")["name"] is None

    def test_none_input_gives_nothing(self):
        assert parse_label(None)["name"] is None

    def test_bare_array_is_not_treated_as_a_drug(self):
        """陣列本身不是藥品資料,直接 .get() 會爆掉。"""
        assert parse_label('["某藥", "另一種藥"]')["name"] is None

    def test_placeholder_words_count_as_missing(self):
        """模型有時不填 null 而填「無」「看不清楚」—— 那是在描述「沒有」,
        不是標示上真的印著這兩個字。
        """
        for placeholder in ("無", "看不清楚", "null", "N/A", "-"):
            result = parse_label(f'{{"name": "某藥", "dosageNote": "{placeholder}"}}')
            assert result["dosageNote"] is None, placeholder


class TestWithdrawalDaysIsGuarded:
    """休藥期是唯一一個讀錯會有食安後果的欄位,型別收斂要最嚴。"""

    def test_accepts_zero(self):
        """0 天是有效答案(部分藥物確實不需要休藥期),不可以被當成沒填。"""
        assert parse_label('{"name": "藥", "withdrawalDays": 0}')["withdrawalDays"] == 0

    def test_rejects_negative(self):
        assert parse_label('{"name": "藥", "withdrawalDays": -3}')["withdrawalDays"] is None

    def test_rejects_string(self):
        assert parse_label('{"name": "藥", "withdrawalDays": "7天"}')["withdrawalDays"] is None

    def test_rejects_fraction(self):
        """半天這種值多半是模型自己換算出來的,不是標示上印的。"""
        assert parse_label('{"name": "藥", "withdrawalDays": 7.5}')["withdrawalDays"] is None

    def test_accepts_whole_float(self):
        assert parse_label('{"name": "藥", "withdrawalDays": 7.0}')["withdrawalDays"] == 7

    def test_rejects_boolean(self):
        """Python 的 True 是 int 的子類別,不特別擋掉會變成休藥期 1 天。"""
        assert parse_label('{"name": "藥", "withdrawalDays": true}')["withdrawalDays"] is None


class TestLengthLimits:
    """模型輸出一樣是不可信輸入 —— 一段超長字串會一路帶進資料庫。"""

    def test_name_truncated(self):
        long_name = "藥" * (config.MAX_DRUG_NAME_CHARS + 50)
        result = parse_label(f'{{"name": "{long_name}"}}')
        assert len(result["name"]) == config.MAX_DRUG_NAME_CHARS

    def test_note_truncated(self):
        long_note = "字" * (config.MAX_DRUG_NOTE_CHARS + 50)
        result = parse_label(f'{{"name": "藥", "dosageNote": "{long_note}"}}')
        assert len(result["dosageNote"]) == config.MAX_DRUG_NOTE_CHARS

    def test_ingredient_truncated(self):
        long_ingredient = "字" * (config.MAX_DRUG_INGREDIENT_CHARS + 50)
        result = parse_label(f'{{"name": "藥", "activeIngredient": "{long_ingredient}"}}')
        assert len(result["activeIngredient"]) == config.MAX_DRUG_INGREDIENT_CHARS
