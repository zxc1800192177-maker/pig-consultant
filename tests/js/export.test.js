// 匯出的純邏輯測試。
//
// 最要緊的一條是「不掉東西」:匯出檔少一欄,使用者拿到的是一份看起來
// 完整、其實缺角的資料,而他沒有辦法發現。所以 detailColumns 對認不得
// 的鍵也要開一欄,那條測試不能刪。

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  BOM,
  EXTRA_LABELS,
  INTERNAL_KEYS,
  backupJson,
  boarCsv,
  boarEventCsv,
  csvCell,
  detailColumns,
  detailValue,
  detailValueLabels,
  eventCsv,
  exportFileName,
  exportSummary,
  sowCsv,
  toCsv,
} from "../../web/lib/export.js";
import { RECORD_FORMS } from "../../web/lib/record.js";

/** 解析回二維陣列,才能斷言「第幾列第幾欄是什麼」而不是比對整串文字。 */
function parseCsv(text) {
  assert.equal(text[0], BOM, "開頭必須有 BOM,否則 Excel 開出來是亂碼");
  const body = text.slice(1).replace(/\r\n$/, "");
  const rows = [];
  let row = [];
  let cell = "";
  let quoted = false;

  for (let i = 0; i < body.length; i += 1) {
    const c = body[i];
    if (quoted) {
      if (c === '"' && body[i + 1] === '"') { cell += '"'; i += 1; }
      else if (c === '"') quoted = false;
      else cell += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") { row.push(cell); cell = ""; }
    else if (c === "\r" && body[i + 1] === "\n") {
      row.push(cell); rows.push(row); row = []; cell = ""; i += 1;
    } else cell += c;
  }
  row.push(cell);
  rows.push(row);
  return rows;
}

const PAYLOAD = {
  farmName: "測試場",
  exportedAt: "2026-08-29",
  sows: [
    { id: 1, earTag: "2580", breed: "LY", parity: 3, status: "active",
      entryDate: "2024-03-01", birthDate: "2023-08-10",
      sireTag: "B1", damTag: "M9", isUnknown: false },
    { id: 2, earTag: "不明-0817", breed: "", parity: 0, status: "active",
      entryDate: null, birthDate: null, sireTag: "", damTag: "", isUnknown: true },
  ],
  boars: [
    { id: 7, earTag: "B1", breed: "D", status: "active",
      entryDate: "2023-01-05", sireTag: "", damTag: "" },
  ],
  events: [
    { id: 20, sowId: 1, earTag: "2580", type: "WN", date: "2026-05-25",
      detail: { weaned: 11, wean_score: 4, hernia: true }, excluded: false },
    { id: 10, sowId: 1, earTag: "2580", type: "MT", date: "2026-01-10",
      detail: { boar_tag: "B1", estrus_stability: "unstable" }, excluded: false },
    { id: 15, sowId: 1, earTag: "2580", type: "MV", date: "2026-04-20",
      detail: { zone: "farrowing", pen_name: "A-12" }, excluded: false },
    { id: 18, sowId: 2, earTag: "不明-0817", type: "FW", date: "2026-05-04",
      detail: { born_alive: 56 }, excluded: true },
  ],
  boarEvents: [
    { id: 90, boarId: 7, earTag: "B1", type: "SC", date: "2026-02-02",
      detail: { volume: 250, motility: 80 } },
  ],
  marketDeaths: [
    { id: 5, date: "2026-03-03", type: "MKD", earTag: "",
      detail: { reason: "熱緊迫", weight_kg: 92.5 } },
  ],
};

describe("csvCell", () => {
  it("原樣輸出不需要處理的值", () => {
    assert.equal(csvCell("2580"), "2580");
    assert.equal(csvCell(11), "11");
  });

  it("沒有值就是空白,不補 0 也不補 undefined", () => {
    assert.equal(csvCell(undefined), "");
    assert.equal(csvCell(null), "");
  });

  it("布林值用中文,不是 true/false", () => {
    assert.equal(csvCell(true), "是");
    assert.equal(csvCell(false), "否");
  });

  it("含逗號、引號、換行的內容用引號包起來", () => {
    assert.equal(csvCell("壓死,體弱"), '"壓死,體弱"');
    assert.equal(csvCell('他說"沒事"'), '"他說""沒事"""');
    assert.equal(csvCell("第一行\n第二行"), '"第一行\n第二行"');
  });

  it("擋住 Excel 公式注入", () => {
    // 使用者自己打的原因欄位是自由文字,而這個檔案會在別人的電腦上打開。
    assert.equal(csvCell("=1+1"), "'=1+1");
    assert.equal(csvCell("@SUM(A1)"), "'@SUM(A1)");
  });

  it("純數字不加引號 —— 負數不該多一個看得見的符號", () => {
    assert.equal(csvCell("-3"), "-3");
    assert.equal(csvCell(-3.5), "-3.5");
  });
});

describe("toCsv", () => {
  it("開頭是 BOM,行尾是 CRLF", () => {
    const text = toCsv(["甲", "乙"], [[1, 2]]);
    assert.equal(text, `${BOM}甲,乙\r\n1,2\r\n`);
  });

  it("沒有資料時仍然有標題列", () => {
    // 空的表格打開來看得到欄位,才知道自己是真的沒資料,不是匯出壞了。
    assert.deepEqual(parseCsv(toCsv(["甲"], [])), [["甲"]]);
  });
});

describe("detailColumns", () => {
  // 每一個「記在既有動物身上」的欄位都填一次,用來檢查順序與標題。
  // 種豬進場(target: "new")刻意排除 —— 那些欄位寫進母豬表,不在
  // 事件的 detail 裡,下面有一條測試專門盯這件事。
  const ALL = [{ detail: Object.fromEntries(
    [...Object.values(RECORD_FORMS)
       .filter((spec) => spec.target !== "new")
       .flatMap((spec) => spec.fields.map((f) => f.key)),
     ...Object.keys(EXTRA_LABELS)].map((k) => [k, 1])) }];

  it("依生產週期排序:配種在分娩前,分娩在離乳前", () => {
    const keys = detailColumns(ALL).map((c) => c.key);
    assert.ok(keys.indexOf("boar_tag") < keys.indexOf("born_alive"));
    assert.ok(keys.indexOf("born_alive") < keys.indexOf("weaned"));
  });

  it("標題用表單上的名字,不另外抄一份", () => {
    const byKey = Object.fromEntries(detailColumns(ALL).map((c) => [c.key, c.label]));
    const wean = RECORD_FORMS.WN.fields.find((f) => f.key === "wean_score");
    assert.equal(byKey.wean_score, wean.label);
  });

  it("同一個鍵只出現一欄", () => {
    const keys = detailColumns(ALL).map((c) => c.key);
    assert.equal(new Set(keys).size, keys.length);
  });

  it("種豬進場的欄位不列入 —— 那些寫進母豬表,不在事件的 detail 裡", () => {
    const keys = detailColumns(ALL).map((c) => c.key);
    assert.ok(!keys.includes("earTag"));
    assert.ok(!keys.includes("birthDate"));
  });

  it("匯入才有的舊欄位也留著位置", () => {
    const keys = detailColumns(ALL).map((c) => c.key);
    for (const key of Object.keys(EXTRA_LABELS)) assert.ok(keys.includes(key), key);
  });

  it("只開資料裡真的用到的欄位", () => {
    // 全部列出來的話,公豬那張表會拖著「死胎」「離乳頭數」一路到最右邊
    // 全是空的 —— 要看的那幾欄反而找不到。
    const keys = detailColumns([{ detail: { volume: 250, motility: 80 } }])
      .map((c) => c.key);
    assert.deepEqual(keys, ["volume", "motility"]);
  });

  it("沒有資料就沒有欄位,不是一整排空白", () => {
    assert.deepEqual(detailColumns([]), []);
  });

  it("資料庫內部的流水號不列 —— 表格是給人看的", () => {
    // 移欄同時存了 pen_id 與 pen_name,後者才是牧場寫在欄位上的編號。
    // 備份那一份仍然原封不動,見 backupJson。
    const cols = detailColumns([{ detail: { pen_id: 12, pen_name: "A-12" } }]);
    const keys = cols.map((c) => c.key);
    assert.ok(!keys.includes("pen_id"));
    assert.ok(keys.includes("pen_name"));
  });

  it("認不得的鍵照樣開一欄 —— 匯出不可以掉東西", () => {
    const cols = detailColumns([{ detail: { 某個沒見過的欄位: 1 } }]);
    const found = cols.find((c) => c.key === "某個沒見過的欄位");
    assert.ok(found, "沒見過的欄位必須也有一欄,否則使用者的資料靜靜消失");
    assert.equal(found.label, "某個沒見過的欄位");
  });
});

describe("detailValue", () => {
  const labels = detailValueLabels();

  it("代碼換成畫面上看得到的字", () => {
    assert.equal(detailValue(labels, "zone", "farrowing"), "產房");
    assert.equal(detailValue(labels, "positive", true), "有懷孕");
    assert.equal(detailValue(labels, "positive", false), "沒懷孕");
  });

  it("對照表查不到就原樣留著", () => {
    // 「其他」那一欄本來就是自由文字,硬要對照只會把它變成空白。
    assert.equal(detailValue(labels, "reason", "被鄰居的狗嚇到"), "被鄰居的狗嚇到");
    assert.equal(detailValue(labels, "weaned", 11), 11);
  });

  it("沒有值就維持沒有值", () => {
    assert.equal(detailValue(labels, "zone", undefined), undefined);
  });
});

describe("eventCsv", () => {
  const rows = parseCsv(eventCsv(PAYLOAD));
  const header = rows[0];
  const col = (name) => header.indexOf(name);
  const find = (tag, event) =>
    rows.slice(1).find((r) => r[0] === tag && r[1] === event);

  it("依日期排序,不管送來的順序", () => {
    const dates = rows.slice(1).map((r) => r[col("日期")]);
    assert.deepEqual(dates, [...dates].sort());
  });

  it("每一筆記錄都在,肉豬死亡也排進同一張表", () => {
    assert.equal(rows.length - 1, PAYLOAD.events.length + PAYLOAD.marketDeaths.length);
    assert.ok(find("", "肉豬死亡"), "肉豬死亡沒有耳號,但仍然是一筆記錄");
  });

  it("事件名稱是中文", () => {
    assert.ok(find("2580", "配種"));
    assert.ok(find("2580", "離乳"));
  });

  it("detail 攤平到對應欄位", () => {
    const wean = find("2580", "離乳");
    assert.equal(wean[col("離乳頭數")], "11");
    assert.equal(wean[col("離乳仔豬評分")], "4");
    assert.equal(wean[col("有單睪/賀尼亞")], "是");
  });

  it("代碼欄位印中文,不是資料庫裡的英文值", () => {
    const move = find("2580", "移欄");
    assert.equal(move[col("區域")], "產房");
    assert.equal(find("2580", "配種")[col("發情穩定度")], "✗");
  });

  it("沒填的欄位留白,不補 0", () => {
    // 沒記活仔數不等於一隻都沒活。補 0 就是憑空捏造一筆資料。
    assert.ok(col("活仔數") >= 0, "這份資料裡有分娩,活仔數應該要有一欄");
    assert.equal(find("2580", "離乳")[col("活仔數")], "");
  });

  it("標為不納入統計的記錄仍在檔案裡,而且看得出來", () => {
    const odd = find("不明-0817", "分娩");
    assert.ok(odd, "排除的記錄不可以從匯出檔裡消失");
    assert.equal(odd[col("納入統計")], "否");
    assert.equal(find("2580", "離乳")[col("納入統計")], "是");
  });
});

describe("sowCsv / boarCsv / boarEventCsv", () => {
  it("母豬名單一頭一列,狀態是中文", () => {
    const rows = parseCsv(sowCsv(PAYLOAD));
    assert.equal(rows.length - 1, 2);
    assert.equal(rows[1][rows[0].indexOf("狀態")], "在場");
  });

  it("耳號待確認的那頭標出來,其餘留白", () => {
    const rows = parseCsv(sowCsv(PAYLOAD));
    const i = rows[0].indexOf("耳號待確認");
    assert.equal(rows[1][i], "");
    assert.equal(rows[2][i], "是");
  });

  it("公豬名單與公豬事件各自成表", () => {
    assert.equal(parseCsv(boarCsv(PAYLOAD)).length - 1, 1);
    const rows = parseCsv(boarEventCsv(PAYLOAD));
    assert.equal(rows[1][rows[0].indexOf("採精量")], "250");
  });

  it("沒有資料也不會爆掉", () => {
    for (const build of [eventCsv, sowCsv, boarCsv, boarEventCsv]) {
      assert.ok(build({}).startsWith(BOM));
    }
  });
});

describe("backupJson", () => {
  it("原封不動 —— 讀回來要跟送出去的一模一樣", () => {
    assert.deepEqual(JSON.parse(backupJson(PAYLOAD)), PAYLOAD);
  });

  it("連 id 與 excluded 都留著,那才叫備份", () => {
    const back = JSON.parse(backupJson(PAYLOAD));
    assert.equal(back.events[0].id, 20);
    assert.equal(back.events[3].excluded, true);
  });

  it("CSV 省略的內部欄位在備份裡一個都不少", () => {
    const payload = { events: [{ id: 1, type: "MV", date: "2026-04-20",
                                 detail: { pen_id: 12, pen_name: "A-12" } }] };
    assert.ok(INTERNAL_KEYS.has("pen_id"));
    assert.equal(JSON.parse(backupJson(payload)).events[0].detail.pen_id, 12);
  });
});

describe("exportFileName", () => {
  it("檔名帶日期,存好幾份才分得出來", () => {
    assert.equal(exportFileName("events", "2026-08-29"), "豬豬顧問-事件明細-2026-08-29.csv");
    assert.equal(exportFileName("sows", "2026-08-29"), "豬豬顧問-母豬名單-2026-08-29.csv");
  });

  it("備份是 json,其餘是 csv", () => {
    assert.ok(exportFileName("backup", "2026-08-29").endsWith(".json"));
    assert.ok(exportFileName("boarEvents", "2026-08-29").endsWith(".csv"));
  });
});

describe("exportSummary", () => {
  it("講清楚這次帶走了什麼", () => {
    const text = exportSummary(PAYLOAD);
    assert.match(text, /2 頭母豬/);
    assert.match(text, /1 頭公豬/);
    assert.match(text, /5 筆事件/);   // 4 筆母豬事件 + 1 筆肉豬死亡
  });
});
