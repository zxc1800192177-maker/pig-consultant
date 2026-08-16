// 紀錄表單的純邏輯測試。
//
// 最要緊的一條在「空值不補預設」—— 沒填的死胎數存成 0 等於宣稱「這窩
// 沒有死胎」,那是憑空捏造資料。

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  ESTRUS_STABILITY_LABEL,
  ESTRUS_STABILITY_OPTIONS,
  RECORD_FORMS,
  SIDE_EFFECTS,
  buildDetail,
  createsNewAnimal,
  formFor,
  recordSummary,
  recordedRow,
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
    for (const code of ["FW", "WN", "SAL", "DTH"]) {
      assert.ok(SIDE_EFFECTS[code], `${code} 沒有說明副作用`);
    }
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
    assert.match(recordedRow(ev({ canUndo: true, sowId: 42 })), /data-sow="42"/);
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
