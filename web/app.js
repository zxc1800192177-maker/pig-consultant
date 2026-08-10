// 畫面接線。純邏輯都在 lib/ 底下,那些有單元測試;這裡只做 DOM 操作。

import { renderMarkdown, trimDangling, escapeHtml } from "./lib/markdown.js";
import {
  formatRecordDate,
  formatShortfall,
  formatValue,
  gradeTone,
  summarizeRecord,
} from "./lib/format.js";
import { SseParser } from "./lib/sse.js";
import { isSpeechRecognitionSupported, mergeTranscript, splitFinalAndInterim } from "./lib/speech.js";
import { addDrug, loadMyDrugs, removeDrug, saveMyDrugs } from "./lib/drugs.js";

const $ = (id) => document.getElementById(id);

// 統一的 API 呼叫。回應不是 JSON(502、逾時、被代理攔截)時不該讓
// 呼叫端整個爆掉 —— 稍早就踩過這個坑:畫面永遠卡在載入中卻沒有錯誤。
async function api(path, options) {
  try {
    const res = await fetch(path, options);
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, data };
  } catch (e) {
    return { ok: false, status: 0, data: { error: `連線失敗:${String(e)}` } };
  }
}

function postJson(body) {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

let metricDefs = [];
let lastWeaknesses = [];   // 供疾病諮詢當背景資訊(US-1 驗收條件 7)

// 對話歷史只存在使用者自己的瀏覽器裡,不上傳保存。
// 若改由伺服器依 IP 保存,同一間辦公室(共用對外 IP)的兩個人會看到彼此的
// 對話內容,是隱私外洩。伺服器端仍會自行裁切則數與長度,不信任這份資料。
const MAX_HISTORY_TURNS = 20;
let history = [];

function rememberTurn(role, content) {
  history.push({ role, content });
  if (history.length > MAX_HISTORY_TURNS) {
    history = history.slice(-MAX_HISTORY_TURNS);
  }
}

// ── 帳號 ──
//
// 兩項核心功能需要先登入(含訪客)才能用,真正的限制在後端(server.py
// 的 _gate),這裡的門檻只是介面。登入後藥品庫改由伺服器保存,可跨裝置;
// 站方沒設定資料庫時(accountsAvailable=false)整個帳號介面不出現 ——
// 不是出現了按下去才報錯,登入要求也會自動失效(見 loginRequired)。
let account = { loggedIn: false, username: null, isGuest: false };
let accountsAvailable = false;
let loginRequired = false;

const LOGGED_OUT = { loggedIn: false, username: null, isGuest: false };

async function refreshAccount() {
  const { ok, data } = await api("/api/auth/me");
  account = ok && data.loggedIn ? data : LOGGED_OUT;
  renderAuthBar();
  applyLoginGate();
  await Promise.all([reloadDrugs(), reloadHistory()]);
}

// 未登入時把功能畫面換成登入引導。
//
// 這只是介面 —— 真正的限制在後端(server.py 的 _gate)。前端隱藏擋不住
// 直接呼叫 API 的人,而疾病諮詢每一次呼叫都在花錢。
function applyLoginGate() {
  const gate = $("loginGate");
  if (!gate) return;

  const blocked = loginRequired && !account.loggedIn;
  const wasHidden = gate.classList.contains("is-hidden");
  gate.classList.toggle("is-hidden", !blocked);
  document.querySelector(".tabs")?.classList.toggle("is-hidden", blocked);

  // 每次重新出現都回到「登入」模式,不要停在使用者上次離開時的模式
  // (例如剛註冊完、登出後回來,下一個直覺動作是登入,不是再註冊一次)
  if (blocked && wasHidden && $("gateSubmit")) {
    gateMode = "login";
    $("gateUsername").value = "";
    $("gatePassword").value = "";
    $("gateError").hidden = true;
    renderGateForm();
  }

  document.querySelectorAll(".panel").forEach((panel) => {
    if (blocked) {
      panel.classList.add("is-hidden");
      return;
    }
    // 解除封鎖時回到目前選取的頁籤,而不是把兩個面板都打開
    const active = document.querySelector(".tab.is-active");
    const wanted = active ? `panel-${active.dataset.tab}` : "panel-consult";
    panel.classList.toggle("is-hidden", panel.id !== wanted);
  });
}

function renderAuthBar() {
  const bar = $("authBar");
  if (!bar) return;

  // 用 .is-hidden 而不是 bar.hidden —— .authbar 這個 class 自己就設了
  // display: flex,跟 [hidden] 內建的 display: none 特異度相同,作者
  // 的規則會贏,結果是設了 hidden 屬性卻沒有真的隱藏,舊內容(例如登出
  // 前的使用者名稱)留在畫面上。.is-hidden 帶 !important,才真的擋得住。
  const hide = () => bar.classList.add("is-hidden");
  const show = () => bar.classList.remove("is-hidden");

  if (!accountsAvailable) {
    hide();
    return;
  }

  if (!account.loggedIn) {
    // 未登入且功能被擋住時,登入引導(#loginGate)已經把帳密輸入框
    // 直接顯示在畫面正中央了 —— 頂部再放一排一樣的連結只是重複,
    // 徒增選擇。只有在「帳號選填」模式(loginRequired=false)才需要
    // 頂部這排連結當作進入帳號功能的唯一入口。
    if (loginRequired) {
      hide();
      return;
    }
    show();
    bar.innerHTML = `
      <button class="btn-ghost" data-auth-open="guest">訪客試用</button>
      <button class="btn-ghost" data-auth-open="register">註冊</button>
      <button class="btn-ghost" data-auth-open="login">登入</button>`;
  } else if (account.isGuest) {
    show();
    bar.innerHTML = `
      <span class="authbar-who">訪客</span>
      <button class="btn-ghost" data-auth-open="claim">設定帳號密碼</button>
      <button class="btn-ghost" data-auth-action="logout">登出</button>`;
  } else {
    show();
    bar.innerHTML = `
      <span class="authbar-who">${escapeHtml(account.username)}</span>
      <button class="btn-ghost" data-auth-action="logout">登出</button>`;
  }
}

// 三種模式共用同一張表單,差別只在文字與端點。
const AUTH_MODES = {
  login: {
    title: "登入", submit: "登入", endpoint: "/api/auth/login",
    hint: "", autocomplete: "current-password",
  },
  register: {
    title: "註冊", submit: "建立帳號", endpoint: "/api/auth/register",
    hint: "密碼至少 8 個英數字元(中文字算兩個,4 個中文字也可以)。"
        + "不要用生日、電話或「password」這類容易被猜到的組合。"
        + "健檢紀錄與藥品庫會存在你的帳號下,換裝置也看得到。",
    autocomplete: "new-password",
  },
  claim: {
    title: "設定帳號密碼", submit: "設定並保留資料", endpoint: "/api/auth/claim",
    hint: "密碼至少 8 個英數字元(中文字算兩個,4 個中文字也可以)。"
        + "目前訪客身分下的健檢紀錄與藥品庫都會完整保留,不會重新開始。",
    autocomplete: "new-password",
  },
};

function openAuthPanel(mode) {
  const spec = AUTH_MODES[mode];
  if (!spec) return;
  $("authPanel").dataset.mode = mode;
  $("authTitle").textContent = spec.title;
  $("authSubmit").textContent = spec.submit;
  $("authHint").textContent = spec.hint;
  $("authPassword").setAttribute("autocomplete", spec.autocomplete);
  $("authError").hidden = true;
  $("authUsername").value = "";
  $("authPassword").value = "";
  $("authPanel").classList.remove("is-hidden");
  $("authUsername").focus();
}

function closeAuthPanel() {
  $("authPanel").classList.add("is-hidden");
}

function showAuthError(message) {
  $("authError").textContent = message;
  $("authError").hidden = false;
}

// 共用的送出邏輯:登入引導(#loginGate)與頂部狀態列的表單(#authPanel)
// 都呼叫這個,不必各自實作一份請求與錯誤處理。
async function performAuth(endpoint, username, password) {
  const { ok, data } = await api(endpoint, postJson({ username, password }));
  return { ok, error: data.error };
}

async function submitAuthForm() {
  const mode = $("authPanel").dataset.mode;
  const spec = AUTH_MODES[mode];
  if (!spec) return;

  $("authSubmit").disabled = true;
  try {
    const { ok, error } = await performAuth(
      spec.endpoint, $("authUsername").value, $("authPassword").value
    );
    if (!ok) return showAuthError(error || "操作失敗,請稍後再試");
    closeAuthPanel();
    await refreshAccount();
  } finally {
    $("authSubmit").disabled = false;
  }
}

// ── 登入引導的表單(直接顯示在初始畫面,不必先點一下才展開) ──
let gateMode = "login";

function renderGateForm() {
  const spec = AUTH_MODES[gateMode];
  $("gateSubmit").textContent = spec.submit;
  $("gatePassword").setAttribute("autocomplete", spec.autocomplete);
  $("gateToggleLabel").textContent = gateMode === "login" ? "還沒有帳號?" : "已經有帳號了?";
  $("gateModeToggle").textContent = gateMode === "login" ? "註冊一個" : "改成登入";
  $("gateHint").textContent = spec.hint;
  $("gateHint").hidden = !spec.hint;
}

function toggleGateMode() {
  gateMode = gateMode === "login" ? "register" : "login";
  $("gateError").hidden = true;
  renderGateForm();
}

async function submitGateForm() {
  const spec = AUTH_MODES[gateMode];
  $("gateSubmit").disabled = true;
  $("gateError").hidden = true;
  try {
    const { ok, error } = await performAuth(
      spec.endpoint, $("gateUsername").value, $("gatePassword").value
    );
    if (!ok) {
      $("gateError").textContent = error || "操作失敗,請稍後再試";
      $("gateError").hidden = false;
      return;
    }
    $("gateUsername").value = "";
    $("gatePassword").value = "";
    await refreshAccount();
  } finally {
    $("gateSubmit").disabled = false;
  }
}

async function startGuestSession() {
  const { ok, data } = await api("/api/auth/guest", postJson({}));
  if (!ok) {
    showBanner(data.error || "無法建立訪客身分,請稍後再試", "warn");
    return;
  }
  await refreshAccount();
}

async function logout() {
  await api("/api/auth/logout", postJson({}));
  await refreshAccount();
}

// 帳號相關的按鈕散落在三個地方(頁首狀態列、登入引導、訪客提醒),
// 而且都會被重繪。用單一的事件委派接住全部,不必每次重繪重新綁定。
document.addEventListener("click", (e) => {
  const opener = e.target.closest("[data-auth-open]");
  if (opener) {
    const mode = opener.dataset.authOpen;
    // 訪客試用不需要填表單,點下去就進入
    return mode === "guest" ? startGuestSession() : openAuthPanel(mode);
  }
  if (e.target.closest('[data-auth-action="logout"]')) logout();
});

$("authSubmit")?.addEventListener("click", submitAuthForm);
$("authClose")?.addEventListener("click", closeAuthPanel);
$("authPassword")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") submitAuthForm();
});

