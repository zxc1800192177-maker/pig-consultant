// v2 畫面渲染的純邏輯測試。
//
// 這些函式沒有測試時,錯的東西照樣畫得出來、看起來也很正常 ——
// 時間軸整整少了年份、標題寫「共 42 筆」卻只畫 40 列,都是這樣溜過去的。

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  TIMELINE_LIMIT,
  alertRow,
  buildAlerts,
  describeEvent,
  eventName,
  eventRow,
  formatWeek,
  parityTone,
  shiftDate,
  sowRow,
  taskGroup,
  timelineCaption,
} from "../../web/lib/v2.js";

const ev = (over = {}) => ({ type: "MT", date: "2026-03-25", detail: {}, ...over });

describe("事件代碼轉中文", () => {
  it("認得的代碼換成中文", () => {
    assert.equal(eventName("FW"), "分娩");
    assert.equal(eventName("WN"), "離乳");
  });

  it("不認得的代碼原樣顯示,不顯示 undefined", () => {
    assert.equal(eventName("ZZ"), "ZZ");
  });
});

describe("週次標籤", () => {
  it("只留月日", () => {
    assert.equal(formatWeek("2026-08-10", "2026-08-16"), "08/10 – 08/16");
  });
});

describe("日期加減", () => {
  it("往後一週", () => {
    assert.equal(shiftDate("2026-08-10", 7), "2026-08-17");
  });

  it("往前跨月", () => {
    assert.equal(shiftDate("2026-08-03", -7), "2026-07-27");
  });

  it("跨年不會算錯", () => {
    assert.equal(shiftDate("2025-12-29", 7), "2026-01-05");
  });

  it("不受時區影響 —— new Date('2026-08-10') 在某些時區會退成前一天", () => {
    assert.equal(shiftDate("2026-08-10", 0), "2026-08-10");
  });
});

