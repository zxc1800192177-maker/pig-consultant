// 紀錄表單的純邏輯測試。
//
// 最要緊的一條在「空值不補預設」—— 沒填的死胎數存成 0 等於宣稱「這窩
// 沒有死胎」,那是憑空捏造資料。

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  ESTRUS_STABILITY_LABEL,
  ESTRUS_STABILITY_OPTIONS,
  OTHER_REASON,
  RECORD_FORMS,
  SIDE_EFFECTS,
  ZONE_OPTIONS,
  buildDetail,
  createsNewAnimal,
  formFor,
  hasOtherOption,
  recordSummary,
  recordedRow,
  supportsMultiSow,
  targetsBoar,
  targetsEither,
  targetsNothing,
} from "../../web/lib/record.js";

describe("表單定義", () => {
  it("每一種事件都有中文名稱", () => {
    for (const [code, spec] of Object.entries(RECORD_FORMS)) {
      assert.ok(spec.label, `${code} 沒有名稱`);
    }
  });

  it("不認得的代碼回 null,不是空表單", () => {
    // 回空表單的話會畫出一張什麼都不能填的表,使用者以為是系統壞了
    assert.equal(formFor("ZZ"), null);
  });

  it("種豬進場是新增一頭豬,不是在某頭豬身上記一筆", () => {
    assert.equal(createsNewAnimal("GA"), true);
    assert.equal(createsNewAnimal("MT"), false);
  });

  it("會改變母豬狀態的事件都要先講清楚", () => {
    // 胎次 +1、耳號改號這些是不可逆的,按下去之前要知道
    for (const code of ["FW", "WN", "SAL", "DTH", "MV"]) {
      assert.ok(SIDE_EFFECTS[code], `${code} 沒有說明副作用`);
    }
  });
});

describe("配種一次記多頭", () => {
  // 同一天、同一隻公豬,常常是一整批母豬一起配 —— 逐頭開表單重打一次
  // 公豬耳號跟發情穩定度太沒效率(使用者要求)。
  it("配種支援一次多頭,其他事件不支援", () => {
    assert.equal(supportsMultiSow("MT"), true);
    assert.equal(supportsMultiSow("FW"), false);
    assert.equal(supportsMultiSow("WN"), false);
    assert.equal(supportsMultiSow("PD"), false);
  });

  it("不認得的代碼不支援,不是拋例外", () => {
    assert.equal(supportsMultiSow("ZZ"), false);
  });

  it("目標還是母豬,只是耳號輸入方式不同", () => {
    assert.equal(formFor("MT").target, "sow");
  });
});

describe("種豬死亡", () => {
  // 使用者決定:公豬死亡跟母豬死亡合併成同一個事件,改名「種豬死亡」,
  // 記在母豬還是公豬身上由記錄當下的切換鈕決定,不是表單本身寫死。
  it("target 是 either,不是寫死 sow 或 boar", () => {
    assert.equal(formFor("DTH").target, "either");
    assert.equal(targetsEither("DTH"), true);
    assert.equal(targetsEither("MT"), false);
  });

  it("不是母豬專屬,也不是公豬專屬", () => {
    assert.equal(createsNewAnimal("DTH"), false);
    assert.equal(targetsBoar("DTH"), false);
  });

  it("原因欄位不分物種,兩邊共用同一份定義", () => {
    const keys = formFor("DTH").fields.map((f) => f.key);
    assert.deepEqual(keys, ["reason"]);
  });
});

