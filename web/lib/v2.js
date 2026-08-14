// v2 的畫面渲染。純字串組裝,不碰 fetch 也不碰全域狀態 ——
// 這樣才能在 node:test 下測試(app.js 那些 DOM 接線不行)。

import { escapeHtml } from "./markdown.js";

export const EVENT_NAMES = {
  MT: "配種", PD: "驗孕", FW: "分娩", WN: "離乳", PL: "仔豬損失",
  GA: "進場", SAL: "淘汰", DTH: "死亡", AB: "流產",
  FON: "寄養移入", FOF: "寄養移出",
};

export function eventName(code) {
  return EVENT_NAMES[code] || code;
}

// 週次標籤:2026-08-10 → 08/10
export function formatWeek(start, end) {
  const f = (s) => String(s).slice(5).replace("-", "/");
  return `${f(start)} – ${f(end)}`;
}

// 往前/往後幾天。用字串進出,不讓 Date 的時區問題滲進來 ——
// new Date("2026-08-10") 在某些時區會變成前一天。
export function shiftDate(iso, days) {
  const d = new Date(`${iso}T12:00:00`);
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}

// 把事件的 detail 整理成一句話。欄位是哪些由 importer.py 決定,
// 這裡只負責挑出有值的來顯示。
export function describeEvent(event) {
  const d = event.detail || {};
  const bits = [];
  if (d.born_alive != null) bits.push(`活仔 ${d.born_alive}`);
  if (d.stillborn) bits.push(`死胎 ${d.stillborn}`);
  if (d.mummified) bits.push(`木乃伊 ${d.mummified}`);
  if (d.weaned != null) bits.push(`離乳 ${d.weaned} 隻`);
  if (d.count != null) bits.push(`${d.count} 隻`);
  if (d.reason) bits.push(d.reason);
  if (d.boar_tag) bits.push(`公豬 ${d.boar_tag}`);
  if (d.session) bits.push(d.session);
  if (d.positive === true) bits.push("陽性");
  if (d.positive === false) bits.push("陰性");
  return bits.join(" ・ ");
}

// 胎次的色階。比照 Agriness 母豬卡:不用讀數字就知道這頭豬老不老。
export function parityTone(parity) {
  const n = Number(parity) || 0;
  if (n >= 8) return "par-r";
  if (n >= 6) return "par-y";
  return "par-g";
}

export function sowRow(sow) {
  return `
    <div class="sow-row" data-sow="${sow.id}">
      <div class="sow-tag">${escapeHtml(sow.earTag)}</div>
      <div class="sow-mid"><div class="sow-st">${escapeHtml(sow.breed || "—")}</div></div>
      <span class="par ${parityTone(sow.parity)}">${sow.parity} 胎</span>
      <span class="chev">›</span>
    </div>`;
}

// 時間軸最多畫幾筆。老母豬累積四十幾筆事件,全畫出來得滑很久,
// 而最舊的那幾筆日常上幾乎不會回頭看。
export const TIMELINE_LIMIT = 40;

/** 時間軸標題。
 *
 * 被截斷時一定要講清楚 —— 原本寫死「共 42 筆」卻只畫 40 列,數得出來
 * 的人會以為系統漏資料。數字與看得到的東西必須對得上(憲法第三條)。
 */
export function timelineCaption(total, limit = TIMELINE_LIMIT) {
  return total > limit
    ? `共 ${total} 筆 ・ 顯示最新 ${limit} 筆`
    : `共 ${total} 筆 ・ 最新在上`;
}

/** 事件的年份;格式不對就回空字串,不讓一筆壞資料炸掉整張卡。 */
function yearOf(event) {
  const text = String(event?.date ?? "");
  return /^\d{4}-/.test(text) ? text.slice(0, 4) : "";
}

/** 時間軸的一列。
 *
 * 直接接 .map(eventRow) 用,所以簽章配合 Array.map 的 (item, index, all)。
 *
 * 每列只印月-日,跨年時才補一個年份標題 —— 一頭母豬的時間軸動輒橫跨
 * 四五年(實測 2580 有 42 筆、2022 到 2026),每列都只有「03-25」的話
 * 根本分不出是哪一年的配種。反過來每列都印完整日期又太吵。
 *
 * 單獨呼叫(拿不到 index)時一律補上年份:寧可多印,也不要顯示一個
 * 看不出年份的日期。
 */