if ($("gateSubmit")) {
  renderGateForm();
  $("gateSubmit").addEventListener("click", submitGateForm);
  $("gateModeToggle").addEventListener("click", toggleGateMode);
  // 帳號欄按 Enter 直接跳到密碼欄,密碼欄按 Enter 直接送出 ——
  // 不用這兩個欄位都逼使用者伸手點按鈕。
  $("gateUsername").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); $("gatePassword").focus(); }
  });
  $("gatePassword").addEventListener("keydown", (e) => {
    if (e.key === "Enter") submitGateForm();
  });
}

// 訪客提醒。這段話必須主動講:資料確實存在伺服器,但只有這台瀏覽器的
// cookie 能取回 —— 使用者不會自己想到「清瀏覽器資料 = 永久失去」。
function guestWarningHtml() {
  if (!account.loggedIn || !account.isGuest) return "";
  return `
    <div class="guest-warning">
      <span>目前是訪客身分,資料只能靠這台裝置的瀏覽器取回。
        清除瀏覽器資料或換裝置就會失去存取權,沒有辦法救回。</span>
      <button class="btn-ghost" data-auth-open="claim">設定帳號密碼</button>
    </div>`;
}

// ── 我的藥品庫 ──
// 未登入:存在這台瀏覽器的 localStorage(行為與加入帳號功能前完全相同)。
// 已登入:存在伺服器,可跨裝置;疾病諮詢時由伺服器直接讀取,
//         不再信任請求裡的內容(見 server.py 的 _drugs_for_consult)。
// 顧問會優先引用這裡的劑量,不會自己生成數字(見 core/dosage.py)。
let myDrugs = [];

