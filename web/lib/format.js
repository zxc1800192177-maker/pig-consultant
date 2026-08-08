// 顯示格式。
//
// 這些函式決定牧場主看到什麼。措辭要精確 ——
// 把「優於平均」講成「落後」會讓人做出錯誤的經營決策。

// 與後端 core/diagnosis.py 的 WEAKNESS_THRESHOLD 對應:
// 弱項 = 低於全國中位數,也就是 D 級以下。C 級是前 25~50%,不是弱項。
const WEAK_GRADES = new Set(["D", "E", "F"]);

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

export function isWeak(grade) {
  return WEAK_GRADES.has(grade);
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
