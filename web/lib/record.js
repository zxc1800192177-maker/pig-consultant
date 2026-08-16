// 紀錄頁的表單定義與資料整理。純邏輯,不碰 DOM 也不碰 fetch。
//
// 表單長什麼樣子寫成**資料**而不是十份手刻的 HTML:十種事件各寫一份表單,
// 改一個共同行為(例如日期欄位)就得改十個地方,漏掉一個沒有人會發現。

import { escapeHtml } from "./markdown.js";

/** 發情穩定度的三個選項。存的是這裡的 value(穩定判斷,不受符號改版
 * 影響),按鈕與時間軸摘要都顯示 label 這個符號 —— 只有一份定義,
 * 表單跟摘要不會各自維護一份對照表而慢慢兜不起來。
 */
export const ESTRUS_STABILITY_OPTIONS = [
  { value: "stable", label: "✓" },
  { value: "uncertain", label: "△" },
  { value: "unstable", label: "✗" },
];

/** value → 符號。摘要文字用,從上面那份選項表算出來,不是另一份定義。 */
export const ESTRUS_STABILITY_LABEL = Object.fromEntries(
  ESTRUS_STABILITY_OPTIONS.map((o) => [o.value, o.label]));

/** 每種事件要填什麼。`type` 決定畫成什麼元件與怎麼收值。 */
export const RECORD_FORMS = {
  MT: {
    label: "配種", target: "sow",
    fields: [
      { key: "boar_tag", label: "公豬", type: "boar" },
      // 配種當下觀察到的發情徵狀。跟離乳評分同樣的道理:主觀判斷,
      // **可以不評**,沒填不補值(憲法第三條第 6 款)。
      { key: "estrus_stability", label: "發情穩定度", type: "tri",
        options: ESTRUS_STABILITY_OPTIONS,
        hint: "配種當下的發情徵狀,不確定可以留空" },
    ],
  },
  FW: {
    label: "分娩", target: "sow",
    fields: [
      { key: "born_alive", label: "活仔數", type: "int", min: 0, max: 30, required: true },
      { key: "stillborn", label: "死胎", type: "int", min: 0, max: 30 },
      { key: "mummified", label: "木乃伊", type: "int", min: 0, max: 30 },
    ],
  },
  WN: {
    label: "離乳", target: "sow",
    fields: [
      { key: "weaned", label: "離乳頭數", type: "int", min: 0, max: 30, required: true },
      // 使用者要求的自評項目。**可以不評** —— 沒評分顯示「—」,不補值。
      { key: "wean_score", label: "離乳仔豬評分", type: "score",
        hint: "1~5 分,由你自己評。不想評可以留空" },
    ],
  },
  PD: {
    label: "驗孕", target: "sow",
    fields: [{ key: "positive", label: "結果", type: "bool",
               yes: "有懷孕", no: "沒懷孕", required: true }],
  },
  FON: {
    label: "寄養移入", target: "sow",
    fields: [{ key: "count", label: "頭數", type: "int", min: 1, max: 30, required: true }],
  },
  FOF: {
    label: "寄養移出", target: "sow",
    fields: [{ key: "count", label: "頭數", type: "int", min: 1, max: 30, required: true }],
  },
  PL: {
    label: "仔豬死亡", target: "sow",
    fields: [
      { key: "count", label: "頭數", type: "int", min: 1, max: 30, required: true },
      { key: "reason", label: "原因", type: "choice", required: true,
        // 取自這個場 3,470 筆仔豬損失的實際原因,依筆數排序 ——
        // 最常按到的排最前面,巡欄時少滑一次
        options: ["母豬壓死", "體弱", "餓死", "下痢", "畸形", "其他"] },
    ],
  },
  DTH: {
    label: "母豬死亡", target: "sow",
    fields: [{ key: "reason", label: "原因", type: "text" }],
  },
  AB: { label: "流產", target: "sow", fields: [] },
  SAL: {
    label: "淘汰", target: "sow",
    fields: [{ key: "reason", label: "原因", type: "choice", required: true,
               // 同樣取自實際記錄:年齡太大 48.0%、不能懷孕 18.6%
               options: ["年齡太大", "不能懷孕", "子宮蓄膿", "生產性能差",
                         "肢蹄問題", "其他"] }],
  },
  GA: {
    label: "種豬進場", target: "new",
    // 母豬跟公豬共用同一張表單(用上面的 母豬/公豬 切換鈕決定送去哪個
    // API),父母耳號兩邊都可能知道也可能不知道,所以是選填。
    fields: [
      { key: "earTag", label: "耳號", type: "text", required: true },
      { key: "breed", label: "品種", type: "text" },
      { key: "birthDate", label: "出生日期", type: "date" },
      { key: "sire_tag", label: "父系耳號", type: "text" },
      { key: "dam_tag", label: "母系耳號", type: "text" },
    ],
  },
};