async function reloadDrugs() {
  if (account.loggedIn) {
    const { ok, data } = await api("/api/my-drugs");
    myDrugs = ok ? data.drugs : [];
  } else {
    myDrugs = loadMyDrugs(localStorage);
  }
  renderDrugList();
  updateDrugStorageHint();
}

function updateDrugStorageHint() {
  const hint = $("drugStorageHint");
  if (!hint) return;
  hint.textContent = account.loggedIn
    ? "列出你手邊有的藥,顧問建議劑量時會優先引用這裡的資料,不會憑空生成數字。已存在你的帳號下,換裝置也看得到。"
    : "列出你手邊有的藥,顧問建議劑量時會優先引用這裡的資料,不會憑空生成數字。只存在這台裝置的瀏覽器裡,不會上傳。";
}

function renderDrugList() {
  const list = $("drugList");
  if (!list) return;

  const warning = guestWarningHtml();
  if (!myDrugs.length) {
    list.innerHTML = `${warning}<li class="drug-empty">還沒有加入任何藥品。</li>`;
    return;
  }
  list.innerHTML = warning + myDrugs
    .map((d) => {
      const metaParts = [];
      if (d.dosageNote) metaParts.push(escapeHtml(d.dosageNote));
      if (d.withdrawalDays != null) metaParts.push(`休藥期 ${d.withdrawalDays} 天`);
      const meta = metaParts.length
        ? `<div class="drug-meta">${metaParts.join("・")}</div>`
        : "";
      return `
        <li class="drug-item">
          <div>
            <div class="drug-name">${escapeHtml(d.name)}</div>
            ${meta}
          </div>
          <button type="button" class="drug-remove" data-id="${escapeHtml(d.id)}">移除</button>
        </li>`;
    })
    .join("");
}

