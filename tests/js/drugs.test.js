// 藥品庫純邏輯測試。用假的 storage 物件模擬 localStorage,
// 驗證的是讀寫/新增/移除的邏輯本身,不是瀏覽器 API。

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { addDrug, loadMyDrugs, removeDrug, saveMyDrugs } from "../../web/lib/drugs.js";

function fakeStorage(initial = {}) {
  const data = { ...initial };
  return {
    getItem: (key) => (key in data ? data[key] : null),
    setItem: (key, value) => {
      data[key] = value;
    },
    _raw: data,
  };
}

describe("讀取藥品庫", () => {
  it("從未存過時回傳空陣列", () => {
    assert.deepEqual(loadMyDrugs(fakeStorage()), []);
  });

  it("讀回先前存的內容", () => {
    const storage = fakeStorage({
      "pigConsultant.myDrugs": JSON.stringify([{ id: "1", name: "阿莫西林" }]),
    });
    assert.deepEqual(loadMyDrugs(storage), [{ id: "1", name: "阿莫西林" }]);
  });

  it("壞掉的 JSON 不拋例外,視為空清單", () => {
    const storage = fakeStorage({ "pigConsultant.myDrugs": "不是 json{{{" });
    assert.deepEqual(loadMyDrugs(storage), []);
  });

  it("存的內容不是陣列時視為空清單", () => {
    const storage = fakeStorage({ "pigConsultant.myDrugs": JSON.stringify({ a: 1 }) });
    assert.deepEqual(loadMyDrugs(storage), []);
  });
});

describe("寫入藥品庫", () => {
  it("序列化後存進 storage", () => {
    const storage = fakeStorage();
    saveMyDrugs(storage, [{ id: "1", name: "阿莫西林" }]);
    assert.equal(storage.getItem("pigConsultant.myDrugs"), '[{"id":"1","name":"阿莫西林"}]');
  });

  it("寫入後讀回是同樣的內容", () => {
    const storage = fakeStorage();
    const drugs = [{ id: "1", name: "阿莫西林" }];
    saveMyDrugs(storage, drugs);
    assert.deepEqual(loadMyDrugs(storage), drugs);
  });
});

describe("新增藥品", () => {
  it("補上 id,回傳新陣列而不修改原陣列", () => {
    const before = [];
    const after = addDrug(before, { name: "阿莫西林" });
    assert.equal(before.length, 0); // 原陣列不受影響
    assert.equal(after.length, 1);
    assert.equal(after[0].name, "阿莫西林");
    assert.ok(after[0].id);
  });

  it("名字前後空白會被裁掉", () => {
    const after = addDrug([], { name: "  阿莫西林  " });
    assert.equal(after[0].name, "阿莫西林");
  });

  it("沒有名字(空字串或只有空白)不新增", () => {
    assert.equal(addDrug([], { name: "" }).length, 0);
    assert.equal(addDrug([], { name: "   " }).length, 0);
  });

  it("劑量說明與休藥期是選填的", () => {
    const after = addDrug([], { name: "阿莫西林" });
    assert.equal(after[0].dosageNote, "");
    assert.equal(after[0].withdrawalDays, null);
  });

  it("休藥期可以帶數字", () => {
    const after = addDrug([], { name: "阿莫西林", withdrawalDays: 7 });
    assert.equal(after[0].withdrawalDays, 7);
  });

  it("休藥期是負數或非數字時視為未填", () => {
    assert.equal(addDrug([], { name: "藥A", withdrawalDays: -1 })[0].withdrawalDays, null);
    assert.equal(addDrug([], { name: "藥B", withdrawalDays: "七天" })[0].withdrawalDays, null);
  });

  it("連續新增兩筆會得到不同的 id", () => {
    let drugs = addDrug([], { name: "藥A" });
    drugs = addDrug(drugs, { name: "藥B" });
    assert.notEqual(drugs[0].id, drugs[1].id);
  });
});

describe("移除藥品", () => {
  it("依 id 移除指定項目,其餘保留", () => {
    const drugs = [
      { id: "1", name: "藥A" },
      { id: "2", name: "藥B" },
    ];
    assert.deepEqual(removeDrug(drugs, "1"), [{ id: "2", name: "藥B" }]);
  });

  it("id 不存在時原陣列內容不變", () => {
    const drugs = [{ id: "1", name: "藥A" }];
    assert.deepEqual(removeDrug(drugs, "不存在"), drugs);
  });

  it("不修改原陣列", () => {
    const drugs = [{ id: "1", name: "藥A" }];
    removeDrug(drugs, "1");
    assert.equal(drugs.length, 1);
  });
});
