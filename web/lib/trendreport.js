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

/** 一格數字對照全國常模的級距 class。
 *
 * 只有全國前 25%(good)與後 25%(poor)上色,中間 50% 留白 —— 這份
 * 報告有 55 個指標乘上最多 12 期,每一格都有顏色等於沒有重點。
 *
 * 用獨立的 .trend-good/.trend-poor 而不是母豬卡那組 .t-good/.t-poor:
 * 那是填滿底色的徽章,適合一頁只有七個的場合;整張表格填滿色塊會蓋掉
 * 數字本身。這裡只染文字顏色,跟 PigCHAMP 原版一樣。
 */
export function tierClass(tier) {
  if (tier === "good") return " trend-good";
  if (tier === "poor") return " trend-poor";
  return "";
}

/** 一個區段(配種/分娩/仔豬死亡/離乳/在養與異動)畫成一張表。
 *
 * 每個區段各自成表而不是全部塞進一張大表 —— 55 個指標排在一起,使用者
 * 找不到「配種」跟「分娩」的分界在哪裡,表格再寬也沒有用。
 *
 * `hasSummary` 為假時整個不畫「總計/平均」兩欄 —— 期間不連續時後端算
 * 不出整段的比率(見 trend._summary),畫兩欄破折號只是佔位。
 */
export function trendSectionTable(section, periods, hasSummary = false) {
  const header = periods.map((p) => `<th>${escapeHtml(p.label)}</th>`).join("");
  const summaryHead = hasSummary
    ? '<th class="trend-sum trend-sum-first">總計</th><th class="trend-sum">平均</th>'
    : "";
  const rows = section.rows.map((r) => {
    const tiers = r.tiers || [];
    const cells = r.values
      .map((v, i) => `<td class="trend-v${tierClass(tiers[i])}">`
                     + `${trendValue(v, r.digits, r.unit)}</td>`)
      .join("");
    const summary = hasSummary
      ? `<td class="trend-sum trend-sum-first">`
        + `${trendValue(r.total, r.digits, r.unit)}</td>`
        + `<td class="trend-sum${tierClass(r.avgTier)}">`
        + `${trendValue(r.avg, r.digits, r.unit)}</td>`
      : "";
    const tone = changeTone(r.change);
    const change = r.change
      ? `<span class="trend-chg tone-${tone}">${changeText(r.change, r.unit, r.digits)}</span>`
      : '<span class="v-none">—</span>';
    return `<tr><td class="trend-metric">${escapeHtml(r.label)}</td>${cells}`
           + `${summary}<td>${change}</td></tr>`;
  }).join("");

  return `
    <div class="section-label">${escapeHtml(section.label)}</div>
    <div class="table-scroll">
      <table>
        <thead><tr><th>指標</th>${header}${summaryHead}<th>變化</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

/** 顏色的圖例。**沒有圖例的顏色等於沒有意義** —— 使用者看到一格是紅的,
 * 得知道那是在跟誰比、比的是哪一年的資料,才有辦法決定要不要當一回事。
 */
export function trendLegend(source) {
  if (!source) return "";
  const who = `${escapeHtml(String(source.year))} 年${escapeHtml(source.name || "")}`;
  const farms = source.farms ? `(全國 ${source.farms} 場)` : "";
  return `<p class="hint trend-legend">
    <span class="trend-good">綠色</span>=全國前 25%、
    <span class="trend-poor">紅色</span>=全國後 25%,中間 50% 不上色。
    基準為 ${who}${farms};沒有對應常模的指標不上色。
  </p>`;
}

/** 整份報告。沒有任何區段代表這段期間一筆記錄都沒有 —— 印一堆空表格
 * 只會讓人以為系統壞了,不如直接說清楚。
 */
export function trendReportGrid(report) {
  if (!report.sections || !report.sections.length) {
    return '<p class="hint">這段期間沒有記錄,算不出任何指標。</p>';
  }
  const tables = report.sections
    .map((s) => trendSectionTable(s, report.periods, report.hasSummary))
    .join("");
  return tables + trendLegend(report.normSource);
}

/** 完整版下載用的 CSV。一列一個指標,一欄一個期間 —— 跟畫面上的表格
 * 同一個形狀,只是把好幾個區段接成一張,拿去 Excel 自己畫圖或再篩選。
 *
 * 沿用 export.js 的 toCsv/csvCell,不另外寫一套逃脫規則 —— 兩邊都是
 * 「這個系統匯出的表格要怎麼變成合法 CSV」,答案只該有一份。
 */
export function trendCsv(report) {
  const num = (v, digits) => (v === null || v === undefined
    ? "" : Number(v.toFixed(digits)));
  const summaryHead = report.hasSummary ? ["總計", "平均"] : [];
  const headers = ["區段", "指標", "單位",
                   ...report.periods.map((p) => p.label),
                   ...summaryHead, "變化(絕對值)", "變化(%)"];
  const rows = [];
  for (const section of report.sections || []) {
    for (const r of section.rows) {
      rows.push([
        section.label, r.label, r.unit,
        ...r.values.map((v) => num(v, r.digits)),
        ...(report.hasSummary ? [num(r.total, r.digits), num(r.avg, r.digits)] : []),
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

/** PDF 版的檔名,跟 CSV 版共用同一份期別中文標籤 —— 一份定義,不是
 * 兩份輸出格式各寫一次「週=週報、月=月報……」的對照表。
 */
export function trendPdfFileName(grain, start, end) {
  const label = { week: "週報", month: "月報", quarter: "季報", year: "年報" }[grain] || grain;
  return `豬豬顧問-生產趨勢${label}-${start}~${end}.pdf`;
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