export function eventRow(event, index, all) {
  const detail = describeEvent(event);
  const tone = (event.type === "PL" || event.type === "DTH") ? "loss" : "ok";
  const year = yearOf(event);

  const previous = Array.isArray(all) && index > 0 ? all[index - 1] : undefined;
  const newYear = year && (previous === undefined || yearOf(previous) !== year);
  const heading = newYear ? `<div class="tl-year">${escapeHtml(year)}</div>` : "";

  return `${heading}
    <div class="ev ${event.excluded ? "" : tone}">
      <span class="d">${String(event.date).slice(5)}</span>${escapeHtml(eventName(event.type))}
      ${event.excluded ? '<span class="excl">未納入統計</span>' : ""}
      ${detail ? `<span class="n">${escapeHtml(detail)}</span>` : ""}
    </div>`;
}

// 工作分組。超過這個數量才收合 —— 太少就收合反而多一次點擊。
export const FOLD_AFTER = 12;

export function taskGroup(group, index) {
  const folded = group.tasks.length > FOLD_AFTER;
  const tags = group.tasks.map((t) => `
    <button class="etag" data-sow="${t.sowId}" title="${escapeHtml(t.why)}"
    >${escapeHtml(t.earTag)}</button>`).join("");

  return `
    <div class="tgrp">
      <div class="tgrp-h"><span>${escapeHtml(group.label)}</span>
        <span class="cnt2">${group.tasks.length} 頭</span></div>
      <div class="tgrp-why">${escapeHtml(group.tasks[0]?.why || "")}</div>
      <div class="tags${folded ? " tags-fold" : ""}" id="tags-${index}">${tags}</div>
      ${folded ? `<button class="foldbtn" data-fold="tags-${index}"
        >展開全部 ${group.tasks.length} 頭 ›</button>` : ""}
    </div>`;
}

// 提醒。依急迫度排序,產房不足與逾期未配種排最前面。
export function buildAlerts(data) {
  const rows = [];
  const pens = data.pens || { free: [], incoming: 0, short_by: 0 };
  const short = pens.short_by ?? pens.shortBy ?? 0;
  const open = data.openSows || [];

  if (short > 0) {
    rows.push({ tone: "urgent", title: "產房空間不足",
                sub: `14 天內 ${pens.incoming} 頭要移入,只剩 ${pens.free.length} 欄`,
                right: `缺 ${short} 欄` });
  }
  if (open.length) {
    rows.push({ tone: "urgent", title: `${open.length} 頭離乳後太久未配種`,
                sub: open.slice(0, 4).map((r) => r.ear_tag ?? r.earTag).join("、"),
                right: `最久 ${open[0].days} 天` });
  }
  if (pens.free.length) {
    rows.push({ tone: "ok", title: "產房尚有空欄",
                sub: pens.free.slice(0, 6).map((p) => p.name).join("、"),
                right: `${pens.free.length} 欄` });
  }
  return rows;
}

/** 「值得檢視」的一列。
 *
 * **一定要把理由畫出來**,不是只列耳號 —— 只給名單而不給依據,使用者
 * 無從判斷該不該採信,而這份名單看的東西恰好不是這個場主要的淘汰依據
 * (specs/v2-facts.md 第 10 條)。
 */
export function reviewRow(sow) {
  const reasons = sow.reasons.map((r) => `
    <div class="rv-r">
      <span class="rv-tag">${escapeHtml(r.label)}</span>
      <span class="rv-d">${escapeHtml(r.detail)}</span>
    </div>`).join("");

  return `
    <div class="rv">
      <div class="rv-h">
        <button class="etag" data-sow="${sow.sowId}">${escapeHtml(sow.earTag)}</button>
        <span class="rv-m">${sow.parity} 胎 ・ ${sow.litters} 窩有記錄</span>
      </div>
      ${reasons}
    </div>`;
}

/** 設定的一列。標題、說明、範圍全由後端給,前端不自己維護一份文字。 */
export function settingRow(field, value, fallback) {
  const changed = value !== fallback;
  return `
    <label class="set-row${changed ? " is-changed" : ""}">
      <div class="set-b">
        <div class="set-l">${escapeHtml(field.label)}
          ${changed ? `<span class="set-chg">已調整</span>` : ""}</div>
        <div class="set-d">${escapeHtml(field.hint)}</div>
      </div>
      <div class="set-in">
        <input type="number" id="set_${field.key}" value="${value}"
               min="${field.min}" max="${field.max}" inputmode="numeric">
        <span class="set-u">${escapeHtml(field.unit)}</span>
      </div>
    </label>`;
}

export function alertRow(row) {
  const icon = { urgent: "!", soon: "⏱", ok: "✓" }[row.tone] || "!";
  return `
    <div class="rem">
      <div class="rem-ic ic-${row.tone}">${icon}</div>
      <div class="rem-b"><div class="rem-t">${escapeHtml(row.title)}</div>
        <div class="rem-s">${escapeHtml(row.sub)}</div></div>
      <div class="rem-w">${escapeHtml(row.right)}</div>
    </div>`;
}