function readOptionalNumber(el) {
  const raw = el.value.trim();
  if (!raw) return undefined;
  const n = Number(raw);
  return Number.isFinite(n) ? n : undefined;
}

async function submitNewDrug() {
  const draft = {
    name: $("drugName").value,
    dosageNote: $("drugNote").value,
    withdrawalDays: readOptionalNumber($("drugWithdrawal")),
  };
  if (!draft.name.trim()) return $("drugName").focus();

  if (account.loggedIn) {
    const { ok, data } = await api("/api/my-drugs", postJson(draft));
    if (!ok) return showBanner(data.error || "新增失敗", "warn");
    await reloadDrugs();
  } else {
    myDrugs = addDrug(myDrugs, draft);
    saveMyDrugs(localStorage, myDrugs);
    renderDrugList();
  }

  $("drugName").value = "";
  $("drugNote").value = "";
  $("drugWithdrawal").value = "";
  $("drugName").focus();
}

async function deleteDrug(id) {
  if (account.loggedIn) {
    const { ok, data } = await api(`/api/my-drugs/${encodeURIComponent(id)}`,
                                   { method: "DELETE" });
    if (!ok) return showBanner(data.error || "移除失敗", "warn");
    await reloadDrugs();
  } else {
    myDrugs = removeDrug(myDrugs, id);
    saveMyDrugs(localStorage, myDrugs);
    renderDrugList();
  }
}

// 只在這個區塊真正需要的元素都存在時才接線 —— 瀏覽器快取(尤其是 PWA
// 的 service worker)偶爾會讓 index.html 跟 app.js 版本對不上,若這裡
// 對著不存在的元素呼叫 addEventListener 而不做防呆,拋出的例外會讓
// 這個檔案後面所有按鈕(載入範例資料、詢問顧問、健檢)全部沒有反應 ——
// 一個次要功能的元素缺失,不該連累完全無關的核心功能。
const drugListEl = $("drugList");
const addDrugBtnEl = $("addDrugBtn");
if (drugListEl && addDrugBtnEl) {
  addDrugBtnEl.addEventListener("click", submitNewDrug);
  drugListEl.addEventListener("click", (e) => {
    const btn = e.target.closest(".drug-remove");
    if (btn) deleteDrug(btn.dataset.id);
  });
}

// ── 健檢歷史紀錄 ──
async function reloadHistory() {
  const card = $("historyCard");
  if (!card) return;
  if (!account.loggedIn) {
    card.classList.add("is-hidden");
    return;
  }
  card.classList.remove("is-hidden");
  const { ok, data } = await api("/api/health-checks");
  renderHistory(ok ? data.records : []);
}