/** 事件會改變母豬狀態時的提醒文字。按下去之前要先知道會發生什麼。 */
export const SIDE_EFFECTS = {
  FW: "胎次會 +1",
  WN: "產房欄位會空出來",
  SAL: "耳號會加上民國年後綴,裸號釋放給新豬",
  DTH: "耳號會加上民國年後綴,裸號釋放給新豬",
};

export function formFor(code) {
  return RECORD_FORMS[code] || null;
}

/** 這種事件是不是「新增一頭豬」而不是「在某頭豬身上記一筆」。 */
export function createsNewAnimal(code) {
  return formFor(code)?.target === "new";
}

/**
 * 把表單收到的原始值整理成 detail,並回報缺漏。
 *
 * 回 { detail, problems }。**空字串一律丟掉,不轉成 0** —— 沒填的死胎數
 * 存成 0 等於宣稱「這窩沒有死胎」,那是憑空捏造的資料(憲法第三條)。
 */
export function buildDetail(code, raw) {
  const spec = formFor(code);
  if (!spec) return { detail: {}, problems: [`不認得的事件類型:${code}`] };

  const detail = {};
  const problems = [];

  for (const field of spec.fields) {
    const value = raw[field.key];
    const blank = value === undefined || value === null || value === "";

    if (blank) {
      if (field.required) problems.push(`請填寫${field.label}`);
      continue;                       // 沒填就不送,不補預設值
    }

    if (field.type === "int" || field.type === "score") {
      const n = Number(value);
      if (!Number.isInteger(n)) {
        problems.push(`${field.label}請填整數`);
        continue;
      }
      const min = field.type === "score" ? 1 : (field.min ?? 0);
      const max = field.type === "score" ? 5 : (field.max ?? 999);
      if (n < min || n > max) {
        problems.push(`${field.label}請填 ${min} 到 ${max}`);
        continue;
      }
      detail[field.key] = n;
    } else if (field.type === "bool") {
      detail[field.key] = value === true || value === "true";
    } else {
      detail[field.key] = String(value).trim();
    }
  }

  return { detail, problems };
}

/** 事件列的一句話摘要,給「已記錄」清單用。 */
export function recordSummary(event) {
  const spec = formFor(event.type);
  const name = spec ? spec.label : event.type;
  const d = event.detail || {};
  const bits = [];

  if (d.boar_tag) bits.push(`公豬 ${d.boar_tag}`);
  if (d.estrus_stability) {
    const symbol = ESTRUS_STABILITY_LABEL[d.estrus_stability];
    if (symbol) bits.push(`發情 ${symbol}`);
  }
  if (d.born_alive != null) bits.push(`活仔 ${d.born_alive}`);
  if (d.stillborn) bits.push(`死胎 ${d.stillborn}`);
  if (d.weaned != null) bits.push(`離乳 ${d.weaned} 隻`);
  // 未評分不顯示,也不補「—」以外的東西
  if (d.wean_score != null) bits.push(`評分 ${d.wean_score} 分`);
  if (d.count != null) bits.push(`${d.count} 隻`);
  if (d.reason) bits.push(d.reason);
  if (d.positive === true) bits.push("有懷孕");
  if (d.positive === false) bits.push("沒懷孕");

  return { name, extra: bits.join(" ・ ") };
}

export function recordedRow(event) {
  const { name, extra } = recordSummary(event);
  return `
    <div class="done-row">
      <div class="done-b">
        <div class="done-t">${escapeHtml(event.earTag || "")} ${escapeHtml(name)}</div>
        <div class="done-s">${escapeHtml(extra || event.date)}</div>
      </div>
      ${event.canUndo
        ? `<button class="btn-ghost undo" data-undo="${event.id}" data-sow="${event.sowId}"
           >收回</button>`
        : ""}
    </div>`;
}
