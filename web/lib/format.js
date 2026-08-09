// 顯示格式。
//
// 這些函式決定牧場主看到什麼。措辭要精確 ——
// 把「優於平均」講成「落後」會讓人做出錯誤的經營決策。

// 弱項判斷規則不在這裡 —— 由後端 core/diagnosis.py 決定,
// 經 /api/grade 的 isWeak 欄位告知前端。前端若自己再判斷一次,
// 規則就有兩份定義,改一邊漏一邊會讓畫面標示與實際排序不一致。

// 這是「視覺嚴重度」對應,屬於呈現層自己的事,與弱項判斷是不同概念:
// 弱項要同時看級距與標準差距離,這裡只依級距決定顏色深淺。
const TONES = {
  A: "good",
  B: "good",
  C: "neutral",
  D: "warn",
  E: "warn",
  F: "critical",
};

// 小於這個標準差差距視為與平均相當,不值得標成優劣。
const NEGLIGIBLE_SD = 0.1;

export function gradeTone(grade) {
  // 未知級距回中性 —— 寧可不表態,也不要誤標成良好。
  return TONES[grade] || "neutral";
}

export function formatShortfall(sd) {
  if (typeof sd !== "number" || Number.isNaN(sd)) return "—";
  const magnitude = Math.abs(sd).toFixed(2);
  if (Math.abs(sd) < NEGLIGIBLE_SD) return "與全國平均相當(差距不到 0.1 個標準差)";
  if (sd > 0) return `落後全國平均 ${magnitude} 個標準差`;
  return `優於全國平均 ${magnitude} 個標準差`;
}

export function formatValue(value, unit) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const text = Number.isInteger(value) ? String(value) : String(value);
  if (!unit) return text;
  return unit === "%" ? `${text}%` : `${text} ${unit}`;
}

// 歷史紀錄的日期。後端送 ISO 字串,直接顯示對農民不友善,
// 但也不需要精確到秒 —— 同一天做兩次健檢才需要時間來分辨。
export function formatRecordDate(isoString) {
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return "—";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}/${pad(d.getMonth() + 1)}/${pad(d.getDate())} ` +
         `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// 歷史清單每一列的摘要。列出全部 18 項會讓清單完全無法掃讀,
// 所以只講「填了幾項、其中幾項待改善」。
export function summarizeRecord(record) {
  const total = Object.keys(record.grades || {}).length;
  if (!total) return "沒有可評級的項目";
  const weak = record.weakCount || 0;
  return weak
    ? `${total} 項指標・${weak} 項待改善`
    : `${total} 項指標・全部達標`;
}
