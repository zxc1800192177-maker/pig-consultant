// v2 畫面渲染的純邏輯測試。
//
// 這些函式沒有測試時,錯的東西照樣畫得出來、看起來也很正常 ——
// 時間軸整整少了年份、標題寫「共 42 筆」卻只畫 40 列,都是這樣溜過去的。

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  TIMELINE_LIMIT,
  alertRow,
  boarPerformanceGrid,
  boarRow,
  buildAlerts,
  customTaskRow,
  customTaskSetting,
  describeEvent,
  eventName,
  eventRow,
  formatWeek,
  parityTone,
  pendingCheckRow,
  performanceGrid,
  shiftDate,
  statusPills,
  visibleEvents,
  sowRow,
  taskGroup,
  timelineCaption,
} from "../../web/lib/v2.js";

const ev = (over = {}) => ({ type: "MT", date: "2026-03-25", detail: {}, ...over });

describe("事件代碼轉中文", () => {
  it("認得的代碼換成中文", () => {
    assert.equal(eventName("FW"), "分娩");
    assert.equal(eventName("WN"), "離乳");
    assert.equal(eventName("MV"), "移欄");
    assert.equal(eventName("SC"), "採精");
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

  it("配種的發情穩定度顯示符號", () => {
    const text = describeEvent(ev({ type: "MT", detail: { estrus_stability: "stable" } }));
    assert.ok(text.includes("發情 ✓"));
  });

  it("沒評發情穩定度就不顯示", () => {
    assert.doesNotMatch(
      describeEvent(ev({ type: "MT", detail: { boar_tag: "D6" } })), /發情/);
  });

  it("認不得的發情穩定度值不顯示,也不會炸掉", () => {
    assert.doesNotThrow(() =>
      describeEvent(ev({ type: "MT", detail: { estrus_stability: "壞掉的值" } })));
    assert.doesNotMatch(
      describeEvent(ev({ type: "MT", detail: { estrus_stability: "壞掉的值" } })), /發情/);
  });

  it("沒有細節就回空字串", () => {
    assert.equal(describeEvent(ev()), "");
  });

  it("移欄顯示移去的欄位名稱(伺服器存的快照)", () => {
    const text = describeEvent(ev({
      type: "MV", detail: { pen_id: 5, pen_name: "配-01", zone: "mating" },
    }));
    assert.ok(text.includes("移至 配-01"));
  });

  it("採精顯示採精量、活力、濃度、劑量", () => {
    const text = describeEvent(ev({
      type: "SC", detail: { volume: 15, motility: 80, concentration: 3.5, doses: 3 },
    }));
    assert.ok(text.includes("採精量 15 ml"));
    assert.ok(text.includes("活力 80%"));
    assert.ok(text.includes("濃度 3.5 億/mL"));
    assert.ok(text.includes("3 劑"));
  });
});

describe("公豬清單的一列", () => {
  it("畫出耳號與品種", () => {
    const html = boarRow({ id: 1, earTag: "D6", breed: "Duroc" });
    assert.ok(html.includes("D6"));
    assert.ok(html.includes("Duroc"));
  });

  it("沒有品種顯示破折號", () => {
    assert.ok(boarRow({ id: 1, earTag: "D6" }).includes("—"));
  });

  it("耳號有跳脫", () => {
    const html = boarRow({ id: 1, earTag: "<img src=x>", breed: "Duroc" });
    assert.doesNotMatch(html, /<img/);
  });

  it("在場公豬不帶標記", () => {
    assert.doesNotMatch(boarRow({ id: 1, earTag: "D6", status: "active" }),
      /sow-exited-badge/);
    assert.doesNotMatch(boarRow({ id: 1, earTag: "D6" }), /sow-exited-badge/);
  });

  it("死亡的公豬帶「已死亡」標記 —— 沒有「淘汰」這個獨立狀態", () => {
    const html = boarRow({ id: 1, earTag: "D6-D115", status: "dead" });
    assert.match(html, /已死亡/);
    assert.match(html, /class="sow-row is-exited"/);
  });
});

describe("公豬卡的配種績效", () => {
  const perf = (over = {}) => ({
    matings: 12, sowsMated: 9, checked: 3, positiveRate: 66.7,
    litters: 5, avgBornAlive: 11.2, basis: "由母豬那邊的配種記錄比對耳號算出來,非 AI 生成",
    ...over,
  });

  it("沒有配種記錄就整區不畫", () => {
    assert.equal(boarPerformanceGrid(null), "");
  });

  it("驗孕陽性率的標籤直接寫出樣本數,不是只列一個百分比", () => {
    // 樣本數小時單看百分比容易誤讀成「這頭公豬配種成功率低」,
    // 其實只是很少被驗過
    const html = boarPerformanceGrid(perf());
    assert.ok(html.includes("驗孕陽性率(3 次)"));
    assert.ok(html.includes("67"));
  });

  it("沒有驗孕記錄時陽性率顯示破折號,不是 0%", () => {
    const html = boarPerformanceGrid(perf({ checked: 0, positiveRate: null }));
    assert.ok(html.includes("驗孕陽性率(0 次)"));
    assert.match(html, /v-none/);
  });

  it("沒有分娩記錄時平均活仔數顯示破折號", () => {
    const html = boarPerformanceGrid(perf({ litters: 0, avgBornAlive: null }));
    assert.match(html, /v-none/);
  });

  it("不分級 —— 沒有 tier 標籤", () => {
    assert.doesNotMatch(boarPerformanceGrid(perf()), /class="tier/);
  });

  it("說明文字來自後端,不是前端自己維護一份", () => {
    const html = boarPerformanceGrid(perf({ basis: "測試依據文字" }));
    assert.ok(html.includes("測試依據文字"));
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

describe("母豬清單裡的離群標記", () => {
  // 死亡/淘汰後這頭母豬還是要看得到、找得到,不能整個從畫面上消失,
  // 但也不能讓她看起來跟在場的母豬一樣正常。
  const row = (status) => sowRow({ id: 1, earTag: "019-D115", breed: "LY", parity: 3, status });

  it("在場母豬不帶標記", () => {
    assert.doesNotMatch(row("active"), /sow-exited-badge/);
    assert.doesNotMatch(row(undefined), /sow-exited-badge/);
  });

  it("淘汰的母豬帶「已淘汰」標記", () => {
    assert.match(row("culled"), /已淘汰/);
    assert.match(row("culled"), /class="sow-row is-exited"/);
  });

  it("死亡的母豬帶「已死亡」標記", () => {
    assert.match(row("dead"), /已死亡/);
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
  // 產房容量是設定裡的一個總數,configured=false 代表還沒設定。
  const data = (over = {}) => ({
    pens: { configured: true, total: 10, occupied: 0, free: 10,
            incoming: 0, short_by: 0 },
    openSows: [],
    ...over,
  });

  it("沒事就只有「還有空位」這則好消息", () => {
    const rows = buildAlerts(data());
    assert.equal(rows.length, 1);
    assert.equal(rows[0].tone, "ok");
  });

  it("產房不足排在最前面", () => {
    const rows = buildAlerts(data({
      pens: { configured: true, total: 10, occupied: 10, free: 0,
              incoming: 48, short_by: 48 },
      openSows: [{ earTag: "1013", days: 607 }],
    }));
    assert.equal(rows[0].title, "產房空間不足");
    assert.equal(rows[0].tone, "urgent");
  });

  it("還沒設定總產房數量時,不宣稱空間夠或不夠,而是提示去設定", () => {
    // 不知道容量就說不知道 —— 憑空給一個「空間不足」是捏造的警示
    const rows = buildAlerts({ pens: { configured: false, total: 0, occupied: 0,
                                       free: 0, incoming: 43, short_by: 0 },
                               openSows: [] });
    assert.equal(rows.length, 1);
    assert.ok(rows[0].title.includes("尚未設定"));
    assert.doesNotMatch(rows.map((r) => r.title).join(), /空間不足/);
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

  it("有空位時是好消息,不用急迫色", () => {
    const rows = buildAlerts(data({
      pens: { configured: true, total: 10, occupied: 3, free: 7,
              incoming: 0, short_by: 0 },
    }));
    assert.equal(rows[0].tone, "ok");
    assert.ok(rows[0].right.includes("7"));
  });

  it("缺漏欄位不會炸掉整頁", () => {
    assert.doesNotThrow(() => buildAlerts({}));
  });

  it("提醒列的文字有跳脫", () => {
    const html = alertRow({ tone: "urgent", title: "<b>x</b>", sub: "", right: "" });
    assert.doesNotMatch(html, /<b>/);
  });
});


describe("母豬目前狀態", () => {
  const status = (over = {}) => ({
    state: "pregnant", label: "懷孕中", dayLabel: "懷孕第 79 天",
    since: "2026-03-24", due: "2026-07-17", weanDue: null, moveInDue: null, ...over,
  });

  it("狀態、天數、預產期都畫出來", () => {
    const html = statusPills(status());
    assert.ok(html.includes("懷孕中"));
    assert.ok(html.includes("懷孕第 79 天"));
    assert.ok(html.includes("預產 7/17"));
  });

  it("預產日期縮成 M/D,不帶年份", () => {
    // 同一頭母豬的狀態都是近期的事,年份是雜訊
    assert.doesNotMatch(statusPills(status()), /2026-07-17/);
  });

  it("沒有預產期就不畫那顆膠囊", () => {
    assert.doesNotMatch(statusPills(status({ due: null })), /預產/);
  });

  it("哺乳中顯示預計離乳日", () => {
    const html = statusPills(status({
      state: "lactating", label: "哺乳中", dayLabel: "哺乳第 11 天",
      due: null, weanDue: "2026-05-12",
    }));
    assert.ok(html.includes("預計離乳 5/12"));
  });

  it("狀態帶自己的 class,配種待驗孕與懷孕中分得開", () => {
    // 兩者用同一個顏色的話,畫面等於宣稱一件還沒確認的事
    assert.match(statusPills(status()), /pill-pregnant/);
    assert.match(statusPills(status({ state: "mated", label: "配種待驗孕" })), /pill-mated/);
  });

  it("沒有狀態就什麼都不畫", () => {
    assert.equal(statusPills(null), "");
  });

  it("文字有跳脫", () => {
    assert.doesNotMatch(statusPills(status({ label: "<img src=x>" })), /<img/);
  });

  it("有指派欄位時顯示區域跟欄位名稱", () => {
    const html = statusPills(status({
      pen: { name: "配-01", zone: "mating", zoneLabel: "配種區" },
    }));
    assert.ok(html.includes("配種區"));
    assert.ok(html.includes("配-01"));
  });

  it("沒有指派欄位就不畫那顆膠囊", () => {
    assert.doesNotMatch(statusPills(status({ pen: null })), /配種區|待產區|產房/);
  });
});

describe("生產表現", () => {
  const metric = (over = {}) => ({
    key: "born_alive", label: "窩均活仔數", unit: "隻", digits: 1,
    value: 9.04, tier: "poor", tierLabel: "待改善", ...over,
  });
  const perf = (metrics) => ({
    litters: 7, basis: "由事件記錄計算,非 AI 生成 ・ 級距是與本場其他母豬比較,不是全國常模",
    metrics,
  });

  it("數值依 digits 取位", () => {
    assert.ok(performanceGrid(perf([metric()])).includes("9.0"));
    assert.ok(performanceGrid(perf([metric({
      key: "litters_per_year", digits: 2, value: 2.383 })])).includes("2.38"));
  });

  it("級距標籤畫出來", () => {
    const html = performanceGrid(perf([metric()]));
    assert.ok(html.includes("待改善"));
    assert.match(html, /t-poor/);
  });

  it("分不出級距時只顯示數字,不畫標籤", () => {
    // 空白比一個猜出來的「中等」誠實
    const html = performanceGrid(perf([metric({ tier: null, tierLabel: "" })]));
    assert.ok(html.includes("9.0"));
    assert.doesNotMatch(html, /class="tier/);
  });

  it("沒有數值顯示破折號,不顯示 0", () => {
    const html = performanceGrid(perf([metric({ value: null, tier: null, tierLabel: "" })]));
    assert.ok(html.includes("—"));
    assert.doesNotMatch(html, />0\.0</);
  });

  it("一定要印出級距的比較基準", () => {
    // 不寫的話讀起來就像系統在說這頭豬「全國待改善」
    assert.ok(performanceGrid(perf([metric()])).includes("不是全國常模"));
  });

  it("沒有表現資料就整區不畫", () => {
    assert.equal(performanceGrid(null), "");
  });

  it("標籤有跳脫", () => {
    const html = performanceGrid(perf([metric({ label: "<script>x</script>" })]));
    assert.doesNotMatch(html, /<script>/);
  });
});


describe("死胎集中在最早一胎的說明", () => {
  const perf = (note) => ({
    litters: 7, basis: "由事件記錄計算", note,
    metrics: [{ key: "stillborn_rate", label: "死胎率", unit: "%", digits: 1,
                value: 17.1, tier: "poor", tierLabel: "待改善" }],
  });

  it("有說明時畫出來", () => {
    const html = performanceGrid(perf("死胎幾乎全來自最早記錄的那一胎。"));
    assert.match(html, /class="flag"/);
    assert.ok(html.includes("最早記錄的那一胎"));
  });

  it("沒有說明就不畫空框", () => {
    assert.doesNotMatch(performanceGrid(perf("")), /class="flag"/);
  });

  it("說明文字有跳脫", () => {
    assert.doesNotMatch(performanceGrid(perf("<script>x</script>")), /<script>/);
  });
});


describe("預產日已過", () => {
  const status = (over = {}) => ({
    state: "mated", label: "配種待驗孕", dayLabel: "配種後 143 天",
    since: "2026-03-24", due: "2026-07-16", weanDue: null,
    overdueLabel: "", ...over,
  });

  it("還沒到期時照常顯示預產日", () => {
    assert.ok(statusPills(status()).includes("預產 7/16"));
  });

  it("已過期就不再寫「預產」—— 那是把過去的日期講成未來的計畫", () => {
    const html = statusPills(status({ overdueLabel: "預產日已過 29 天,尚無分娩記錄" }));
    assert.doesNotMatch(html, /預產 7\/16/);
    assert.ok(html.includes("預產日已過 29 天"));
    assert.match(html, /pill-overdue/);
  });
});


describe("驗孕事件的燈號", () => {
  const ev2 = (over = {}) => ({ type: "PD", date: "2026-05-30", detail: {}, ...over });

  it("陰性不是損失(紅),也不是好消息(綠) —— 獨立成 warn", () => {
    const html = eventRow(ev2({ detail: { positive: false } }));
    assert.match(html, /class="ev warn"/);
  });

  it("陽性算好消息,跟正常事件同一個綠點", () => {
    const html = eventRow(ev2({ detail: { positive: true } }));
    assert.match(html, /class="ev ok"/);
  });

  it("流產跟仔豬死亡、母豬死亡同樣是損失", () => {
    assert.match(eventRow({ type: "AB", date: "2026-05-30", detail: {} }), /class="ev loss"/);
  });
});

describe("時間軸裡的「還沒驗孕」提示", () => {
  const status = (over = {}) => ({
    state: "mated", label: "配種待驗孕", dayLabel: "配種後 143 天",
    pregCheckNote: "尚未驗孕,已超過建議驗孕時間(配種後 26 天)共 117 天",
    ...over,
  });

  it("配種待驗孕時畫出提示", () => {
    const html = pendingCheckRow(status());
    assert.match(html, /class="tl-pending"/);
    assert.ok(html.includes("117"));
  });

  it("已確認懷孕就不畫 —— 沒有這件事可提示", () => {
    assert.equal(pendingCheckRow(status({ state: "pregnant", pregCheckNote: "" })), "");
  });

  it("待配種狀態也不畫 —— 她已經驗過了", () => {
    assert.equal(pendingCheckRow(status({ state: "open", pregCheckNote: "" })), "");
  });

  it("沒有狀態就不畫", () => {
    assert.equal(pendingCheckRow(null), "");
  });

  it("文字有跳脫", () => {
    const html = pendingCheckRow(status({ pregCheckNote: "<script>x</script>" }));
    assert.doesNotMatch(html, /<script>/);
  });
});


describe("時間軸驗孕記錄一律保留", () => {
  // 實測 1183:47 筆事件、5 次驗孕陰性,已經超過原本的 40 筆上限。
  const mk = (n, type, dayOffset) => ({
    id: n, type, date: `2020-${String(1 + (dayOffset % 12)).padStart(2, "0")}-01`,
    detail: {},
  });

  it("一般事件超過上限時只留最新的部分", () => {
    const events = Array.from({ length: 50 }, (_, i) => mk(i, "MT", i));
    const shown = visibleEvents(events, 40);
    assert.equal(shown.length, 40);
  });

  it("驗孕記錄在名額內優先保留,不會被其他事件擠掉", () => {
    const events = [
      ...Array.from({ length: 45 }, (_, i) => mk(i, "MT", i)),
      ...Array.from({ length: 5 }, (_, i) => mk(100 + i, "PD", i)),
    ];
    const shown = visibleEvents(events, 40);
    const pdCount = shown.filter((e) => e.type === "PD").length;
    assert.equal(pdCount, 5, "5 筆驗孕記錄應該全部保留");
    assert.equal(shown.length, 40);   // 5 筆 PD + 35 筆其他,總數仍是上限
  });

  it("驗孕記錄比上限還多時,總數會超過上限 —— 一筆都不能丟", () => {
    const events = Array.from({ length: 45 }, (_, i) => mk(i, "PD", i));
    const shown = visibleEvents(events, 40);
    assert.equal(shown.length, 45);
  });

  it("其餘事件仍然只留最新的,不是全部混在一起顯示", () => {
    const events = [
      ...Array.from({ length: 50 }, (_, i) => ({
        id: i, type: "MT",
        date: `20${20 + Math.floor(i / 12)}-${String(1 + (i % 12)).padStart(2, "0")}-01`,
        detail: {},
      })),
    ];
    const shown = visibleEvents(events, 40);
    assert.equal(shown.length, 40);
    // 保留下來的應該是日期最新的 40 筆
    const kept = new Set(shown.map((e) => e.id));
    assert.ok(kept.has(49) && kept.has(10) && !kept.has(0));
  });

  it("由新到舊排序", () => {
    const events = [mk(1, "MT", 0), mk(2, "PD", 5), mk(3, "MT", 8)];
    const shown = visibleEvents(events, 40);
    const dates = shown.map((e) => e.date);
    assert.deepEqual(dates, [...dates].sort().reverse());
  });

  it("沒有驗孕記錄時行為跟原本一樣", () => {
    const events = Array.from({ length: 3 }, (_, i) => mk(i, "MT", i));
    assert.equal(visibleEvents(events, 40).length, 3);
  });
});


describe("自訂工作", () => {
  const task = (over = {}) => ({
    id: 7, name: "產房消毒", due: "2026-08-19", repeat: "weekly",
    repeatLabel: "每週", done: false, ...over,
  });

  it("畫出名稱、日期與重複方式", () => {
    const html = customTaskRow(task());
    assert.ok(html.includes("產房消毒"));
    assert.ok(html.includes("08-19"));
    assert.ok(html.includes("每週"));
  });

  it("勾選框帶著 id 跟日期 —— 重複性工作每一次發生各自標記", () => {
    // 只有 id 的話伺服器不知道要標哪一次
    const html = customTaskRow(task());
    assert.match(html, /data-task="7"/);
    assert.match(html, /data-due="2026-08-19"/);
  });

  it("已完成的打勾並淡化", () => {
    const html = customTaskRow(task({ done: true }));
    assert.match(html, /checked/);
    assert.match(html, /is-done/);
  });

  it("未完成的不打勾", () => {
    assert.doesNotMatch(customTaskRow(task()), /checked/);
  });

  it("名稱有跳脫", () => {
    assert.doesNotMatch(customTaskRow(task({ name: "<script>x</script>" })), /<script>/);
  });
});

describe("自訂工作的設定列", () => {
  const task = (over = {}) => ({
    id: 7, name: "產房消毒", startDate: "2026-08-19",
    repeat: "weekly", repeatLabel: "每週", ...over,
  });

  it("顯示重複方式與起始日", () => {
    const html = customTaskSetting(task());
    assert.ok(html.includes("每週"));
    assert.ok(html.includes("2026-08-19"));
  });

  it("刪除鈕帶得到 id", () => {
    assert.match(customTaskSetting(task()), /data-del-task="7"/);
  });

  it("名稱有跳脫", () => {
    assert.doesNotMatch(customTaskSetting(task({ name: "<img src=x>" })), /<img/);
  });
});
