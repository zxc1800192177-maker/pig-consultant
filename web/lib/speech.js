// 語音輸入的純邏輯。
//
// 瀏覽器的 SpeechRecognition 物件本身沒辦法在測試環境建構(node:test 沒有
// DOM),所以能測的邏輯都拆到這裡,用普通物件模擬事件形狀即可測試。
// app.js 只留「怎麼接上真正的瀏覽器 API」這件事。

// SpeechRecognition 有廠商前綴問題(webkitSpeechRecognition),
// 且 Firefox 桌面版完全不支援 —— 判斷式集中在這裡,只需要改一處。
export function isSpeechRecognitionSupported(win) {
  return Boolean(win && (win.SpeechRecognition || win.webkitSpeechRecognition));
}

// continuous 模式下,event.results 是「這次錄音從頭到現在」的完整結果,
// 不是每次事件才新增的片段 —— 每次都要重新掃過全部,不能只看最後一筆。
export function splitFinalAndInterim(results) {
  let finalText = "";
  let interimText = "";
  for (const result of results) {
    const transcript = result[0]?.transcript || "";
    if (result.isFinal) finalText += transcript;
    else interimText += transcript;
  }
  return { finalText, interimText };
}

// 把辨識出來的語音接到使用者原本已經打好的文字後面。
// 兩段文字之間補一個空格,避免「保育豬咳嗽」接「喘氣」黏成「保育豬咳嗽喘氣」
// 讀起來像一個詞,語音跟手打文字交錯輸入時尤其容易發生。
export function mergeTranscript(base, spoken) {
  const trimmedBase = (base || "").trim();
  const trimmedSpoken = (spoken || "").trim();
  if (!trimmedSpoken) return trimmedBase;
  if (!trimmedBase) return trimmedSpoken;
  return `${trimmedBase} ${trimmedSpoken}`;
}
