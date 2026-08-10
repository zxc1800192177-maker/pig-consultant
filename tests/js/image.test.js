// 拍照上傳前的縮圖計算。
//
// 只測純函式:fileToJpegBase64 需要 canvas 與 Image,node:test 環境沒有 DOM,
// 那部分靠瀏覽器實測(比照 lib/speech.js 的做法)。

import { test, describe } from "node:test";
import assert from "node:assert";

import { fitWithin, stripDataUrl, MAX_EDGE } from "../../web/lib/image.js";

describe("fitWithin", () => {
  test("大圖等比縮到最長邊", () => {
    assert.deepStrictEqual(fitWithin(3000, 2000, 1500), { width: 1500, height: 1000 });
  });

  test("直式照片以高度為最長邊", () => {
    assert.deepStrictEqual(fitWithin(2000, 4000, 1000), { width: 500, height: 1000 });
  });

  test("小圖不放大", () => {
    // 放大不會生出原本不存在的細節,只是讓上傳變慢
    assert.deepStrictEqual(fitWithin(400, 300, 1500), { width: 400, height: 300 });
  });

  test("剛好等於上限時不變", () => {
    assert.deepStrictEqual(fitWithin(1500, 1500, 1500), { width: 1500, height: 1500 });
  });

  test("極端長寬比的短邊至少留 1 像素", () => {
    // canvas 寬或高給 0 會直接拋錯
    const { height } = fitWithin(4000, 2, 1000);
    assert.ok(height >= 1);
  });

  test("尺寸為 0 不會算出 NaN", () => {
    assert.deepStrictEqual(fitWithin(0, 0, 1500), { width: 0, height: 0 });
  });

  test("預設上限是 MAX_EDGE", () => {
    assert.strictEqual(fitWithin(5000, 5000).width, MAX_EDGE);
  });

  test("回傳整數,canvas 不接受小數尺寸", () => {
    const { width, height } = fitWithin(1001, 777, 500);
    assert.ok(Number.isInteger(width));
    assert.ok(Number.isInteger(height));
  });
});

describe("stripDataUrl", () => {
  test("切掉前綴只留 base64 本體", () => {
    assert.strictEqual(stripDataUrl("data:image/jpeg;base64,QUJD"), "QUJD");
  });

  test("沒有逗號時回空字串,不回傳整串當成資料", () => {
    // 把前綴當成 base64 送出去,後端會解出一包垃圾才發現不對
    assert.strictEqual(stripDataUrl("這不是 data URL"), "");
  });

  test("空值不炸掉", () => {
    assert.strictEqual(stripDataUrl(null), "");
    assert.strictEqual(stripDataUrl(undefined), "");
  });
});