describe("肉豬死亡", () => {
  // 使用者要求:肉豬不用耳號,只要日期、原因、公斤數 —— 肉豬本來就不是
  // 這個系統追蹤身分的對象。
  it("target 是 none,不掛在任何一頭豬身上", () => {
    assert.equal(formFor("MKD").target, "none");
    assert.equal(targetsNothing("MKD"), true);
    assert.equal(targetsNothing("MT"), false);
  });

  it("不認得的代碼不算 target none,不是拋例外", () => {
    assert.equal(targetsNothing("ZZ"), false);
  });

  it("不是新增一頭豬,也不是母豬或公豬專屬", () => {
    assert.equal(createsNewAnimal("MKD"), false);
    assert.equal(targetsBoar("MKD"), false);
    assert.equal(targetsEither("MKD"), false);
  });

  it("兩個欄位:原因跟重量,都是必填", () => {
    const fields = formFor("MKD").fields;
    assert.deepEqual(fields.map((f) => f.key), ["reason", "weight_kg"]);
    assert.ok(fields.every((f) => f.required));
  });

  it("重量是 decimal 不是 int —— 公斤數可以有小數", () => {
    assert.equal(formFor("MKD").fields.find((f) => f.key === "weight_kg").type, "decimal");
  });

  it("留空原因或重量都要擋下來", () => {
    assert.ok(buildDetail("MKD", { weight_kg: "85" }).problems.length);
    assert.ok(buildDetail("MKD", { reason: "熱衰竭" }).problems.length);
  });

  it("填了兩者就正常送出", () => {
    const { detail, problems } = buildDetail("MKD", { reason: "熱衰竭", weight_kg: "85.5" });
    assert.deepEqual(problems, []);
    assert.equal(detail.reason, "熱衰竭");
    assert.equal(detail.weight_kg, 85.5);
  });
});

describe("移欄", () => {
  // 使用者要求:直接在紀錄頁打欄位編號,不必先到設定頁一個一個新增
  // —— 一區動輒幾百個欄位,要求先手動建一輪根本不會有人做。
  it("表單目標是既有母豬,不是新增一頭豬", () => {
    assert.equal(formFor("MV").target, "sow");
    assert.equal(createsNewAnimal("MV"), false);
  });

  it("兩個欄位:區域跟欄位編號", () => {
    const fields = formFor("MV").fields;
    assert.deepEqual(fields.map((f) => f.key), ["zone", "pen_name"]);
    assert.equal(fields[0].type, "choice");
    assert.equal(fields[1].type, "pen");
  });

  it("三個區域,對應後端的 mating/gestation/farrowing", () => {
    assert.deepEqual(ZONE_OPTIONS.map((z) => z.value),
      ["mating", "gestation", "farrowing"]);
    assert.deepEqual(ZONE_OPTIONS.map((z) => z.label), ["配種區", "待產區", "產房"]);
  });

  it("區域選項就是 ZONE_OPTIONS,不是另一份定義", () => {
    assert.equal(formFor("MV").fields[0].options, ZONE_OPTIONS);
  });

  it("填了區域跟編號就存成字串", () => {
    const { detail, problems } = buildDetail("MV",
      { zone: "mating", pen_name: "配-05" });
    assert.deepEqual(problems, []);
    assert.equal(detail.zone, "mating");
    assert.equal(detail.pen_name, "配-05");
  });

  it("編號前後空白會裁掉,跟其他文字欄位一樣", () => {
    const { detail } = buildDetail("MV", { zone: "mating", pen_name: "  37  " });
    assert.equal(detail.pen_name, "37");
  });

  it("沒填區域要報錯", () => {
    const { problems } = buildDetail("MV", { pen_name: "配-05" });
    assert.ok(problems.length);
    assert.ok(problems[0].includes("區域"));
  });

  it("沒填欄位編號要報錯 —— 這正是這筆記錄的全部內容", () => {
    const { problems } = buildDetail("MV", { zone: "mating" });
    assert.ok(problems.length);
    assert.ok(problems[0].includes("欄位編號"));
  });

  it("摘要顯示移去的欄位名稱(伺服器存的快照)", () => {
    const { extra } = recordSummary({
      type: "MV", detail: { pen_id: 5, pen_name: "配-01", zone: "mating" },
    });
    assert.ok(extra.includes("移至 配-01"));
  });
});

