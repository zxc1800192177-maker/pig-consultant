// 生產性能趨勢報告的畫面渲染與 CSV 匯出。純字串組裝,不碰 DOM 也不碰
// fetch —— 跟 record.js/export.js 同一個理由,這樣才測得到。
//
// 後端(trend.py)已經決定好每一列要不要出現、變化算不算得出來
// (見 trend_report 的 _change),這裡只負責把那個決定畫出來,不重新判斷。

import { escapeHtml } from "./markdown.js";
import { csvCell, toCsv } from "./export.js";

/** 一格數值。沒有記錄印 —,不是 0 —— 兩者意思完全不同(憲法第三條)。 */
export function trendValue(value, digits, unit) {
  if (value === null || value === undefined) {
    return '<span class="v-none">—</span>';
  }
  return `${value.toFixed(digits)}<span class="u">${escapeHtml(unit)}</span>`;
}

/** 顏色跟既有的分級色調共用同一組 class(.tone-good/.tone-critical/
 * .tone-neutral),不是這裡另外發明一套配色 —— 兩處都是「這個數字好不好」
 * 的視覺判斷,理由相同。
 */
export function changeTone(change) {
  if (!change || change.improved === null || change.improved === undefined) {
    return "neutral";
  }
  return change.improved ? "good" : "critical";
}

/** 最後一期跟第一期的差,一句話。
 *
 * 方向的箭頭跟著**數字本身的正負**(漲用 ▲、跌用 ▼),顏色才跟著
 * `improved`(這個方向算不算進步)—— 兩者不是同一件事:死胎率的箭頭
 * 跟顏色會是反過來的(▲卻是紅色),故意讓使用者一眼看出「漲了,而且
 * 這是壞事」,不是靠顏色掩蓋方向。
 */
export function changeText(change, unit, digits) {
  if (!change) return "";
  const arrow = change.delta > 0 ? "▲" : change.delta < 0 ? "▼" : "→";
  const sign = change.delta > 0 ? "+" : "";
  const delta = `${sign}${change.delta.toFixed(digits)}${unit}`;
  const pct = change.pct === null || change.pct === undefined
    ? ""
    : ` (${change.pct > 0 ? "+" : ""}${change.pct.toFixed(0)}%)`;
  return `${arrow} ${delta}${pct}`;
}

/** 一個區段(配種/分娩/仔豬死亡/離乳/在養與異動)畫成一張表。
 *
 * 每個區段各自成表而不是全部塞進一張大表 —— 55 個指標排在一起,使用者
 * 找不到「配種」跟「分娩」的分界在哪裡,表格再寬也沒有用。
 */