function renderHistory(records) {
  const list = $("historyList");
  if (!list) return;

  const warning = guestWarningHtml();
  if (!records.length) {
    list.innerHTML =
      `${warning}<li class="history-empty">還沒有存過健檢紀錄。做完健檢後按「存入歷史紀錄」。</li>`;
    return;
  }
  list.innerHTML = warning + records
    .map((r) => `
      <li class="history-item">
        <div>
          <div class="history-date">${escapeHtml(formatRecordDate(r.createdAt))}</div>
          <div class="history-summary">${escapeHtml(summarizeRecord(r))}</div>
        </div>
        <div class="history-actions">
          <button type="button" class="btn-ghost" data-history-load="${escapeHtml(r.id)}">
            載入
          </button>
          <button type="button" class="drug-remove" data-history-delete="${escapeHtml(r.id)}">
            刪除
          </button>
        </div>
      </li>`)
    .join("");
}

// 最近一次健檢送出的數字。存入歷史時用這份,而不是重新讀表單 ——
// 使用者可能在看完結果後又改了輸入框,那樣存進去的會跟畫面上的結果對不起來。
let lastGradedValues = null;

async function saveCurrentHealthCheck() {
  if (!lastGradedValues) return;
  const { ok, data } = await api("/api/health-checks", postJson({ values: lastGradedValues }));
  if (!ok) return showBanner(data.error || "存檔失敗", "warn");
  showBanner("已存入歷史紀錄", "info");
  await reloadHistory();
}

function loadHistoryRecord(records, id) {
  const record = records.find((r) => String(r.id) === String(id));
  if (!record) return;
  Object.entries(record.values).forEach(([key, value]) => {
    const input = $(`m-${key}`);
    if (input) input.value = value;
  });
  showBanner("已載入該次紀錄的數字,可直接按「開始健檢」重新查看", "info");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

const historyListEl = $("historyList");
if (historyListEl) {
  historyListEl.addEventListener("click", async (e) => {
    const loadBtn = e.target.closest("[data-history-load]");
    const deleteBtn = e.target.closest("[data-history-delete]");

    if (loadBtn) {
      const { ok, data } = await api("/api/health-checks");
      if (ok) loadHistoryRecord(data.records, loadBtn.dataset.historyLoad);
      return;
    }
    if (deleteBtn) {
      const id = deleteBtn.dataset.historyDelete;
      const { ok, data } = await api(`/api/health-checks/${encodeURIComponent(id)}`,
                                     { method: "DELETE" });
      if (!ok) return showBanner(data.error || "刪除失敗", "warn");
      await reloadHistory();
    }
  });
}

// ── 頁籤 ──
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => {
      const on = t === tab;
      t.classList.toggle("is-active", on);
      t.setAttribute("aria-selected", String(on));
    });
    document.querySelectorAll(".panel").forEach((p) => {
      p.classList.toggle("is-hidden", p.id !== `panel-${tab.dataset.tab}`);
    });
  });
});

// ── 啟動 ──
async function init() {
  // 三個請求一起發:登入狀態要盡早知道,否則畫面會先顯示完整功能
  // 再被登入引導蓋掉(或反過來),使用者會看到明顯的閃動。
  const [health, meta, me] = await Promise.all([
    api("/api/health"),
    api("/api/metrics"),
    api("/api/auth/me"),
  ]);

  if (!health.ok) {
    showBanner("無法連線到伺服器,請稍後再試。", "warn");
  } else {
    $("sourceLabel").textContent = health.data.source;
    accountsAvailable = Boolean(health.data.accountsAvailable);
    loginRequired = Boolean(health.data.loginRequired);
    if (!health.data.aiAvailable) {
      // 提示文字由後端提供(core/labels.py),前端不自己維護一份措辭
      showBanner(health.data.aiUnavailableNote, "warn");
      $("askBtn").disabled = true;
    }
  }

  if (meta.ok) {
    metricDefs = meta.data.metrics;
    $("disclaimer").textContent = meta.data.disclaimer;
    renderMetricFields();   // 欄位要先畫出來,「載入某次紀錄」才有地方可填
  }

  if (accountsAvailable) {
    account = me.ok && me.data.loggedIn ? me.data : LOGGED_OUT;
    renderAuthBar();
    applyLoginGate();
    await Promise.all([reloadDrugs(), reloadHistory()]);
  }
}

function showBanner(text, tone) {
  $("banner").innerHTML = `<div class="notice notice-${tone}">${escapeHtml(text)}</div>`;
}

// ── 生產健檢 ──
function renderMetricFields() {
  $("metricFields").innerHTML = metricDefs
    .map(
      (m) => `
      <div class="field">
        <label for="m-${m.key}">${escapeHtml(m.name)}
          ${m.unit ? `<span class="unit">(${escapeHtml(m.unit)})</span>` : ""}
        </label>
        <input type="number" step="any" id="m-${m.key}" data-key="${m.key}"
               title="${escapeHtml(m.definition)}">
      </div>`
    )
    .join("");
}

