# 豬豬顧問 — 技術計畫

版本 1.0 · 2026-08-08 · 對應規格 v0.2

> 本文描述**怎麼做**。受 `constitution.md` 約束,實作 `spec.md` 所定義的行為。

---

## 1. v0.1 範圍

時間限制:幾天內。因此以下功能**確定延後**,但架構須預留:

| 延後項目 | 預留方式 |
|---|---|
| PDF 上傳自動解析 | 輸入層抽象成 `parse_farm_report()`,v0.1 只有手動輸入的實作 |
| 歷史紀錄與逐年比較 | 健檢結果封裝成可序列化物件,v0.1 不落地儲存 |
| 對外上線 | AI 呼叫封裝在單一模組,換成 API 計費時只改該模組 |

v0.1 交付:疾病諮詢 + 生產指標分析,手動輸入、不存檔、僅本機。

## 2. 技術選型

| 層 | 選擇 | 理由 |
|---|---|---|
| 後端 | Python 3.9 標準庫 | v1 已驗證可行;零相依,免安裝環境 |
| 測試 | pytest 7.1.2 | 環境已有 |
| 前端 | 原生 HTML/CSS/JS | v1 已驗證;無建置流程,改完直接跑 |
| AI | 本機 claude CLI,`--output-format stream-json` | 憲法第五條 |
| 資料 | JSON 檔 | 常模資料是靜態的,不需要資料庫 |

不引入框架。v0.1 規模不足以攤平框架的學習與維護成本。

## 3. 架構:計算與生成分離

憲法第二條要求確定性計算不得經過 AI。架構上用**目錄邊界**強制執行:

```
pig-consultant-v2/
├── core/                  純計算層 — 禁止 import 任何 AI、網路、子行程
│   ├── grading.py         A~F 分級演算法
│   ├── metrics.py         指標定義、合理範圍、跨欄一致性
│   ├── reportable.py      法定傳染病關鍵字比對
│   ├── benchmark.py       常模資料載入與查詢
│   └── diagnosis.py       弱項排序(落後程度 × 對 PSY 的影響)
├── ai/
│   └── consultant.py      CLI 呼叫封裝 — 唯一與 AI 溝通的模組
├── data/
│   ├── benchmark_2025.json
│   └── reportable_diseases.json
├── web/
│   ├── index.html
│   └── app.js
├── tests/
│   ├── test_grading.py
│   ├── test_metrics.py
│   ├── test_reportable.py
│   └── test_diagnosis.py
├── server.py              HTTP 層 — 只做路由與驗證,不放商業邏輯
└── config.py              用量保護參數等設定
```

**強制規則:** `core/` 底下任何模組不得 import `ai/`、`subprocess`、`urllib`、`socket`。
此規則本身要有測試(`test_core_purity`),不靠人為記得。

資料流向(憲法第二條第五款,單向):

```
使用者數字 → core/ 計算 → 級距與弱項 ─┬─→ 直接顯示(標示「計算結果」)
                                      └─→ 當作背景資訊送進 ai/ → 建議(標示「AI 生成」)
```

AI 的輸出**不得**回流進 `core/`。

## 4. 資料格式

`data/benchmark_2025.json`:

```json
{
  "source": { "name": "豬隻生產指標年報", "year": 2025, "farms": 110,
              "publisher": "PigCHAMP × PMMT 生產醫學管理團隊" },
  "bands": [[10,"A"],[25,"B"],[50,"C"],[75,"D"],[90,"E"]],
  "metrics": [
    { "key": "psy", "name": "母豬年產離乳仔豬數", "unit": "隻",
      "gradable": true, "sample_size": 110,
      "mean": 21.52, "sd": 2.770,
      "percentiles": [25.65, 23.22, 21.11, 19.11, 18.24],
      "range": [5, 35],
      "definition": "…", "improvement": "…" }
  ]
}
```

`percentiles` 一律由**最佳到最差**排列。指標方向從 `percentiles[0] > percentiles[-1]` 推導,不另外儲存,避免兩處資料不一致。

`gradable: false` 者為規模型指標,只顯示對照不評級。

## 5. 核心演算法

已用合億畜牧場 18 項實際評級驗證通過(見 `reference/benchmark-2025.md`)。

```python
def grade(value, percentiles, bands):
    higher_better = percentiles[0] > percentiles[-1]
    for cut, letter in bands:
        better = value > percentiles[i] if higher_better else value < percentiles[i]
        if better:
            return letter
    return "F"
```

邊界採**嚴格不等式**:值等於切點時歸較差一級。

## 6. 弱項排序

規格 US-4 要求「依落後程度 × 對 PSY 的影響」排序,而非只按級距。

```
落後分數 = 級距序位 (A=0 … F=5)
影響權重 = 該指標對 PSY 的影響層級
優先度   = 落後分數 × 影響權重
```

影響權重依年報定義的因果鏈:

| 層級 | 權重 | 指標 |
|---|---|---|
| 直接構成 PSY | 3 | 母豬年產胎數、母豬平均離乳仔豬數 |
| 上游驅動 | 2 | 窩均活仔數、離乳前死亡率、分娩率、母豬非生產天數 |
| 更上游 | 1 | 窩均總仔數、重發情配種佔比、離乳至第一次配種間隔 |

同分時,級距差的排前面。權重表放在 `data/`,不寫死在程式裡。

## 7. AI 呼叫

沿用 v1 已驗證的方式:

```
claude -p --system-prompt <人格> --model sonnet --strict-mcp-config
       --settings '{"permissions":{"deny":[…全部工具…]}}'
       --output-format stream-json --include-partial-messages --verbose
```

問題由 stdin 傳入,不放命令列參數。

**工具封鎖清單是安全邊界(憲法第四條),必須有測試驗證其生效。**

## 8. 用量保護(憲法第九條)

`config.py`:

| 參數 | 預設 | 作用 |
|---|---|---|
| `MAX_QUESTION_CHARS` | 2000 | 單題字數上限 |
| `MIN_REQUEST_INTERVAL_SEC` | 3 | 同一 session 連續請求最短間隔 |
| `AI_TIMEOUT_SEC` | 180 | 逾時上限 |

超過間隔限制回 429,訊息說明原因。連續失敗時提示「可能是訂閱額度限制」。

## 9. 測試策略

| 層 | 測法 | 需不需要 AI/網路 |
|---|---|---|
| `core/` | 單元測試,含 18 項真實資料驗證案例 | 否 |
| 純度檢查 | 掃描 `core/` 的 import | 否 |
| 工具封鎖 | 送出誘導 AI 使用工具的提示,驗證被拒 | 是(標記為 slow) |
| HTTP 層 | 輸入驗證、錯誤碼、限流 | 否(AI 層以假物件替代) |

預設測試指令不得依賴 AI:

```bash
pytest tests/ -m "not slow"
```

## 10. 實作順序(TDD)

先寫測試,再寫實作。順序依「錯了最嚴重」排:

1. `test_grading` → `core/grading.py`(算錯會導致錯誤經營決策)
2. `test_core_purity` → 目錄邊界(架構腐化的第一道防線)
3. `test_reportable` → `core/reportable.py`(漏報法定傳染病)
4. `test_metrics` → `core/metrics.py`(擋下不合理輸入)
5. `test_diagnosis` → `core/diagnosis.py`(弱項排序)
6. HTTP 層與前端(此層以手動驗證為主)

---

## 修訂紀錄

| 版本 | 日期 | 變更 |
|---|---|---|
| 1.0 | 2026-08-08 | 初版 |
