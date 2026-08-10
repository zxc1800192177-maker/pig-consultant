// 「其他參考因素」純邏輯測試。跟藥品庫的新增/移除是同一套邏輯,
// 差別只在沒有 localStorage 持久化(這裡的資料只跟單次健檢有關)。

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { addFactor, removeFactor } from "../../web/lib/factors.js";

describe("新增參考因素", () => {
  it("補上 id,回傳新陣列而不修改原陣列", () => {
    const before = [];
    const after = addFactor(before, { name: "豬舍類型", value: "開放式豬舍" });
    assert.equal(before.length, 0); // 原陣列不受影響
    assert.equal(after.length, 1);
    assert.equal(after[0].name, "豬舍類型");
    assert.equal(after[0].value, "開放式豬舍");
    assert.ok(after[0].id);
  });

  it("名稱與內容前後空白會被裁掉", () => {
    const after = addFactor([], { name: "  豬舍類型  ", value: "  開放式  " });
    assert.equal(after[0].name, "豬舍類型");
    assert.equal(after[0].value, "開放式");
  });

  it("沒有名稱(空字串或只有空白)不新增", () => {
    assert.equal(addFactor([], { name: "" }).length, 0);
    assert.equal(addFactor([], { name: "   " }).length, 0);
  });

  it("內容是選填的", () => {
    const after = addFactor([], { name: "豬舍類型" });
    assert.equal(after[0].value, "");
  });

  it("連續新增兩筆會得到不同的 id", () => {
    let factors = addFactor([], { name: "甲" });
    factors = addFactor(factors, { name: "乙" });
    assert.notEqual(factors[0].id, factors[1].id);
  });
});

describe("移除參考因素", () => {
  it("依 id 移除指定項目,其餘保留", () => {
    const factors = [
      { id: "1", name: "甲", value: "" },
      { id: "2", name: "乙", value: "" },
    ];
    assert.deepEqual(removeFactor(factors, "1"), [{ id: "2", name: "乙", value: "" }]);
  });

  it("id 不存在時原陣列內容不變", () => {
    const factors = [{ id: "1", name: "甲", value: "" }];
    assert.deepEqual(removeFactor(factors, "不存在"), factors);
  });

  it("不修改原陣列", () => {
    const factors = [{ id: "1", name: "甲", value: "" }];
    removeFactor(factors, "1");
    assert.equal(factors.length, 1);
  });
});