describe("種豬進場的父母耳號", () => {
  // 母豬跟公豬共用同一張表單(切換鈕決定送去哪個 API),父母耳號兩邊
  // 都可能不知道,所以是選填,不因為留空而報錯。
  it("表單裡有父系耳號跟母系耳號兩個欄位", () => {
    const keys = formFor("GA").fields.map((f) => f.key);
    assert.ok(keys.includes("sire_tag"));
    assert.ok(keys.includes("dam_tag"));
  });

  it("兩個都填會存進 detail", () => {
    const { detail, problems } = buildDetail("GA", {
      earTag: "2580", sire_tag: "L鄭", dam_tag: "2416",
    });
    assert.deepEqual(problems, []);
    assert.equal(detail.sire_tag, "L鄭");
    assert.equal(detail.dam_tag, "2416");
  });

  it("留空不報錯 —— 不是每頭豬都知道父母耳號", () => {
    const { problems } = buildDetail("GA", { earTag: "2580" });
    assert.deepEqual(problems, []);
  });

  it("只填一邊也可以", () => {
    const { detail } = buildDetail("GA", { earTag: "2580", sire_tag: "L鄭" });
    assert.equal(detail.sire_tag, "L鄭");
    assert.ok(!("dam_tag" in detail));
  });

  it("前後空白會裁掉", () => {
    const { detail } = buildDetail("GA", { earTag: "2580", sire_tag: "  L鄭  " });
    assert.equal(detail.sire_tag, "L鄭");
  });
});

describe("整理表單內容", () => {
  it("整數欄位轉成數字", () => {
    const { detail, problems } = buildDetail("FW", { born_alive: "12", stillborn: "2" });
    assert.deepEqual(problems, []);
    assert.equal(detail.born_alive, 12);
    assert.equal(detail.stillborn, 2);
  });

  it("沒填的欄位不送,不補 0", () => {
    // 死胎沒填就存成 0,等於宣稱這窩沒有死胎
    const { detail } = buildDetail("FW", { born_alive: "12", stillborn: "" });
    assert.equal(detail.born_alive, 12);
    assert.ok(!("stillborn" in detail));
  });

  it("活仔 0 是有效的填答,不可被當成沒填", () => {
    const { detail, problems } = buildDetail("FW", { born_alive: "0" });
    assert.deepEqual(problems, []);
    assert.equal(detail.born_alive, 0);
  });

  it("必填漏掉會報出來", () => {
    const { problems } = buildDetail("FW", {});
    assert.equal(problems.length, 1);
    assert.ok(problems[0].includes("活仔數"));
  });

  it("非必填漏掉不報錯", () => {
    const { problems } = buildDetail("DTH", {});
    assert.deepEqual(problems, []);
  });

  it("小數被擋下來", () => {
    const { problems } = buildDetail("FW", { born_alive: "12.5" });
    assert.ok(problems[0].includes("整數"));
  });

  it("超出範圍被擋下來", () => {
    const { problems } = buildDetail("FW", { born_alive: "99" });
    assert.ok(problems.length);
  });

  it("負數被擋下來", () => {
    const { problems } = buildDetail("PL", { count: "-1", reason: "體弱" });
    assert.ok(problems.length);
  });

  it("驗孕結果轉成布林,不是字串", () => {
    assert.equal(buildDetail("PD", { positive: "true" }).detail.positive, true);
    assert.equal(buildDetail("PD", { positive: "false" }).detail.positive, false);
  });

  it("沒選驗孕結果要報錯 —— 那正是這筆記錄的全部內容", () => {
    assert.ok(buildDetail("PD", {}).problems.length);
  });

  it("文字欄位前後空白會裁掉", () => {
    assert.equal(buildDetail("DTH", { reason: "  難產  " }).detail.reason, "難產");
  });

  it("流產沒有欄位要填,也不該報錯", () => {
    const { detail, problems } = buildDetail("AB", {});
    assert.deepEqual(problems, []);
    assert.deepEqual(detail, {});
  });

  it("不認得的事件類型會報錯,不是靜靜送出空的 detail", () => {
    assert.ok(buildDetail("ZZ", {}).problems.length);
  });
});

