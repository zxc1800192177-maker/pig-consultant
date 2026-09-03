// 生產性能趨勢報告畫面渲染的純邏輯測試。
//
// 這裡不重新驗證趨勢怎麼算(那是 tests/test_trend.py 的事),只驗證
// 「後端給的資料畫得對不對」:沒有記錄印 —— 不是 0,箭頭方向跟著數字
// 正負、顏色跟著 improved(兩者可能相反,例如死胎率上升該是紅色的 ▲)。

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  EXPORT_LOOKBACK_PERIODS,
  GRAIN_LABELS,
  changeText,
  changeTone,
  trendCsv,
  trendCsvFileName,
  trendReportGrid,
  trendSectionTable,
  trendValue,
  widestStart,
} from "../../web/lib/trendreport.js";

function parseCsv(text) {
  const body = text.slice(1).replace(/\r\n$/, "");
  return body.split("\r\n").map((line) => line.split(","));
}

const PERIODS = [
  { key: "2025", label: "2025 年", start: "2025-01-01", end: "2025-12-31" },
  { key: "2026", label: "2026 年", start: "2026-01-01", end: "2026-08-31" },
];

const REPORT = {
  periods: PERIODS,
  sections: [
    {
      key: "farrowing", label: "分娩",
      rows: [
        { key: "alive_per_litter", label: "窩均活仔數", unit: "隻", digits: 1,
          better: "high", values: [11.0, 12.2],
          change: { delta: 1.2, pct: 10.9, improved: true } },
        { key: "stillborn_pct", label: "死胎率", unit: "%", digits: 1,
          better: "low", values: [7.0, 9.4],
          change: { delta: 2.4, pct: 34.3, improved: false } },
        { key: "gestation_days", label: "懷孕天數", unit: "天", digits: 1,
          better: null, values: [113.9, null], change: null },
      ],
    },
  ],
};

describe("trendValue", () => {
  it("有數字就格式化成指定小數位加單位", () => {
    assert.equal(trendValue(12.345, 1, "隻"), '12.3<span class="u">隻</span>');
  });

  it("沒有記錄印 —,不是 0", () => {
    // 0 是「這期真的一頭都沒死」,None 是「沒有人記」—— 混在一起會讓
    // 趨勢圖憑空多出一個谷底(憲法第三條)。
    assert.match(trendValue(null, 1, "隻"), /—/);
    assert.match(trendValue(undefined, 1, "隻"), /—/);
    assert.doesNotMatch(trendValue(0, 0, "隻"), /—/);
  });
});

describe("changeTone / changeText", () => {
  it("improved 決定顏色,不是數字的正負", () => {
    assert.equal(changeTone({ delta: 1.2, pct: 10, improved: true }), "good");
    assert.equal(changeTone({ delta: 2.4, pct: 34, improved: false }), "critical");
  });

  it("沒有 change 就是中性", () => {
    assert.equal(changeTone(null), "neutral");
    assert.equal(changeTone({ delta: 0, pct: 0, improved: null }), "neutral");
  });

  it("箭頭跟著數字正負,不是跟著 improved", () => {
    // 死胎率上升是壞事(improved:false),但箭頭仍然要向上 ——
    // 顏色負責告訴使用者「這是壞事」,箭頭只負責告訴使用者「漲了」。
    // 顏色掩蓋方向的話,使用者反而看不出數字實際往哪裡動。
    const worse = changeText({ delta: 2.4, pct: 34.3, improved: false }, "%", 1);
    assert.match(worse, /^▲/);
    const better = changeText({ delta: -2.4, pct: -34.3, improved: true }, "%", 1);
    assert.match(better, /^▼/);
  });

  it("正數帶正號,百分比一起顯示", () => {
    const text = changeText({ delta: 1.2, pct: 10.9, improved: true }, "隻", 1);
    assert.equal(text, "▲ +1.2隻 (+11%)");
  });

  it("完全沒變化用 → 而不是 ▲/▼", () => {
    assert.match(changeText({ delta: 0, pct: 0, improved: null }, "隻", 1), /^→/);
  });

  it("沒有 change 回空字串,不是丟例外", () => {
    assert.equal(changeText(null, "隻", 1), "");
  });
});

