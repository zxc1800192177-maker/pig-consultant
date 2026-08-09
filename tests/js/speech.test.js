// 語音輸入邏輯測試。真正的 SpeechRecognition 物件無法在 node:test 建構,
// 這裡用普通物件模擬事件形狀,驗證的是純邏輯,不是瀏覽器 API 本身。

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  isSpeechRecognitionSupported,
  mergeTranscript,
  splitFinalAndInterim,
} from "../../web/lib/speech.js";

describe("瀏覽器支援判斷", () => {
  it("有 SpeechRecognition 視為支援", () => {
    assert.equal(isSpeechRecognitionSupported({ SpeechRecognition: function () {} }), true);
  });

  it("有 webkitSpeechRecognition(Safari/Chrome 前綴)也視為支援", () => {
    assert.equal(
      isSpeechRecognitionSupported({ webkitSpeechRecognition: function () {} }),
      true
    );
  });

  it("兩者都沒有視為不支援(如桌面版 Firefox)", () => {
    assert.equal(isSpeechRecognitionSupported({}), false);
  });

  it("window 物件本身不存在也不拋例外", () => {
    assert.equal(isSpeechRecognitionSupported(undefined), false);
  });
});

describe("拆分定案與暫定文字", () => {
  it("單一定案結果", () => {
    const results = [{ isFinal: true, 0: { transcript: "小豬下痢" } }];
    assert.deepEqual(splitFinalAndInterim(results), {
      finalText: "小豬下痢",
      interimText: "",
    });
  });

  it("continuous 模式下多段定案要全部串起來", () => {
    const results = [
      { isFinal: true, 0: { transcript: "小豬下痢" } },
      { isFinal: true, 0: { transcript: "已經兩天" } },
    ];
    assert.deepEqual(splitFinalAndInterim(results), {
      finalText: "小豬下痢已經兩天",
      interimText: "",
    });
  });

  it("最後一段還在辨識中(未定案)歸到 interimText", () => {
    const results = [
      { isFinal: true, 0: { transcript: "小豬下痢" } },
      { isFinal: false, 0: { transcript: "已經" } },
    ];
    assert.deepEqual(splitFinalAndInterim(results), {
      finalText: "小豬下痢",
      interimText: "已經",
    });
  });

  it("空陣列回傳空字串", () => {
    assert.deepEqual(splitFinalAndInterim([]), { finalText: "", interimText: "" });
  });
});

describe("合併語音文字與既有輸入", () => {
  it("原本是空的,直接用語音文字", () => {
    assert.equal(mergeTranscript("", "小豬下痢"), "小豬下痢");
  });

  it("原本有手打文字,語音接在後面並補空格", () => {
    assert.equal(mergeTranscript("保育豬咳嗽", "喘氣"), "保育豬咳嗽 喘氣");
  });

  it("語音辨識結果是空字串時,保留原本文字不變", () => {
    assert.equal(mergeTranscript("保育豬咳嗽", ""), "保育豬咳嗽");
  });

  it("兩邊多餘空白都會被裁掉", () => {
    assert.equal(mergeTranscript("  保育豬咳嗽  ", "  喘氣  "), "保育豬咳嗽 喘氣");
  });
});