export function trendSectionTable(section, periods) {
  const header = periods.map((p) => `<th>${escapeHtml(p.label)}</th>`).join("");
  const rows = section.rows.map((r) => {
    const cells = r.values
      .map((v) => `<td>${trendValue(v, r.digits, r.unit)}</td>`)
      .join("");
    const tone = changeTone(r.change);
    const change = r.change
      ? `<span class="trend-chg tone-${tone}">${changeText(r.change, r.unit, r.digits)}</span>`
      : '<span class="v-none">—</span>';
    return `<tr><td class="trend-metric">${escapeHtml(r.label)}</td>${cells}<td>${change}</td></tr>`;
  }).join("");

  return `
    <div class="section-label">${escapeHtml(section.label)}</div>
    <div class="table-scroll">
      <table>
        <thead><tr><th>指標</th>${header}<th>變化</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

/** 整份報告。沒有任何區段代表這段期間一筆記錄都沒有 —— 印一堆空表格
 * 只會讓人以為系統壞了,不如直接說清楚。
 */
export function trendReportGrid(report) {
  if (!report.sections || !report.sections.length) {
    return '<p class="hint">這段期間沒有記錄,算不出任何指標。</p>';
  }
  return report.sections.map((s) => trendSectionTable(s, report.periods)).join("");
}

// ── 列印/PDF ──
//
// 跟畫面上的表格是同一份資料,但**不能沿用同一段 HTML**:畫面用的
// .v-none / .trend-chg.tone-* 顏色綁在這個 app 自己的深色主題 CSS 變數
// 上,而列印是「離開這個 app 的脈絡,變成一張紙或一份 PDF」——
// 使用者存好的 PDF 幾年後打開,不會有這個網站的樣式表跟著。顏色因此
// 用行內樣式直接寫死,不依賴任何外部定義。

const PRINT_TONE_COLOR = { good: "#1a7f37", critical: "#c0392b", neutral: "#666" };

/** 列印用的一格數值,跟 trendValue 同樣的規則(沒有記錄印 —,不是 0),
 * 只是顏色用行內樣式。
 */
export function trendPrintValue(value, digits, unit) {
  if (value === null || value === undefined) {
    return '<span style="color:#999">—</span>';
  }
  return `${value.toFixed(digits)}${escapeHtml(unit)}`;
}

/** 列印用的變化欄。文字組裝直接複用 changeText —— 那段邏輯(箭頭跟數字
 * 正負、正號、百分比)只該有一份,不能因為換了輸出目標就重寫一次。
 */
export function trendPrintChange(change, unit, digits) {
  if (!change) return '<span style="color:#999">—</span>';
  const color = PRINT_TONE_COLOR[changeTone(change)];
  return `<span style="color:${color};font-weight:600">${escapeHtml(changeText(change, unit, digits))}</span>`;
}

/** 一個區段的列印表格。 */
export function trendPrintSection(section, periods) {
  const header = periods.map((p) => `<th>${escapeHtml(p.label)}</th>`).join("");
  const rows = section.rows.map((r) => {
    const cells = r.values
      .map((v) => `<td>${trendPrintValue(v, r.digits, r.unit)}</td>`)
      .join("");
    return `<tr><td class="pm">${escapeHtml(r.label)}</td>${cells}` +
           `<td>${trendPrintChange(r.change, r.unit, r.digits)}</td></tr>`;
  }).join("");

  return `
    <h3>${escapeHtml(section.label)}</h3>
    <table class="ptable">
      <thead><tr><th>指標</th>${header}<th>變化</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

/** 整份可列印的報告(含抬頭)。
 *
 * 抬頭資訊比照使用者原本從 PigCHAMP 印出來的那份 —— 牧場名稱、期間、
 * 產生時間,好讓這張紙離開螢幕之後還看得出是誰的資料、算到什麼時候。
 * 不放「目標值」欄:全國常模這個 app 只服務生產健檢的場級對比
 * (已確認的設計決定 #3),這份報告是牧場跟自己歷史比,不跟全國比。
 */
export function trendPrintReport(report, meta = {}) {
  const periods = report.periods || [];
  const range = periods.length
    ? `${periods[0].label} ~ ${periods[periods.length - 1].label}(共 ${periods.length} 期)`
    : "沒有資料";
  const metaLine = [meta.farmName, range, meta.generatedAt ? `產生於 ${meta.generatedAt}` : ""]
    .filter(Boolean).map(escapeHtml).join(" ・ ");

  const body = report.sections && report.sections.length
    ? report.sections.map((s) => trendPrintSection(s, periods)).join("")
    : "<p>這段期間沒有記錄,算不出任何指標。</p>";

  return `
    <h1>豬豬顧問 生產性能趨勢分析報告</h1>
    <p class="pmeta">${metaLine}</p>
    ${body}`;
}

/** 完整版下載用的 CSV。一列一個指標,一欄一個期間 —— 跟畫面上的表格
 * 同一個形狀,只是把好幾個區段接成一張,拿去 Excel 自己畫圖或再篩選。
 *
 * 沿用 export.js 的 toCsv/csvCell,不另外寫一套逃脫規則 —— 兩邊都是
 * 「這個系統匯出的表格要怎麼變成合法 CSV」,答案只該有一份。
 */
export function trendCsv(report) {
  const headers = ["區段", "指標", "單位",
                   ...report.periods.map((p) => p.label), "變化(絕對值)", "變化(%)"];
  const rows = [];
  for (const section of report.sections || []) {
    for (const r of section.rows) {
      rows.push([
        section.label, r.label, r.unit,
        ...r.values.map((v) => (v === null || v === undefined
          ? "" : Number(v.toFixed(r.digits)))),
        r.change ? Number(r.change.delta.toFixed(r.digits)) : "",
        r.change && r.change.pct !== null && r.change.pct !== undefined
          ? Number(r.change.pct.toFixed(1)) : "",
      ]);
    }
  }
  return toCsv(headers, rows);
}

export function trendCsvFileName(grain, start, end) {
  const label = { week: "週報", month: "月報", quarter: "季報", year: "年報" }[grain] || grain;
  return `豬豬顧問-生產趨勢${label}-${start}~${end}.csv`;
}

// 期別按鈕的中文標籤。設定頁、記錄頁的常數就近放在同一個檔案裡,這裡也
// 一樣 —— 畫面上的字只該有一份定義。
export const GRAIN_LABELS = { week: "週", month: "月", quarter: "季", year: "年" };

// 每期大約幾天,用來從「這個期別最多看幾期」反推一個夠早的起點,
// 讓「下載完整版」不必先問伺服器一次涵蓋範圍有多大。
//
// 刻意抓得比真正的上限保守一點(EXPORT_LOOKBACK_PERIODS 而非
// MAX_TREND_PERIODS 本身)——月份長短、閏年這些因素會讓實際切出來的
// 期數跟天數粗估有一兩期的落差,抓保守才不會因為多切一期就整個被拒絕。
const APPROX_DAYS = { week: 7, month: 30, quarter: 91, year: 365 };
export const EXPORT_LOOKBACK_PERIODS = 110;

export function widestStart(end, grain) {
  const days = APPROX_DAYS[grain] * EXPORT_LOOKBACK_PERIODS;
  const d = new Date(`${end}T12:00:00`);
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}
