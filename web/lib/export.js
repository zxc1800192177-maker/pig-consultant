// 匯出:把 /api/export 的整包資料整理成可以下載的檔案。
//
// 純字串組裝,不碰 DOM 也不碰 fetch —— 跟 record.js/v2.js 同一個理由,
// 這樣才測得到。真正的下載動作(Blob、<a download>)在 app.js。
//
// ## 為什麼欄位標題在前端組
//
// 「活仔數」「離乳仔豬評分」這些中文名字只有一份定義,就在 record.js 的
// RECORD_FORMS 裡。後端要自己再寫一份的話,以後改一個欄位名字就會有一邊
// 沒跟上,而且沒有任何錯誤提示 —— 匯出的表格默默用著舊名字。所以資料由
// 後端整包送來,標題由這裡從既有的表單定義算出來。

import { RECORD_FORMS } from "./record.js";
import { EVENT_NAMES, eventName } from "./v2.js";

/** Excel 開 UTF-8 的 CSV 需要開頭這個位元組順序記號,否則中文全變亂碼。
 *
 * 這不是可有可無的細節:牧場主打開檔案看到的是一堆問號還是「活仔數」,
 * 差別就在這三個位元組。匯入那邊本來就認得 utf-8-sig(見 importer.py 的
 * ENCODINGS),所以加了它也不影響匯出的檔案再拿回去匯入。
 */
export const BOM = "﻿";

/** 資料裡有、但 RECORD_FORMS 沒有的欄位。
 *
 * 兩個來源:PigCHAMP 匯進來的記錄帶著這個 app 的表單沒有的鍵(配種時段、
 * 寄養對象、進場那批的品種與父母耳號),以及換過做法之後留下來的舊鍵
 * (hernia_count 是改成勾選之前存的頭數)。
 *
 * 這些欄位**照樣要出現在匯出檔裡** —— 匯出少一欄就是把使用者的資料弄丟
 * 了,而他不會知道。
 */
export const EXTRA_LABELS = {
  session: "配種時段",
  partner_tag: "寄養對象耳號",
  hernia_count: "單睪/賀尼亞頭數(舊)",
  breed: "品種",
  birth_date: "出生日期",
  sire_tag: "父系耳號",
  dam_tag: "母系耳號",
  note: "備註",
};

/** 資料庫內部用的鍵,CSV 不列。
 *
 * 移欄的 detail 同時存了 `pen_id` 與 `pen_name`(見 server.py 的 _add_event)
 * —— 前者是資料表的流水號,後者才是牧場自己寫在欄位上的編號。表格是給人
 * 看的,多一欄「12」除了佔位置沒有任何用處。
 *
 * 這**不違反「匯出不掉東西」**:完整備份那一份是原封不動的 JSON,內部
 * 編號一個都沒少。兩份檔案的用途本來就不同。
 */
export const INTERNAL_KEYS = new Set(["pen_id"]);

/** 每個 detail 欄位的中文標題,以及固定的欄位順序。
 *
 * 順序照 EVENT_NAMES(配種、驗孕、分娩、離乳……),也就是生產週期的順序
 * —— 打開表格由左往右讀,就是一頭母豬走過的流程。
 *
 * 同一個鍵出現在好幾種事件裡是**刻意合併成一欄**的:`reason` 在仔豬死亡
 * 是「壓死」、在淘汰是「年齡太大」,擺在同一欄「原因」底下篩選一次就看得
 * 完,不必在三個幾乎全空的欄位之間切換。
 */