describe("原因選了「其他」要能打字說明", () => {
  // 仔豬死亡跟淘汰的原因都是從實際記錄取出來的固定選項,「其他」接住
  // 選不到的長尾 —— 選了卻沒地方寫清楚是什麼,這筆資料就白記了。
  it("仔豬死亡的原因有「其他」選項", () => {
    assert.ok(hasOtherOption(formFor("PL").fields.find((f) => f.key === "reason")));
  });

  it("淘汰的原因有「其他」選項", () => {
    assert.ok(hasOtherOption(formFor("SAL").fields.find((f) => f.key === "reason")));
  });

  it("不是 choice 類型就不算有「其他」", () => {
    assert.equal(hasOtherOption({ type: "text" }), false);
  });

  it("choice 但選項裡沒有「其他」就不用準備打字框", () => {
    assert.equal(hasOtherOption({ type: "choice", options: ZONE_OPTIONS }), false);
  });

  it("OTHER_REASON 就是選項清單裡「其他」那個值,兩邊用同一份定義", () => {
    assert.ok(formFor("PL").fields.find((f) => f.key === "reason").options
      .includes(OTHER_REASON));
  });

  it("打字說明的內容照樣進 reason,跟直接選固定選項一樣存法", () => {
    // readRecordFields()(app.js)選到「其他」時會把打字框的內容換進來
    // 當作 reason 的值送進 buildDetail() —— 這裡直接驗證那個值能正常存。
    const { detail, problems } = buildDetail("PL", { count: "2", reason: "難產" });
    assert.deepEqual(problems, []);
    assert.equal(detail.reason, "難產");
  });
});

describe("離乳仔豬評分", () => {
  // 使用者要求:1~5 分由牧場主自評,之前沒有這項評分就不用寫
  it("1 到 5 分都收", () => {
    for (const n of [1, 2, 3, 4, 5]) {
      const { detail, problems } = buildDetail("WN", { weaned: "11", wean_score: String(n) });
      assert.deepEqual(problems, [], `${n} 分被擋`);
      assert.equal(detail.wean_score, n);
    }
  });

  it("留空就是沒評分,不補中間值", () => {
    // 補 3 分會讓「沒人看過」與「看過覺得普通」變成同一件事
    const { detail, problems } = buildDetail("WN", { weaned: "11", wean_score: "" });
    assert.deepEqual(problems, []);
    assert.ok(!("wean_score" in detail));
  });

  it("0 分與 6 分被擋下來", () => {
    for (const n of ["0", "6"]) {
      assert.ok(buildDetail("WN", { weaned: "11", wean_score: n }).problems.length, n);
    }
  });

  it("離乳頭數仍然是必填 —— 評分可略過不代表整筆可以空著", () => {
    assert.ok(buildDetail("WN", { wean_score: "4" }).problems.length);
  });
});

describe("發情穩定度", () => {
  // 使用者要求:配種表單裡三個選項,✓/△/✗,同樣可以不評
  it("三個選項,符號是 ✓ △ ✗", () => {
    assert.deepEqual(ESTRUS_STABILITY_OPTIONS.map((o) => o.label), ["✓", "△", "✗"]);
  });

  it("存的是判斷值,不是符號本身 —— 符號以後想換不必動到已存資料", () => {
    const { detail, problems } = buildDetail("MT", { estrus_stability: "stable" });
    assert.deepEqual(problems, []);
    assert.equal(detail.estrus_stability, "stable");
  });

  it("三個判斷值都收", () => {
    for (const value of ["stable", "uncertain", "unstable"]) {
      const { detail, problems } = buildDetail("MT", { estrus_stability: value });
      assert.deepEqual(problems, []);
      assert.equal(detail.estrus_stability, value);
    }
  });

  it("留空不算漏填 —— 可以不評", () => {
    const { problems } = buildDetail("MT", {});
    assert.deepEqual(problems, []);
  });

  it("公豬耳號仍然可以跟發情穩定度一起填", () => {
    const { detail } = buildDetail("MT", { boar_tag: "D6", estrus_stability: "unstable" });
    assert.equal(detail.boar_tag, "D6");
    assert.equal(detail.estrus_stability, "unstable");
  });

  it("摘要顯示符號,值跟符號的對照只有這一份定義", () => {
    const { extra } = recordSummary({ type: "MT", detail: { estrus_stability: "uncertain" } });
    assert.ok(extra.includes(`發情 ${ESTRUS_STABILITY_LABEL.uncertain}`));
  });

  it("沒評就不顯示,不補一個中間符號", () => {
    const { extra } = recordSummary({ type: "MT", detail: { boar_tag: "D6" } });
    assert.doesNotMatch(extra, /發情/);
  });
});

