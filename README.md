# 豬豬顧問 v2

用 SDD(規格驅動開發)+ TDD(測試驅動開發)重做的版本。

## 兩個方法的差別

**SDD** 決定寫什麼:憲法 → 規格 → 技術計畫 → 實作,每關確認過才往下。
**TDD** 決定怎麼寫:先寫一個會失敗的測試 → 寫最少的程式讓它過 → 整理 → 重複。

為什麼這個專案特別需要 TDD:分級演算法算錯,牧場主會照著錯的數字淘汰母豬,
而且**從畫面上看不出來哪裡錯**。測試先寫,才能保證這件事不會發生。

## 跑測試

```bash
python -m pytest tests/ -q
```

預設不跑需要 AI 或網路的測試。全部跑:

```bash
python -m pytest tests/ -q -m ""
```

## 目前進度

| 階段 | 檔案 | 狀態 |
|---|---|---|
| 憲法 | [specs/constitution.md](specs/constitution.md) | 完成(9 條) |
| 規格 | [specs/spec.md](specs/spec.md) | 完成 v0.2 |
| 常模資料 | [specs/reference/benchmark-2025.md](specs/reference/benchmark-2025.md) | 完成 |
| 通報清單 | [specs/reference/reportable-diseases.md](specs/reference/reportable-diseases.md) | 草稿,**待專家覆核** |
| 技術計畫 | [specs/plan.md](specs/plan.md) | 完成 |
| 分級演算法 | `core/grading.py` | 完成 |
| 常模載入 | `core/benchmark.py` | 完成 |
| 通報偵測 | `core/reportable.py` | 完成 |
| 輸入驗證 | `core/metrics.py` | 完成 |
| 弱項排序 | `core/diagnosis.py` | 完成(權重待覆核) |
| 架構守衛 | `tests/test_core_purity.py` | 完成 |
| **核心層小計** | | **300 測試通過** |
| AI 層 | `ai/consultant.py` | 未開始 |
| HTTP 層 | `server.py` | 未開始 |
| 前端 + 前端測試 | `web/`、`tests/js/` | 未開始 |

### 已達成的規格成功條件

- ✅ 條件 1:合億畜牧場 18 項評級與官方報告完全一致(`test_acceptance_holding_farm.py`)
- ✅ 條件 3:純計算邏輯有自動化測試,不需網路或 AI
- ✅ 條件 5:通報關鍵字有對應測試,驗證命中必觸發、不命中不誤觸發

### 測試抓到的實際 bug

**年產胎數一致性檢查的公式寫錯。** 原本用 `365.25 / (哺乳+懷孕)`,
漏掉年報定義中「扣除非生產天數」那一段。拿合億畜牧場真實資料驗算:

| | 推導值 | 與填報值 2.25 的落差 |
|---|---|---|
| 錯誤公式 | 2.6876 | 16.3% → **誤報矛盾** |
| 年報公式 `(365.25−NPD)/(哺乳+懷孕)` | 2.2462 | 0.17% → 正確 |

後果是每一份填寫正確的表單都會跳假警告。已修正,並加上以真實資料為基準的迴歸測試。

### 待覆核事項

- `core/diagnosis.py` 的影響權重與上游關係表為暫定值,依年報文字描述推導,
  尚未經領域專家確認。結果帶 `provisional=True`,畫面須標示。
- `data/reportable_diseases.json` 僅涵蓋三大豬病,非完整甲乙丙類清單。

## 架構重點

### 純度守衛

`core/` 是純計算層,**禁止** import AI、網路、子行程、`random`、`time`、`datetime`。
這條規則不是靠人記得,而是由 `tests/test_core_purity.py` 強制執行 —— 違反會讓測試失敗。

理由是憲法第二條:確定性的計算不得經過 AI。同樣的輸入必須永遠得到同樣的級距。

### 單一職責(SRP)與依賴方向

每個模組只有一個「會需要修改它」的理由:

| 模組 | 唯一職責 | 什麼情況才需要改它 |
|---|---|---|
| `coercion.py` | 把原始輸入轉成數字 | 輸入來源改變(表單 → PDF → API) |
| `benchmark.py` | 常模資料存取 | 資料檔格式改變 |
| `grading.py` | A~F 分級演算法 | 年報的分級規則改變 |
| `metrics.py` | 什麼數值算合理 | 領域的合理範圍改變 |
| `diagnosis.py` | 弱項排序邏輯 | 排序方式改變 |
| `labels.py` | 產生給人看的文字 | 顯示措辭改變 |

依賴方向由 `test_core_purity.py::test_dependency_direction` 鎖住:

```
coercion ─┐
          ├─→ metrics
benchmark ─┼─→ diagnosis
grading ──┴─→ labels
```

**資料層不得依賴呈現層。** `benchmark.py` 反過來 import `labels.py` 會讓測試失敗 ——
否則改一句顯示文字就得動到資料存取模組,那正是拆開它們要避免的事。

新增 core 模組時必須在 `ALLOWED_DEPENDENCIES` 宣告它可以依賴誰,
否則測試會擋下來,強迫先想清楚這個模組的職責。

### 領域知識集中在資料檔

影響權重與上游關係都放在 `data/benchmark_2025.json`,不寫死在程式碼裡。
這兩節都需要領域專家覆核 —— 分散在程式碼與資料檔兩處會讓專家漏看其中一邊。

資料只單向流動:

```
使用者數字 → core/ 計算 → 級距與弱項 ─┬─→ 顯示(標示「計算結果」)
                                      └─→ 當背景資訊送進 ai/ → 建議(標示「AI 生成」)
```

## v0.1 範圍與已知限制

因應「幾天內要交」的時限,以下確定延後,但架構已預留位置:

| 延後 | 預留方式 |
|---|---|
| PDF 上傳自動解析 | 輸入層抽象,v0.1 只有手動輸入實作 |
| 歷史紀錄、逐年比較 | 結果封裝成可序列化物件,暫不落地 |
| 對外上線 | AI 呼叫封裝在單一模組 |

⚠️ **對外上線前必須改用 API 計費。** 個人 claude.ai 訂閱額度僅供本人使用,
拿來驅動服務外部客戶的產品違反使用條款,有帳號停權風險。

## 與 v1 的關係

`../pig-consultant-ai` 是可運作的 v1(疾病諮詢,CLI 串流),v2 完成前不動,仍可 demo。
