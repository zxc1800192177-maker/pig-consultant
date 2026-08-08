// 顯示格式測試。
//
// 這些函式決定牧場主看到什麼。級距顏色、落後程度的說法若表達不清,
// 使用者會誤判自己的經營狀況。

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { gradeTone, formatShortfall, formatValue } from "../../web/lib/format.js";

describe("級距語意色", () => {
  it("A、B 為良好", () => {
    assert.equal(gradeTone("A"), "good");
    assert.equal(gradeTone("B"), "good");
  });

  it("C 為持平(高於中位數,不是弱項)", () => {
    assert.equal(gradeTone("C"), "neutral");
  });

  it("D、E 為需注意", () => {
    assert.equal(gradeTone("D"), "warn");
    assert.equal(gradeTone("E"), "warn");
  });

  it("F 為嚴重", () => {
    assert.equal(gradeTone("F"), "critical");
  });

  it("未知級距不當成良好 —— 寧可顯示中性也不要誤導", () => {
    assert.equal(gradeTone("Z"), "neutral");
    assert.equal(gradeTone(undefined), "neutral");
  });
});

// 弱項判定的測試已移除:該規則只存在後端(core/diagnosis.py),
// 由 /api/grade 的 isWeak 欄位告知前端。對應的測試在
// tests/test_server.py::TestIsWeakComesFromBackend。
// 前端若再寫一份判斷,tests/test_single_source.py 會擋下來。

describe("落後程度的說法", () => {
  it("正值說「落後」", () => {
    assert.ok(formatShortfall(2.96).includes("落後"));
    assert.ok(formatShortfall(2.96).includes("2.96"));
  });

  it("負值說「優於」,不能也講成落後", () => {
    const text = formatShortfall(-0.33);
    assert.ok(text.includes("優於"));
    assert.ok(!text.includes("落後"));
  });

  it("負值顯示絕對值,不出現負號", () => {
    assert.ok(!formatShortfall(-0.33).includes("-"));
  });

  it("接近零說「與平均相當」", () => {
    assert.ok(formatShortfall(0.02).includes("相當"));
  });

  it("一律標明單位是標準差,讓數字有依據", () => {
    assert.ok(formatShortfall(1.5).includes("標準差"));
  });
});

describe("數值顯示", () => {
  it("帶單位", () => {
    assert.equal(formatValue(71.56, "%"), "71.56%");
  });

  it("無單位就不加", () => {
    assert.equal(formatValue(2.42, ""), "2.42");
  });

  it("整數不補小數點", () => {
    assert.equal(formatValue(11, "頭"), "11 頭");
  });

  it("缺值顯示為破折號,不顯示 undefined", () => {
    assert.equal(formatValue(null, "%"), "—");
    assert.equal(formatValue(undefined, "%"), "—");
  });
});