describe("已記錄清單", () => {
  const ev = (over = {}) => ({
    id: 1, sowId: 42, type: "MT", date: "2026-08-13", earTag: "1183",
    detail: {}, canUndo: false, ...over,
  });

  it("摘要帶出事件名稱與重點", () => {
    const { name, extra } = recordSummary(ev({ type: "FW", detail: { born_alive: 12, stillborn: 2 } }));
    assert.equal(name, "分娩");
    assert.ok(extra.includes("活仔 12"));
    assert.ok(extra.includes("死胎 2"));
  });

  it("未評分不顯示評分", () => {
    const { extra } = recordSummary(ev({ type: "WN", detail: { weaned: 11 } }));
    assert.doesNotMatch(extra, /評分/);
  });

  it("有評分才顯示", () => {
    const { extra } = recordSummary(ev({ type: "WN", detail: { weaned: 11, wean_score: 4 } }));
    assert.ok(extra.includes("評分 4 分"));
  });

  it("可以收回時才畫收回按鈕", () => {
    assert.match(recordedRow(ev({ canUndo: true })), /data-undo="1"/);
    assert.doesNotMatch(recordedRow(ev({ canUndo: false })), /data-undo/);
  });

  it("收回按鈕帶著母豬 id,收回後才知道該重新整理哪一張卡", () => {
    // 實際踩過的 bug:記成死亡或淘汰後,已經開著的母豬卡耳號沒有更新,
    // 因為送出記錄後只重讀了列表跟提醒,沒有重讀開著的那張卡。收回
    // 同樣需要知道是哪一頭,才能對應著重新整理。
    const html = recordedRow(ev({ canUndo: true, sowId: 42 }));
    assert.match(html, /data-animal="42"/);
    assert.match(html, /data-kind="sow"/);
  });

  it("沒有 kind 時當成母豬事件 —— 舊呼叫端不用跟著改", () => {
    const { kind, ...rest } = ev({ canUndo: true, sowId: 7 });
    assert.match(recordedRow(rest), /data-kind="sow"/);
  });

  it("公豬事件帶 kind=boar 跟公豬 id", () => {
    const html = recordedRow({
      id: 9, kind: "boar", boarId: 5, type: "SC", date: "2026-08-17",
      earTag: "D6", detail: { volume: 15 }, canUndo: true,
    });
    assert.match(html, /data-kind="boar"/);
    assert.match(html, /data-animal="5"/);
  });

  it("種豬進場摘要顯示品種", () => {
    const { name, extra } = recordSummary({ type: "GA", detail: { breed: "Duroc" } });
    assert.equal(name, "種豬進場");
    assert.ok(extra.includes("Duroc"));
  });

  it("母豬進場(kind=sow-entry)收回按鈕帶母豬 id,打錯耳號時整頭撤掉", () => {
    const html = recordedRow({
      id: 12, sowId: 12, kind: "sow-entry", type: "GA", date: "2026-08-17",
      earTag: "9001", detail: { breed: "" }, canUndo: true,
    });
    assert.match(html, /data-kind="sow-entry"/);
    assert.match(html, /data-animal="12"/);
  });

  it("公豬進場(kind=boar-entry)收回按鈕帶的是公豬 id 不是母豬 id", () => {
    const html = recordedRow({
      id: 5, boarId: 5, kind: "boar-entry", type: "GA", date: "2026-08-17",
      earTag: "D6", detail: { breed: "" }, canUndo: true,
    });
    assert.match(html, /data-kind="boar-entry"/);
    assert.match(html, /data-animal="5"/);
  });

  it("肉豬死亡摘要顯示原因跟公斤數", () => {
    const { name, extra } = recordSummary(
      { type: "MKD", detail: { reason: "熱衰竭", weight_kg: 85.5 } });
    assert.equal(name, "肉豬死亡");
    assert.ok(extra.includes("熱衰竭"));
    assert.ok(extra.includes("85.5 公斤"));
  });

  it("肉豬死亡(kind=market-death)沒有耳號、沒有 data-animal —— 不掛在任何一頭豬身上", () => {
    const html = recordedRow({
      id: 3, kind: "market-death", type: "MKD", date: "2026-08-17",
      earTag: "", detail: { reason: "熱衰竭", weight_kg: 85.5 }, canUndo: true,
    });
    assert.match(html, /data-kind="market-death"/);
    assert.doesNotMatch(html, /data-animal/);
    // 沒有耳號時標題不該留下一格空白(以前的寫法是耳號永遠印在前面,
    // 沒耳號就變成 " 肉豬死亡" 開頭多一個空格)。
    assert.match(html, /class="done-t">肉豬死亡</);
  });

  it("耳號有跳脫", () => {
    const html = recordedRow(ev({ earTag: "<img src=x>" }));
    assert.doesNotMatch(html, /<img/);
  });

  it("原因文字有跳脫", () => {
    const html = recordedRow(ev({ type: "SAL", detail: { reason: "<script>x</script>" } }));
    assert.doesNotMatch(html, /<script>/);
  });
});