export function detailColumns(events = []) {
  // **只開資料裡真的用到的欄位。** 把所有事件的欄位全列出來的話,公豬那
  // 張表會拖著「死胎」「離乳頭數」一路到最右邊全是空的,母豬那張也會多出
  // 採精的四欄 —— 三十幾欄裡有二十欄永遠空白,要看的那幾欄反而找不到。
  const present = new Set();
  for (const e of events) {
    for (const key of Object.keys(e?.detail || {})) present.add(key);
  }

  const labels = new Map();
  const take = (key, label) => {
    if (present.has(key) && !labels.has(key)) labels.set(key, label);
  };

  // 順序照表單定義走,所以留下來的欄位仍然是生產週期的順序。
  for (const code of [...Object.keys(EVENT_NAMES), ...Object.keys(RECORD_FORMS)]) {
    const spec = RECORD_FORMS[code];
    // 種豬進場填的是**一頭新的豬**,那些欄位寫進 sows 表而不是事件的
    // detail(見 server.py 的 _add_sow)。匯入進來的同一批資料用的是別的
    // 鍵(breed / birth_date,見 EXTRA_LABELS),對不起來。
    if (!spec || spec.target === "new") continue;
    for (const field of spec.fields) take(field.key, field.label);
  }
  for (const [key, label] of Object.entries(EXTRA_LABELS)) take(key, label);

  // 上面兩份清單都沒有的鍵,拿原始鍵當標題 —— 不好看,但總比整欄消失好。
  // 匯出的第一守則是不掉東西。
  for (const key of present) take(key, key);

  return [...labels.entries()]
    .filter(([key]) => !INTERNAL_KEYS.has(key))
    .map(([key, label]) => ({ key, label }));
}

/** 存進資料庫的值 → 使用者看得懂的字。
 *
 * 區域存的是 `farrowing`、發情穩定度存的是 `unstable`,那是給程式判斷用
 * 的穩定值(見 record.js 的說明)。表格裡照樣印英文的話,牧場主打開檔案
 * 會看到一欄自己從來沒在畫面上見過的字。
 *
 * 對照表**從表單定義本身算出來**,不另外抄一份 —— 選項改了名字,匯出跟
 * 著改,不會有一邊沒跟上。
 */
export function detailValueLabels() {
  const map = new Map();
  for (const spec of Object.values(RECORD_FORMS)) {
    for (const field of spec.fields) {
      if (field.type === "bool") {
        map.set(field.key, new Map([[true, field.yes], [false, field.no]]));
        continue;
      }
      if (field.type !== "choice" && field.type !== "tri") continue;
      const pairs = field.options
        .filter((o) => typeof o === "object")
        .map((o) => [o.value, o.label]);
      if (pairs.length) map.set(field.key, new Map(pairs));
    }
  }
  return map;
}

const PLAIN_NUMBER = /^-?\d+(\.\d+)?$/;

/** 一格的內容。
 *
 * 逗號、引號、換行都得包起來,否則一個「壓死,體弱」就會把整列錯開一格。
 *
 * 開頭的 = + - @ 另外處理:Excel 會把它們當公式執行,那是 CSV 注入 ——
 * 使用者自己打的原因欄位是自由文字,而這個檔案會在別人的電腦上打開。
 * 純數字放行,不然負數會被加上一個看得見的引號。
 */
