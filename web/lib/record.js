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

/** 三個區域,對應 schedule.py 的 ZONES / core/labels.py 的 ZONE_LABELS。
 * 只有這三個值,寫死在這裡跟寫死發情穩定度的三個選項是同一個道理。
 */
export const ZONE_OPTIONS = [
  { value: "mating", label: "配種區" },
  { value: "gestation", label: "待產區" },
  { value: "farrowing", label: "產房" },
];

/** 原因選單裡的「其他」——選到這個時要跳出一個框給使用者打字說明實際
 * 原因,不能就這樣存一個不具體的「其他」進資料庫。固定選項本來就是從
 * 實際記錄裡最常見的幾種原因取出來的(見 PL/SAL 的欄位定義),「其他」
 * 存在的意義正是接住那些選不到的長尾,選了卻沒有地方寫清楚是什麼,
 * 這筆資料就白記了。
 */
export const OTHER_REASON = "其他";

/** 這個 choice 欄位是不是有「其他」選項,要不要準備打字框給它用。 */
export function hasOtherOption(field) {
  if (field.type !== "choice") return false;
  return field.options.some((o) => (typeof o === "string" ? o : o.value) === OTHER_REASON);
}

/** 每種事件要填什麼。`type` 決定畫成什麼元件與怎麼收值。 */
export const RECORD_FORMS = {
  MT: {
    // 同一天、同一隻公豬,常常是一整批母豬一起配 —— 逐頭開表單重打一次
    // 公豬耳號跟發情穩定度太沒效率(使用者要求)。multiSow: true 讓耳號
    // 欄位改成可以連續加很多筆,其餘欄位(公豬、發情穩定度)整批共用同
    // 一組值,一次送出多筆事件。
    // 一頭母豬一次發情通常連配 2–3 天、一天一次,而且**每天可能換不同
    // 公豬**(使用者說明),整批母豬又是同步進行的。所以日期與公豬不是
    // 整張表單共用一組,而是「每一次配種」各有一組 —— multiService: true
    // 讓表單長出可增減的配種列(見 app.js 的 serviceRowsField)。
    //
    // 使用者是**整批配完才一次記進來**的,所以預設就開兩列,不是開一列
    // 再叫他自己加。
    label: "配種", target: "sow", multiSow: true, multiService: true,
    fields: [
      // perService:這一項屬於「某一次配種」而不是整批,所以表單不把它
      // 畫在共用區,而是每一列配種各一個(見 app.js 的 serviceRowsField)。
      // 仍然留在 fields 裡 —— 它是 MT 事件 detail 的一部分,buildDetail
      // 要認得它、驗證它,不能因為換了畫法就從驗證路徑上消失。
      { key: "boar_tag", label: "公豬", type: "boar", perService: true },
      // 配種當下觀察到的發情徵狀。跟離乳評分同樣的道理:主觀判斷,
      // **可以不評**,沒填不補值(憲法第三條第 6 款)。
      // 這一項整批共用:它描述的是這次發情的狀態,不是某一天的。
      { key: "estrus_stability", label: "發情穩定度", type: "tri",
        options: ESTRUS_STABILITY_OPTIONS,
        hint: "配種當下的發情徵狀,不確定可以留空" },
    ],
  },
  FW: {
    perSowRows: true,
    label: "分娩", target: "sow",
    fields: [
      { key: "born_alive", label: "活仔數", type: "int", min: 0, max: 30, required: true },
      { key: "stillborn", label: "死胎", type: "int", min: 0, max: 30 },
      { key: "mummified", label: "木乃伊", type: "int", min: 0, max: 30 },
      // 分娩當天常會併窩、寄養調整,留給這頭母豬養的頭數因此可能跟活仔數
      // 不一樣(使用者決定)。跟離乳評分同樣的道理:**可以不填**,沒填
      // 不補值(憲法第三條第 6 款)—— 不代表「跟活仔數一樣」,只是沒記。
      { key: "raised", label: "飼養頭數", type: "int", min: 0, max: 30,
        hint: "分娩當天併窩、寄養調整後,實際留給這頭母豬養的頭數,可以不填" },
      // 使用者決定:預設沒有助產,有的話使用者自己勾選 —— 跟上面幾個
      // 「沒填代表沒記」的欄位不同,這裡沒勾**就是**答案(沒有助產),
      // 不是「不確定」,所以是有預設值的勾選框,不是留白的欄位。
      { key: "assisted", label: "有助產", type: "checkbox" },
    ],
  },
  WN: {
    perSowRows: true,
    label: "離乳", target: "sow",
    fields: [
      { key: "weaned", label: "離乳頭數", type: "int", min: 0, max: 30, required: true },
      // 使用者決定:預設 0,大多數窩沒有這個問題,不必每筆都手動填 0 ——
      // 有的話使用者自己改數字。跟上面的離乳頭數不同,這裡沒改就是
      // 明確的「0 隻」,不是沒填(欄位一開始就帶著 0)。
      { key: "hernia_count", label: "單睪/賀尼亞頭數", type: "int", min: 0, max: 30,
        default: 0 },
      // 使用者要求的自評項目。**可以不評** —— 沒評分顯示「—」,不補值。
      { key: "wean_score", label: "離乳仔豬評分", type: "score",
        hint: "1~5 分,由你自己評。不想評可以留空" },
    ],
  },
  PD: {
    // 驗孕整批一起做,而且結果通常一致 —— 唯一的欄位就是「有沒有懷孕」,
    // 整批共用一個值完全合理,所以用跟配種一樣的做法(耳號連續加入多筆,
    // 欄位共用)。分娩、離乳那些「每頭數字不同」的則是一頭一列,見
    // perSowRows。
    label: "驗孕", target: "sow", multiSow: true,
    fields: [{ key: "positive", label: "結果", type: "bool",
               yes: "有懷孕", no: "沒懷孕", required: true }],
  },
  FON: {
    perSowRows: true,
    label: "寄養移入", target: "sow",
    fields: [{ key: "count", label: "頭數", type: "int", min: 1, max: 30, required: true }],
  },
  FOF: {
    perSowRows: true,
    label: "寄養移出", target: "sow",
    fields: [{ key: "count", label: "頭數", type: "int", min: 1, max: 30, required: true }],
  },
  PL: {
    // 每頭的頭數與原因都不一樣(這窩壓死三隻、那窩下痢一隻),不能整批
    // 共用一組值 —— 共用等於把第一頭的數字複製給所有豬。
    perSowRows: true,
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
    // 使用者決定跟公豬死亡合併成同一個事件,不分公母 —— 選母豬還是
    // 公豬由記錄當下的切換鈕決定(target: "either"),不是表單本身寫死。
    perSowRows: true,
    label: "種豬死亡", target: "either",
    fields: [{ key: "reason", label: "原因", type: "text" }],
  },
  // 流產沒有任何欄位,只有「哪幾頭、哪一天」—— 用耳號連續加入多筆的
  // 做法(跟驗孕一樣)最單純,不必為了零個欄位長出一堆列。
  AB: { label: "流產", target: "sow", multiSow: true, fields: [] },
  MKD: {
    // 肉豬(育肥豬)不掛在任何一頭母豬或公豬身上 —— 牠們本來就不是這個
    // 系統追蹤身分的對象,沒有耳號、沒有進場記錄。target: "none" 讓表單
    // 不畫耳號欄位,只記使用者要的三件事:日期、原因、公斤數
    // (使用者決定)。
    // 一次死好幾頭時每頭的原因與重量都不同。沒有耳號,所以列裡不畫
    // 動物選擇器,一列就是一頭肉豬。
    perSowRows: true,
    label: "肉豬死亡", target: "none",
    fields: [
      { key: "reason", label: "死亡原因", type: "text", required: true },
      { key: "weight_kg", label: "重量", type: "decimal", min: 0, max: 500,
        required: true, hint: "公斤(kg)" },
    ],
  },
  SAL: {
    // 一批淘汰的原因常常不一樣(一頭年齡太大、一頭肢蹄問題),所以原因
    // 放在各自的列裡,不整批共用 —— 共用會把一個沒發生的原因記到別頭上。
    perSowRows: true,
    label: "淘汰", target: "sow",
    fields: [{ key: "reason", label: "原因", type: "choice", required: true,
               // 同樣取自實際記錄:年齡太大 48.0%、不能懷孕 18.6%
               options: ["年齡太大", "不能懷孕", "子宮蓄膿", "生產性能差",
                         "肢蹄問題", "其他"] }],
  },
  MV: {
    // 整批移進產房是同一個區域,但**每頭進不同的欄位** —— 所以區域整批
    // 共用一個值,欄位編號每頭一個。這正是這個事件最花時間的地方:
    // 二十頭要開二十次表單,每次都重選一樣的區域。
    perSowRows: true,
    label: "移欄", target: "sow",
    // 直接打欄位編號,不必先到設定頁一個一個新增 —— 一區動輒幾百個
    // 欄位,要求先手動建一輪根本不會有人做(使用者要求)。第一次打到
    // 的編號會自動建立,之後同一區打同樣編號會找到同一個欄位。
    fields: [
      { key: "zone", label: "區域", type: "choice", required: true,
        options: ZONE_OPTIONS, shared: true },
      { key: "pen_name", label: "欄位編號", type: "pen", required: true,
        hint: "直接輸入,新編號會自動建立" },
    ],
  },
  SC: {
    // 採精量、活力、濃度每頭都是自己的數字。target 是 boar,所以列裡
    // 畫的是公豬耳號,不是母豬。
    perSowRows: true,
    label: "採精", target: "boar",
    fields: [
      { key: "volume", label: "採精量", type: "int", min: 1, max: 999, required: true,
        hint: "毫升(ml)" },
      { key: "motility", label: "精蟲活力", type: "int", min: 0, max: 100, hint: "%" },
      { key: "concentration", label: "精液濃度", type: "decimal", min: 0, max: 99,
        hint: "億/mL" },
      { key: "doses", label: "可分裝劑量", type: "int", min: 0, max: 999 },
    ],
  },
  GA: {
    // 一批進場常常是同一天、同一個來源、同一個品種(使用者說明),所以
    // 一次建好幾頭:耳號等每頭不同的欄位一頭一列,品種整批共用一個值,
    // 不必打二十次。進場日期跟其他事件一樣是整張表單共用的那個日期欄。
    perSowRows: true,
    label: "種豬進場", target: "new",
    // 母豬跟公豬共用同一張表單(用上面的 母豬/公豬 切換鈕決定送去哪個
    // API),父母耳號兩邊都可能知道也可能不知道,所以是選填。
    fields: [
      { key: "earTag", label: "耳號", type: "text", required: true },
      // shared:整批共用一個值,畫在共用區而不是每一列各一個。
      { key: "breed", label: "品種", type: "text", shared: true },
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
  MV: "原本所在的欄位會空出來",
};

export function formFor(code) {
  return RECORD_FORMS[code] || null;
}

/** 這種事件是不是「新增一頭豬」而不是「在某頭豬身上記一筆」。 */
export function createsNewAnimal(code) {
  return formFor(code)?.target === "new";
}

/** 這種事件記在公豬身上,不是母豬 —— 採精。 */
export function targetsBoar(code) {
  return formFor(code)?.target === "boar";
}

/** 這種事件記在母豬或公豬身上都可以,記錄當下用切換鈕決定 —— 種豬死亡
 * (使用者決定跟公豬死亡合併成同一個事件,不必再分兩顆按鈕)。
 */
export function targetsEither(code) {
  return formFor(code)?.target === "either";
}

/** 這種事件不掛在任何一頭豬身上 —— 目前只有肉豬死亡:肉豬不是這個
 * 系統追蹤身分的對象,沒有耳號可選(使用者決定)。
 */
export function targetsNothing(code) {
  return formFor(code)?.target === "none";
}

/** 這種事件可以一次對多頭母豬記錄同一組內容 —— 目前只有配種:同一天、
 * 同一隻公豬,常常是一整批母豬一起配(使用者要求)。
 */
export function supportsMultiSow(code) {
  return Boolean(formFor(code)?.multiSow);
}

/** 這種事件是「同一次發情連配好幾天」,每一天各有日期與公豬 ——
 * 表單因此不用整張共用的日期欄,改用可增減的配種列。目前只有配種。
 */
export function supportsMultiService(code) {
  return Boolean(formFor(code)?.multiService);
}

/** 這種事件一次記多頭,但**每頭的欄位各自不同** —— 分娩的活仔數、離乳的
 * 離乳頭數,每頭都是自己的數字,不能像配種的公豬那樣整批共用一組值。
 *
 * 所以表單長成「一頭一列」:每列自己的耳號 + 自己的欄位,填完一次送出。
 * 使用者的原話是一隻一隻送太慢,每送一次還要等幾秒。
 */
export function usesPerSowRows(code) {
  return Boolean(formFor(code)?.perSowRows);
}

/** 預設開幾列。使用者說一次通常配 2–3 次,而且整批配完才一起記,
 * 所以開兩列剛好是最常見的情形,要第三次再按一下就好。
 */
export const DEFAULT_SERVICE_ROWS = 2;

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

    // 勾選框自成一格:沒勾**就是**預設答案(沒有),不是「不確定」,
    // 所以不走下面「空白就跳過、必填才報錯」那一套 —— 勾選框本來就
    // 不會是必填,也永遠有值可讀。
    if (field.type === "checkbox") {
      if (value === true) detail[field.key] = true;
      continue;
    }

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
    } else if (field.type === "decimal") {
      // 精液濃度這類量測值本來就有小數(例如 3.5 億/mL),不能像整數
      // 欄位那樣擋掉。
      const n = Number(value);
      if (Number.isNaN(n)) {
        problems.push(`${field.label}請填數字`);
        continue;
      }
      const min = field.min ?? 0;
      const max = field.max ?? 999;
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

  if (d.breed) bits.push(d.breed);
  if (d.boar_tag) bits.push(`公豬 ${d.boar_tag}`);
  if (d.estrus_stability) {
    const symbol = ESTRUS_STABILITY_LABEL[d.estrus_stability];
    if (symbol) bits.push(`發情 ${symbol}`);
  }
  if (d.pen_name) bits.push(`移至 ${d.pen_name}`);
  if (d.born_alive != null) bits.push(`活仔 ${d.born_alive}`);
  if (d.stillborn) bits.push(`死胎 ${d.stillborn}`);
  // 沒填代表沒記,不代表「跟活仔數一樣」,所以不顯示不補值
  if (d.raised != null) bits.push(`飼養 ${d.raised}`);
  // 沒助產是預設情形,不特別標;有助產才值得在摘要裡點出來
  if (d.assisted) bits.push("助產");
  if (d.weaned != null) bits.push(`離乳 ${d.weaned} 隻`);
  // 0 是預設情形,不特別標;有才值得在摘要裡點出來(跟死胎同樣的道理)
  if (d.hernia_count) bits.push(`單睪/賀尼亞 ${d.hernia_count}`);
  // 未評分不顯示,也不補「—」以外的東西
  if (d.wean_score != null) bits.push(`評分 ${d.wean_score} 分`);
  if (d.count != null) bits.push(`${d.count} 隻`);
  if (d.reason) bits.push(d.reason);
  if (d.positive === true) bits.push("有懷孕");
  if (d.positive === false) bits.push("沒懷孕");
  if (d.volume != null) bits.push(`採精量 ${d.volume} ml`);
  if (d.motility != null) bits.push(`活力 ${d.motility}%`);
  if (d.concentration != null) bits.push(`濃度 ${d.concentration} 億/mL`);
  if (d.doses != null) bits.push(`${d.doses} 劑`);
  if (d.weight_kg != null) bits.push(`${d.weight_kg} 公斤`);

  return { name, extra: bits.join(" ・ ") };
}

/** 已記錄清單裡的一列。母豬事件、公豬事件(kind: "boar")、種豬進場
 * (kind: "sow-entry"/"boar-entry",打錯耳號時整筆收回,不是改一筆事件)、
 * 肉豬死亡(kind: "market-death",不掛在任何一頭豬身上,沒有耳號也沒有
 * 動物 id)合併顯示,收回按鈕要分得出該打哪個 API、該重新整理哪一張卡
 * —— 沒有 kind 時一律當母豬事件,舊呼叫端(單純母豬事件)不用跟著改。
 */
export function recordedRow(event) {
  const { name, extra } = recordSummary(event);
  const kind = event.kind || "sow";
  const animalId = (kind === "boar" || kind === "boar-entry") ? event.boarId
    : kind === "market-death" ? null
    : event.sowId;
  // 補登的(日期不在清單的天數範圍內)一定要把日期寫出來。平常 extra 有
  // 內容時就不顯示日期,但補登的列如果只寫「公豬 B9」,使用者會看不出來
  // 這筆到底記成哪一天 —— 而他剛剛就是為了那個日期才補登的。
  const sub = event.backdated
    ? [extra, event.date].filter(Boolean).join(" ・ ")
    : (extra || event.date);
  return `
    <div class="done-row">
      <div class="done-b">
        <div class="done-t">${event.earTag ? `${escapeHtml(event.earTag)} ` : ""}${escapeHtml(name)}</div>
        <div class="done-s">${escapeHtml(sub)}</div>
      </div>
      ${event.canUndo
        ? `<button class="btn-ghost undo" data-undo="${event.id}" data-kind="${kind}"
           ${animalId != null ? `data-animal="${animalId}"` : ""}>收回</button>`
        : ""}
    </div>`;
}