describe("trendSectionTable", () => {
  const html = trendSectionTable(REPORT.sections[0], PERIODS);

  it("標題列是指標 + 每個期間 + 變化", () => {
    assert.match(html, /<th>指標<\/th>/);
    assert.match(html, /<th>2025 年<\/th>/);
    assert.match(html, /<th>2026 年<\/th>/);
    assert.match(html, /<th>變化<\/th>/);
  });

  it("每個指標一列,值跟變化都畫出來", () => {
    assert.match(html, /窩均活仔數/);
    assert.match(html, /12\.2<span class="u">隻<\/span>/);
    assert.match(html, /tone-good/);
    assert.match(html, /tone-critical/);
  });

  it("算不出變化的那一列不假裝有變化", () => {
    // 懷孕天數那一列 change 是 null(2026 那期沒有記錄),不該印出
    // 箭頭或百分比,只能是「—」。
    const rows = html.split("<tr>");
    const gestationRow = rows.find((r) => r.includes("懷孕天數"));
    assert.ok(gestationRow);
    assert.match(gestationRow, /<td><span class="v-none">—<\/span><\/td>\s*<\/tr>/);
  });
});

describe("trendReportGrid", () => {
  it("每個區段各自成表", () => {
    const html = trendReportGrid(REPORT);
    assert.equal((html.match(/section-label/g) || []).length, 1);
    assert.match(html, /分娩/);
  });

  it("沒有任何區段時講清楚是沒有記錄,不是印一堆空表格", () => {
    const html = trendReportGrid({ periods: PERIODS, sections: [] });
    assert.match(html, /沒有記錄/);
    assert.doesNotMatch(html, /<table>/);
  });
});

describe("trendCsv", () => {
  it("一列一個指標,一欄一個期間,單位跟區段都在", () => {
    const rows = parseCsv(trendCsv(REPORT));
    assert.deepEqual(rows[0],
      ["區段", "指標", "單位", "2025 年", "2026 年", "變化(絕對值)", "變化(%)"]);
    const alive = rows.find((r) => r[1] === "窩均活仔數");
    assert.deepEqual(alive, ["分娩", "窩均活仔數", "隻", "11", "12.2", "1.2", "10.9"]);
  });

  it("沒有記錄的儲存格是空白,不是 0 或 null 字樣", () => {
    const rows = parseCsv(trendCsv(REPORT));
    const gestation = rows.find((r) => r[1] === "懷孕天數");
    assert.equal(gestation[4], "");    // 2026 那一欄
    assert.equal(gestation[5], "");    // 變化(絕對值)
  });

  it("沒有任何區段也不會爆掉", () => {
    assert.ok(trendCsv({ periods: PERIODS, sections: [] }).length > 0);
  });
});

describe("trendCsvFileName", () => {
  it("期別轉成中文,帶著起訖日期", () => {
    assert.equal(trendCsvFileName("month", "2025-09-01", "2026-08-31"),
      "豬豬顧問-生產趨勢月報-2025-09-01~2026-08-31.csv");
  });

  it("認不得的期別就用原始字串,不讓檔名整個消失", () => {
    assert.match(trendCsvFileName("fortnight", "2025-01-01", "2025-02-01"),
      /fortnight/);
  });
});

describe("GRAIN_LABELS", () => {
  it("四種期別都有中文名字", () => {
    assert.deepEqual(GRAIN_LABELS, { week: "週", month: "月", quarter: "季", year: "年" });
  });
});

describe("widestStart", () => {
  it("往回推得夠遠,不會不小心只抓到幾期", () => {
    const start = widestStart("2026-08-31", "month");
    const days = (new Date("2026-08-31") - new Date(start)) / 86400000;
    // 抓得比 EXPORT_LOOKBACK_PERIODS 期還寬裕,但沒有寬到荒謬(例如上百年)
    assert.ok(days > 30 * (EXPORT_LOOKBACK_PERIODS - 10));
    assert.ok(days < 30 * (EXPORT_LOOKBACK_PERIODS + 10));
  });

  it("不同期別各自用自己的天數換算", () => {
    const week = widestStart("2026-08-31", "week");
    const year = widestStart("2026-08-31", "year");
    // 年別的回推天數遠比週別長 —— 同樣是「最多看這麼多期」,一年一期
    // 涵蓋的時間本來就比一週一期長得多。
    const weekDays = (new Date("2026-08-31") - new Date(week)) / 86400000;
    const yearDays = (new Date("2026-08-31") - new Date(year)) / 86400000;
    assert.ok(yearDays > weekDays * 10);
  });
});