export function csvCell(value) {
  if (value === null || value === undefined) return "";
  if (value === true) return "是";
  if (value === false) return "否";

  let text = String(value);
  if (/^[=+\-@\t\r]/.test(text) && !PLAIN_NUMBER.test(text)) text = "'" + text;
  if (/[",\n\r]/.test(text)) text = '"' + text.replace(/"/g, '""') + '"';
  return text;
}

/** 一格的值:認得的代碼換成中文,其餘原樣。
 *
 * 對照表查不到就回原值 —— 匯進來的舊資料可能存著現在的選項清單裡沒有的
 * 原因(「其他」那一欄本來就是自由文字),硬要對照只會把它變成空白。
 */
export function detailValue(labels, key, value) {
  if (value === null || value === undefined) return value;
  const table = labels.get(key);
  if (!table || !table.has(value)) return value;
  return table.get(value);
}

/** 標題列 + 資料列 → CSV 文字。
 *
 * 行尾用 CRLF:RFC 4180 這樣定,而且舊版 Excel 對純 \n 會整份讀成一列。
 */
export function toCsv(headers, rows) {
  const lines = [headers.map(csvCell).join(",")];
  for (const row of rows) lines.push(row.map(csvCell).join(","));
  return BOM + lines.join("\r\n") + "\r\n";
}

/** 母豬事件 + 肉豬死亡,依日期排好。一筆記錄一列,detail 攤平成欄位。 */
export function eventCsv(payload) {
  const events = [...(payload.events || []), ...(payload.marketDeaths || [])]
    .sort((a, b) => String(a.date).localeCompare(String(b.date))
                    || (a.id || 0) - (b.id || 0));
  const cols = detailColumns(events);
  const values = detailValueLabels();
  const headers = ["耳號", "事件", "日期", ...cols.map((c) => c.label), "納入統計"];

  const rows = events.map((e) => [
    e.earTag || "",
    eventName(e.type),
    e.date || "",
    ...cols.map((c) => detailValue(values, c.key, e.detail?.[c.key])),
    // 標為離群的記錄仍然在檔案裡(不刪使用者的資料),但要看得出來它沒有
    // 進統計 —— 否則拿去自己算的人會跟系統的數字對不起來,而且找不出為
    // 什麼。
    e.excluded ? "否" : "是",
  ]);
  return toCsv(headers, rows);
}

/** 公豬事件:採精、死亡。跟母豬事件分成兩張表,欄位完全不同。 */
export function boarEventCsv(payload) {
  const events = payload.boarEvents || [];
  const cols = detailColumns(events);
  const values = detailValueLabels();
  const headers = ["耳號", "事件", "日期", ...cols.map((c) => c.label)];
  const rows = events.map((e) => [
    e.earTag || "", eventName(e.type), e.date || "",
    ...cols.map((c) => detailValue(values, c.key, e.detail?.[c.key])),
  ]);
  return toCsv(headers, rows);
}

// 母豬/公豬的 status 欄位只有這三個值(見 server.py:「culled if SAL else dead」)。
// 注意這跟 core/labels.py 的 SOW_STATE_LABELS 是兩回事 —— 那個講的是
// 懷孕中/哺乳中/待配種,是由事件推算出來的繁殖狀態,不是資料表的欄位。
const STATUS_LABELS = { active: "在場", culled: "已淘汰", dead: "已死亡" };

function statusLabel(status) {
  return STATUS_LABELS[status] || status || "";
}

/** 母豬名單:一頭一列,身分資料而不是事件。 */
export function sowCsv(payload) {
  const headers = ["耳號", "品種", "胎次", "狀態", "進場日期", "出生日期",
                   "父系耳號", "母系耳號", "耳號待確認"];
  const rows = (payload.sows || []).map((s) => [
    s.earTag, s.breed, s.parity, statusLabel(s.status),
    s.entryDate, s.birthDate, s.sireTag, s.damTag,
    // 耳號看不清楚時先用配種日期記著的那些。空白比「否」好讀 ——
    // 這一欄要一眼看出「哪幾頭還沒認回來」,不是每一列都要回答。
    s.isUnknown ? "是" : "",
  ]);
  return toCsv(headers, rows);
}

/** 公豬名單。 */
export function boarCsv(payload) {
  const headers = ["耳號", "品種", "狀態", "進場日期", "父系耳號", "母系耳號"];
  const rows = (payload.boars || []).map((b) => [
    b.earTag, b.breed, statusLabel(b.status), b.entryDate, b.sireTag, b.damTag,
  ]);
  return toCsv(headers, rows);
}

/** 完整備份:後端送來什麼就存什麼。
 *
 * 不整理、不改名 —— 它的用途是「哪天資料沒了,這個檔案裡什麼都還在」,
 * 所以連 id、seq、excluded 都原樣留著。CSV 那幾份是給人看的,這一份是
 * 給程式讀的。
 */
export function backupJson(payload) {
  return JSON.stringify(payload, null, 2);
}

const FILE_LABELS = {
  events: "事件明細", sows: "母豬名單", boars: "公豬名單",
  boarEvents: "公豬事件", backup: "完整備份",
};

/** 檔名。帶日期 —— 存了好幾份之後才分得出哪份是哪天匯的。 */
export function exportFileName(kind, today) {
  const date = String(today || "").slice(0, 10);
  const ext = kind === "backup" ? "json" : "csv";
  return `豬豬顧問-${FILE_LABELS[kind] || kind}-${date}.${ext}`;
}

/** 這次會匯出多少東西 —— 按下按鈕之前要知道自己拿到的是什麼。 */
export function exportSummary(payload) {
  const events = (payload.events || []).length
                 + (payload.marketDeaths || []).length;
  const bits = [
    `${(payload.sows || []).length} 頭母豬`,
    `${(payload.boars || []).length} 頭公豬`,
    `${events} 筆事件`,
  ];
  if ((payload.boarEvents || []).length) {
    bits.push(`${payload.boarEvents.length} 筆公豬記錄`);
  }
  return bits.join(" ・ ");
}