function collectValues() {
  const values = {};
  document.querySelectorAll("#metricFields input").forEach((input) => {
    if (input.value.trim() !== "") values[input.dataset.key] = input.value.trim();
  });
  return values;
}

$("loadExample").addEventListener("click", async () => {
  const example = await (await fetch("/api/example")).json();
  Object.entries(example.values).forEach(([key, value]) => {
    const input = $(`m-${key}`);
    if (input) input.value = value;
  });
  showBanner(`已載入${example.label}`, "info");
});

$("gradeBtn").addEventListener("click", async () => {
  const values = collectValues();
  const { ok, data } = await api("/api/grade", postJson({ values }));

  if (!ok) {
    const messages = (data.errors || []).map((e) => escapeHtml(e.message)).join("<br>");
    $("healthResult").innerHTML =
      `<div class="card"><div class="notice notice-warn">${
        messages || escapeHtml(data.error || "健檢失敗,請稍後再試")
      }</div></div>`;
    return;
  }

  lastWeaknesses = data.weaknesses;
  lastGradedValues = values;
  renderHealthResult(data);
  requestAdvice(data.weaknesses);
});

// 存檔按鈕每次健檢都會重新產生,用事件委派接才不必重複綁定
$("healthResult")?.addEventListener("click", (e) => {
  if (e.target.closest("#saveHealthCheck")) saveCurrentHealthCheck();
});

function renderHealthResult(data) {
  const graded = Object.entries(data.grades);
  if (!graded.length) {
    $("healthResult").innerHTML =
      `<div class="card"><p class="hint">請至少填入一項指標。</p></div>`;
    return;
  }

  const warnings = data.warnings.length
    ? `<div class="notice notice-warn">${data.warnings
        .map((w) => escapeHtml(w.message))
        .join("<br>")}</div>`
    : "";

  const rows = graded
    .map(
      ([key, g]) => `
      <tr${g.isWeak ? ' class="row-weak"' : ""}>
        <td>${escapeHtml(g.name)}${g.isWeak ? '<span class="weak-mark" title="列入改善清單">●</span>' : ""}</td>
        <td><span class="grade-pill tone-${gradeTone(g.grade)}">${g.grade}</span></td>
        <td>${escapeHtml(formatValue(g.value, g.unit))}</td>
        <td>${escapeHtml(formatValue(g.mean, g.unit))}</td>
        <td>${g.percentileBand[0]}~${g.percentileBand[1]}%</td>
        <td class="hint">${escapeHtml(g.sampleNote)}</td>
      </tr>`
    )
    .join("");

  const ranking = data.weaknesses.length
    ? data.weaknesses
        .map(
          (w, i) => `
          <li class="rank-item">
            <span class="rank-num">${i + 1}</span>
            <div>
              <div class="rank-name">${escapeHtml(w.name)}</div>
              <div class="rank-detail">${escapeHtml(formatShortfall(w.shortfallSd))}</div>
              ${
                w.downstreamNames.length
                  ? `<div class="rank-chain">改善後會帶動:${w.downstreamNames
                      .map(escapeHtml)
                      .join("、")}</div>`
                  : ""
              }
            </div>
            <span class="grade-pill tone-${gradeTone(w.grade)}">${w.grade}</span>
          </li>`
        )
        .join("")
    : `<li class="hint">沒有低於全國中位數的項目,表現良好。</li>`;

  // 存檔按鈕只在登入時出現 —— 未登入時按了也沒地方存,不如不要顯示
  const saveButton = account.loggedIn
    ? `<button class="btn-ghost" id="saveHealthCheck">存入歷史紀錄</button>`
    : "";

  $("healthResult").innerHTML = `
    ${warnings}
    <div class="card">
      <div class="card-head">
        <div class="section-label tag-computed">計算結果 · ${escapeHtml(data.source)}</div>
        ${saveButton}
      </div>
      <div class="table-scroll">
        <table>
          <thead><tr>
            <th>指標</th><th>級距</th><th>本場</th><th>全國平均</th><th>百分位</th><th>備註</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <div class="section-label tag-computed">改善優先順序 · ${escapeHtml(data.shortfallNote)}</div>
      <ul class="rank-list">${ranking}</ul>
      <p class="hint">${escapeHtml(data.upstreamNote)}</p>
    </div>

    <div class="card" id="adviceCard">
      <div class="section-label tag-ai">AI 改善建議</div>
      <div class="notice notice-caution">${escapeHtml(data.medicalDisclaimer)}</div>
      <div id="adviceBody" class="md"></div>
    </div>`;
}

