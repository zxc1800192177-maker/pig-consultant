"""官方劑量對照表比對。

「劑量查表化」的核心模組。同業比較後發現的關鍵差異:多數同類產品要嘛完全
不碰用藥劑量,要嘛讓 AI 自由生成 —— 後者的風險是休藥期若講錯,藥物殘留的
豬肉會直接流入食物鏈,傷害的是第三方消費者,不是使用者自己。

因此這裡的設計原則跟 core/reportable.py 一致:結果是固定資料,不經過 AI。
一旦要顯示「某藥劑量是多少」,數字只能來自這裡(管理者查證過的資料)或
使用者自己輸入的藥品庫(牧場主抄自己藥品標示,信任邊界是他自己擁有的
實體藥品,不是任何人生成的內容)。

data/dosage_table.json 裡目前的資料是 AI 從官方手冊(《豬隻飼養管理與
安全用藥手冊》,行政院農業委員會動植物防疫檢疫局出版)檢索並轉錄,經
Ian review 後授權顯示(見 data/dosage_table.json 的 _source.verified_at)。
這不等於逐條的獸醫覆核 —— 手冊自己也講這些數字僅供參考,實際用藥仍應
以獸醫師處方或藥品標示為準,這句話跟這裡每一筆一起,永遠隨 medical_
disclaimer() 顯示在畫面上,不因為 verified:true 而被拿掉。

verified 欄位仍是防呆機制:草稿資料要有人明確按下這個開關才會生效,
不會因為手滑把資料貼進 json 就自動外流 —— 只是「查證」的標準從「逐條
核對手冊原文」放寬成「來源可信 + 人工授權顯示」,兩者差別必須在
data/dosage_table.json 的 sourceNote 裡誠實反映,尤其是靠推論而非手冊
逐字寫出的項目(如豬丹毒那筆)。

資料:data/dosage_table.json
"""

import json
import pathlib
import unicodedata
from typing import List, NamedTuple, Optional

DATA_PATH = pathlib.Path(__file__).parent.parent / "data" / "dosage_table.json"


class DosageEntry(NamedTuple):
    id: str
    disease_name: str
    drugs: List[dict]
    source_note: str


def _load():
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


_DATA = _load()


def _normalize(text: str) -> str:
    """全形轉半形、移除空白、統一小寫 —— 與 core/reportable.py 同一套規則,
    現場輸入格式不一致的問題兩邊都會遇到。
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(text.split())
    return text.lower()


def match_dosage_entries(
    question: str, entries: Optional[List[dict]] = None
) -> List[DosageEntry]:
    """依症狀關鍵字比對官方劑量對照表,只回傳已查證(verified=True)的項目。

    entries 參數只給測試用來注入假資料 —— 正式資料檔目前是空陣列,
    這裡若不能注入資料,比對邏輯本身就無從測試。留空則讀正式資料檔。
    """
    normalized = _normalize(question)
    if not normalized:
        return []

    source = _DATA.get("entries", []) if entries is None else entries

    matches = []
    for entry in source:
        if not entry.get("verified"):
            continue
        keywords = entry.get("keywords", [])
        if any(_normalize(kw) in normalized for kw in keywords):
            matches.append(DosageEntry(
                id=entry["id"],
                disease_name=entry["diseaseName"],
                drugs=entry.get("drugs", []),
                source_note=entry.get("sourceNote", ""),
            ))
    return matches