describe("時間軸的年份", () => {
  // 實測 2580 這頭母豬有 42 筆事件、橫跨 2022 到 2026。每列只印
  // 「03-25」的話,牧場主根本看不出是哪一年的配種。
  const rows = [
    ev({ date: "2026-03-25" }),
    ev({ date: "2026-03-24" }),
    ev({ date: "2025-10-14" }),
    ev({ date: "2025-10-13" }),
  ];

  it("第一列一定帶年份", () => {
    assert.match(eventRow(rows[0], 0, rows), /tl-year">2026</);
  });

  it("同一年的後續列不重複印年份", () => {
    assert.doesNotMatch(eventRow(rows[1], 1, rows), /tl-year/);
  });

  it("跨到不同年時補上年份標題", () => {
    assert.match(eventRow(rows[2], 2, rows), /tl-year">2025</);
  });

  it("整串接起來,每個年份各出現一次", () => {
    const html = rows.map(eventRow).join("");
    assert.equal(html.match(/tl-year/g).length, 2);
    assert.ok(html.includes(">2026<") && html.includes(">2025<"));
  });

  it("單獨呼叫拿不到 index 時仍印年份,不顯示一個看不出年份的日期", () => {
    assert.match(eventRow(ev({ date: "2024-01-02" })), /tl-year">2024</);
  });

  it("日期格式壞掉不會炸,只是不印年份", () => {
    assert.doesNotThrow(() => eventRow(ev({ date: null })));
    assert.doesNotMatch(eventRow(ev({ date: "壞掉" })), /tl-year/);
  });
});

describe("時間軸標題", () => {
  it("沒截斷時不提「顯示最新」", () => {
    assert.equal(timelineCaption(12), "共 12 筆 ・ 最新在上");
  });

  it("被截斷時要講清楚只畫了幾筆", () => {
    // 原本寫死「共 42 筆」卻只畫 40 列,數得出來的人會以為系統漏資料
    const caption = timelineCaption(42, 40);
    assert.ok(caption.includes("42"), caption);
    assert.ok(caption.includes("40"), caption);
  });

  it("剛好等於上限不算截斷", () => {
    assert.equal(timelineCaption(TIMELINE_LIMIT, TIMELINE_LIMIT),
                 `共 ${TIMELINE_LIMIT} 筆 ・ 最新在上`);
  });
});

describe("事件細節整理", () => {
  it("分娩顯示活仔與死胎", () => {
    const text = describeEvent(ev({ type: "FW", detail: { born_alive: 12, stillborn: 2 } }));
    assert.ok(text.includes("活仔 12"));
    assert.ok(text.includes("死胎 2"));
  });

  it("活仔 0 要顯示,不可因為是 0 就被當成沒填", () => {
    assert.ok(describeEvent(ev({ type: "FW", detail: { born_alive: 0 } })).includes("活仔 0"));
  });

  it("死胎 0 不顯示 —— 那是常態,列出來只是雜訊", () => {
    assert.doesNotMatch(describeEvent(ev({ detail: { stillborn: 0 } })), /死胎/);
  });

  it("驗孕陰性與陽性都講明白", () => {
    assert.ok(describeEvent(ev({ type: "PD", detail: { positive: false } })).includes("陰性"));
    assert.ok(describeEvent(ev({ type: "PD", detail: { positive: true } })).includes("陽性"));
  });

  it("沒有細節就回空字串", () => {
    assert.equal(describeEvent(ev()), "");
  });
});

describe("胎次色階", () => {
  it("老母豬標紅", () => {
    assert.equal(parityTone(8), "par-r");
  });

  it("中段標黃", () => {
    assert.equal(parityTone(6), "par-y");
  });

  it("年輕標綠", () => {
    assert.equal(parityTone(2), "par-g");
  });

  it("未產過也算綠,不可因為 0 而變成未定義", () => {
    assert.equal(parityTone(0), "par-g");
  });
});

describe("跳脫", () => {
  // 匯入檔裡真的有帶中文字的耳號,誰知道別的牧場會匯入什麼
  it("耳號裡的角括號不會變成標籤", () => {
    const html = sowRow({ id: 1, earTag: '<img src=x onerror=alert(1)>', breed: "LY", parity: 2 });
    assert.doesNotMatch(html, /<img/);
  });

  it("事件細節同樣跳脫", () => {
    const html = eventRow(ev({ detail: { boar_tag: "<script>" } }));
    assert.doesNotMatch(html, /<script>/);
  });
});

describe("工作分組", () => {
  const group = (n) => ({
    kind: "wean", label: "離乳",
    tasks: Array.from({ length: n }, (_, i) => ({ sowId: i, earTag: `A${i}`, why: "哺乳滿 22 天" })),
  });

  it("標題帶頭數與共同理由", () => {
    const html = taskGroup(group(3), 0);
    assert.ok(html.includes("離乳"));
    assert.ok(html.includes("3 頭"));
    assert.ok(html.includes("哺乳滿 22 天"));
  });

  it("頭數不多時不收合 —— 為了三頭豬多點一次沒有意義", () => {
    assert.doesNotMatch(taskGroup(group(3), 0), /foldbtn/);
  });

  it("頭數多才收合,且展開鈕要寫出總數", () => {
    const html = taskGroup(group(30), 0);
    assert.match(html, /foldbtn/);
    assert.ok(html.includes("展開全部 30 頭"));
  });

  it("每頭豬都是可點的耳號按鈕,帶得到 sowId", () => {
    const html = taskGroup(group(2), 0);
    assert.equal(html.match(/class="etag"/g).length, 2);
    assert.ok(html.includes('data-sow="0"'));
  });
});

describe("提醒排序與內容", () => {
  const data = (over = {}) => ({
    pens: { free: [], incoming: 0, short_by: 0 },
    openSows: [],
    ...over,
  });

  it("沒事就沒有提醒", () => {
    assert.deepEqual(buildAlerts(data()), []);
  });

  it("產房不足排在最前面", () => {
    const rows = buildAlerts(data({
      pens: { free: [], incoming: 48, short_by: 48 },
      openSows: [{ earTag: "1013", days: 607 }],
    }));
    assert.equal(rows[0].title, "產房空間不足");
    assert.equal(rows[0].tone, "urgent");
  });

  it("逾期未配種列出頭數與最久天數", () => {
    const rows = buildAlerts(data({
      openSows: [{ earTag: "1013", days: 607 }, { earTag: "1412", days: 400 }],
    }));
    assert.ok(rows[0].title.includes("2 頭"));
    assert.ok(rows[0].right.includes("607"));
  });

  it("後端用 ear_tag 或 earTag 都讀得到 —— 兩種命名實際都出現過", () => {
    const snake = buildAlerts(data({ openSows: [{ ear_tag: "1013", days: 10 }] }));
    const camel = buildAlerts(data({ openSows: [{ earTag: "1013", days: 10 }] }));
    assert.ok(snake[0].sub.includes("1013"));
    assert.ok(camel[0].sub.includes("1013"));
  });

  it("有空欄時是好消息,不用急迫色", () => {
    const rows = buildAlerts(data({ pens: { free: [{ name: "A-01" }], incoming: 0, short_by: 0 } }));
    assert.equal(rows[0].tone, "ok");
  });

  it("缺漏欄位不會炸掉整頁", () => {
    assert.doesNotThrow(() => buildAlerts({}));
  });

  it("提醒列的文字有跳脫", () => {
    const html = alertRow({ tone: "urgent", title: "<b>x</b>", sub: "", right: "" });
    assert.doesNotMatch(html, /<b>/);
  });
});