async function requestAdvice(weaknesses) {
  if (!weaknesses.length) {
    $("adviceCard").remove();
    return;
  }
  const body = $("adviceBody");
  body.innerHTML = `<div class="loading"><span class="spinner"></span>顧問分析中…</div>`;

  // 曾經這裡沒有 try/catch:伺服器回傳非預期內容(如 502、逾時)時
  // res.json() 會拋例外,畫面永遠卡在「顧問分析中…」,使用者看不到任何錯誤。
  try {
    const res = await fetch("/api/advise", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ weaknesses }),
    });
    const data = await res.json().catch(() => ({}));

    body.innerHTML = res.ok
      ? renderMarkdown(data.advice || "")
      : `<div class="notice notice-warn">${escapeHtml(
          data.error || `伺服器錯誤(HTTP ${res.status}),請稍後再試`
        )}</div>`;
  } catch (e) {
    body.innerHTML = `<div class="notice notice-warn">連線失敗:${escapeHtml(String(e))}</div>`;
  }
}

// ── 疾病諮詢 ──
function setConsultBusy(busy) {
  $("askBtn").disabled = busy;
  document.querySelectorAll("#examples .chip").forEach((c) => (c.disabled = busy));
}

$("examples").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  $("question").value = chip.dataset.q;
  ask(chip.dataset.q);
});

$("askBtn").addEventListener("click", () => {
  const q = $("question").value.trim();
  if (!q) return $("question").focus();
  ask(q);
});

$("question").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("askBtn").click();
  }
});

// ── 語音輸入 ──
// 桌面版 Firefox 完全不支援,其餘瀏覽器需要廠商前綴 —— 不支援就不顯示
// 按鈕,漸進增強,不影響手動打字的既有流程。
// 同時也要檢查 DOM 元素本身存在(而不是只檢查瀏覽器 API 支援)——
// 快取造成 index.html 跟 app.js 版本對不上時,元素可能不存在,若沒防呆,
// 拋出的例外會讓這個檔案後面所有東西(包含最下面的 init() 與 service
// worker 註冊)全部沒有執行。
const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;

if (isSpeechRecognitionSupported(window) && $("micBtn") && $("micHint")) {
  const recognition = new SpeechRecognitionCtor();
  recognition.lang = "zh-TW";
  recognition.continuous = true;
  recognition.interimResults = true;

  // 錄音開始當下的文字要記住 —— continuous 模式的 event.results 是
  // 整段錄音從頭到現在的累積結果,每次都要接在「錄音前」的文字後面,
  // 不能接在「上一次事件」的文字後面,否則手打的內容會被蓋掉或重複。
  let baseTextAtStart = "";

  const setListening = (on) => $("micBtn").setAttribute("aria-pressed", String(on));

  const showMicHint = (text) => {
    $("micHint").textContent = text;
    $("micHint").hidden = false;
  };

  recognition.addEventListener("result", (event) => {
    const { finalText, interimText } = splitFinalAndInterim(event.results);
    $("question").value = mergeTranscript(baseTextAtStart, finalText + interimText);
  });

  recognition.addEventListener("end", () => setListening(false));

  recognition.addEventListener("error", (event) => {
    // no-speech、aborted 這類使用者無法採取行動的狀況靜默重置就好;
    // 權限或裝置問題才值得中斷並說明,否則使用者只會看到按鈕跳掉卻不知道為什麼。
    if (event.error === "not-allowed" || event.error === "service-not-allowed") {
      showMicHint("沒有取得麥克風權限,請到瀏覽器設定允許存取麥克風後再試一次。");
    } else if (event.error === "audio-capture") {
      showMicHint("找不到可用的麥克風裝置。");
    }
  });

  $("micBtn").hidden = false;
  $("micBtn").addEventListener("click", () => {
    if ($("micBtn").getAttribute("aria-pressed") === "true") {
      recognition.stop();
      return;
    }
    $("micHint").hidden = true;
    baseTextAtStart = $("question").value;
    try {
      recognition.start();
      setListening(true);
    } catch {
      // 例如上一段錄音還沒完全結束就被再次觸發,忽略即可,使用者再點一次就好
    }
  });
}