describe("採精", () => {
  // 使用者決定:不需要獨立的「精液品質」事件,精蟲活力跟精液濃度併進
  // 採精表單裡即可。
  it("記在公豬身上,不是母豬", () => {
    assert.equal(formFor("SC").target, "boar");
    assert.equal(targetsBoar("SC"), true);
    assert.equal(targetsBoar("MT"), false);
  });

  it("不再是可記錄的事件類型", () => {
    assert.equal(formFor("SP"), null);
  });

  it("採精量必填,其餘選填", () => {
    const { detail, problems } = buildDetail("SC", { volume: "15" });
    assert.deepEqual(problems, []);
    assert.equal(detail.volume, 15);
    assert.ok(!("doses" in detail));
    assert.ok(!("motility" in detail));
    assert.ok(!("concentration" in detail));
  });

  it("沒填採精量要報錯", () => {
    assert.ok(buildDetail("SC", {}).problems.length);
  });

  it("精蟲活力是整數百分比", () => {
    const { detail, problems } = buildDetail("SC", { volume: "15", motility: "80" });
    assert.deepEqual(problems, []);
    assert.equal(detail.motility, 80);
  });

  it("精蟲活力超出 0~100 被擋下來", () => {
    assert.ok(buildDetail("SC", { volume: "15", motility: "101" }).problems.length);
  });

  it("精液濃度可以是小數,不像整數欄位那樣被擋", () => {
    const { detail, problems } = buildDetail("SC", { volume: "15", concentration: "3.5" });
    assert.deepEqual(problems, []);
    assert.equal(detail.concentration, 3.5);
  });

  it("精液濃度不是數字時要報錯", () => {
    assert.ok(buildDetail("SC", { volume: "15", concentration: "abc" }).problems.length);
  });

  it("摘要顯示採精量、活力、濃度、劑量", () => {
    const { extra } = recordSummary({
      type: "SC", detail: { volume: 15, motility: 80, concentration: 3.5, doses: 3 },
    });
    assert.ok(extra.includes("採精量 15 ml"));
    assert.ok(extra.includes("活力 80%"));
    assert.ok(extra.includes("濃度 3.5 億/mL"));
    assert.ok(extra.includes("3 劑"));
  });
});