async function ask(question) {
  setConsultBusy(true);
  $("consultResult").innerHTML = `
    <div class="card">
      <div class="section-label tag-ai">AI 回覆</div>
      <div class="loading" id="consultLoading"><span class="spinner"></span>顧問思考中…</div>
      <div class="md" id="consultBody"></div>
    </div>`;

  let answer = "";
  try {
    const res = await fetch("/api/consult", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        weaknesses: lastWeaknesses,
        history,   // 送出前的歷史,不含這一題本身
        // 已登入時伺服器會自己去資料庫拿,送了也會被忽略(見 server.py 的
        // _drugs_for_consult)—— 不送是為了讓「誰是可信來源」在這裡就看得出來
        myDrugs: account.loggedIn ? undefined : myDrugs,
      }),
    });

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    const parser = new SseParser();

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      for (const event of parser.push(decoder.decode(value, { stream: true }))) {
        handleConsultEvent(event, () => answer, (text) => (answer = text));
      }
    }
  } catch (e) {
    $("consultBody").innerHTML =
      `<div class="notice notice-warn">連線失敗:${escapeHtml(String(e))}</div>`;
  } finally {
    $("consultLoading")?.remove();
    if (answer) {
      $("consultBody").innerHTML = renderMarkdown(answer);
      // 成功拿到回答才記錄。失敗的請求不進歷史,否則之後的追問會
      // 帶著一段沒有答案的殘缺對話,反而干擾 AI 判斷。
      rememberTurn("user", question);
      rememberTurn("assistant", answer);
    }
    setConsultBusy(false);
  }
}

function renderDosageReference(entries) {
  const body = entries
    .map((entry) => {
      const drugs = entry.drugs
        .map((d) => {
          const withdrawal = d.withdrawalDays != null ? `・休藥期 ${d.withdrawalDays} 天` : "";
          return `
            <div class="dosage-drug">
              <span class="dosage-drug-name">${escapeHtml(d.name)}</span>
              ${escapeHtml(d.dosage)}${withdrawal}
            </div>`;
        })
        .join("");
      const source = entry.sourceNote
        ? `<div class="dosage-source">資料來源:${escapeHtml(entry.sourceNote)}</div>`
        : "";
      return `
        <div class="dosage-entry">
          <div class="dosage-disease">${escapeHtml(entry.diseaseName)}</div>
          ${drugs}${source}
        </div>`;
    })
    .join("");
  return `
    <div class="notice notice-verified">
      <div class="section-label tag-computed">官方劑量對照(系統查表,非 AI 生成)</div>
      ${body}
    </div>`;
}

function handleConsultEvent(event, getAnswer, setAnswer) {
  if (event.type === "meta") {
    // 通報提示是計算出來的,在 AI 開口之前就先呈現(憲法第一條)
    const parts = [];
    if (event.escalation) {
      parts.push(`<div class="notice notice-alert">${escapeHtml(event.escalation.notice)}</div>`);
    }
    parts.push(`<div class="notice notice-info">${escapeHtml(event.baselineNotice)}</div>`);
    // 劑量查表化:這張卡片是伺服器直接算出來的,不是 AI 寫的 ——
    // 跟下面的 AI 文字分開呈現,使用者才看得出哪些數字是查表來的。
    if (event.dosageReference && event.dosageReference.length) {
      parts.push(renderDosageReference(event.dosageReference));
    }
    // 醫療免責緊貼在回答上方,使用者不用捲到頁尾才看得到
    if (event.medicalDisclaimer) {
      parts.push(
        `<div class="notice notice-caution">${escapeHtml(event.medicalDisclaimer)}</div>`
      );
    }
    $("consultResult").insertAdjacentHTML("afterbegin", parts.join(""));
    return;
  }

  if (event.type === "delta") {
    $("consultLoading")?.remove();
    const text = getAnswer() + event.text;
    setAnswer(text);
    $("consultBody").innerHTML =
      renderMarkdown(trimDangling(text)) + '<span class="cursor"></span>';
    return;
  }

  if (event.type === "error") {
    $("consultLoading")?.remove();
    $("consultBody").innerHTML =
      `<div class="notice notice-warn">${escapeHtml(event.error)}</div>`;
  }
}

init();

// 註冊失敗(舊瀏覽器、非 HTTPS)不影響網站運作,純粹是漸進增強
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  });
}
