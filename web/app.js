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
import { addFactor, removeFactor } from "./lib/factors.js";
import {
  alertRow, boarPerformanceGrid, boarRow, buildAlerts, customTaskRow, customTaskSetting,
  eventName, eventRow, formatMonth, formatWeek, monthPickerGrid, monthReportGrid,
  performanceGrid, pendingCheckRow, reviewRow, settingRow, shiftDate, shiftMonth, sowRow,
  weanScoreCard,
  statusPills, taskGroup, timelineCaption, TIMELINE_LIMIT, visibleEvents, yearOfMonth,
} from "./lib/v2.js";
import {
  DEFAULT_SERVICE_ROWS, SIDE_EFFECTS, buildDetail, createsNewAnimal, formFor,
  hasOtherOption, OTHER_REASON, recordedRow, supportsMultiService, supportsMultiSow,
  targetsBoar, targetsEither, targetsNothing, usesPerSowRows,
} from "./lib/record.js";

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
let lastWeaknesses = [];

// ── 帳號 ──
//
// v2 的資料屬於牧場而非個人,所以一定要先有身分才能用。真正的限制在後端
// (server.py 的 _need_farm),這裡的門檻只是介面 —— 前端隱藏擋不住直接
// 呼叫 API 的人。站方沒設定資料庫時整個帳號介面不出現。
// account.role 決定看得到哪些頁籤(owner 全部,worker 只能記錄)。
let account = { loggedIn: false, username: null, isGuest: false,
                role: "owner", isOwner: true };
let accountsAvailable = false;
let loginRequired = false;

const LOGGED_OUT = { loggedIn: false, username: null, isGuest: false,
                     role: "owner", isOwner: true };

async function refreshAccount() {
  const { ok, data } = await api("/api/auth/me");
  account = ok && data.loggedIn ? data : LOGGED_OUT;
  renderAuthBar();
  applyLoginGate();
  // 沒登入就沒有帳號可刪/可產生救援碼,整張卡收起來而不是留一個
  // 按了會失敗的按鈕。
  $("deleteAccountCard")?.classList.toggle("is-hidden", !account.loggedIn);
  $("recoverySetCard")?.classList.toggle("is-hidden", !account.loggedIn);
  // 登入/登出後全部重讀。少列一項的話,換帳號後那一區會留著上一個
  // 使用者的資料 —— 跨牧場的資料外洩就是這樣發生的。
  await Promise.all([
    reloadHistory(), reloadTasks(), reloadAlerts(), reloadSows(),
    reloadBoars(), reloadRecent(), reloadReview(), reloadDataProblems(), reloadMonthReport(), reloadSettings(),
    reloadCustomTaskSettings(),
  ]);
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
  document.querySelector(".nav")?.classList.toggle("is-hidden", blocked);

  // 每次重新出現都回到「登入」模式,不要停在使用者上次離開時的模式
  // (例如剛註冊完、登出後回來,下一個直覺動作是登入,不是再註冊一次)
  if (blocked && wasHidden && $("gateSubmit")) {
    gateMode = "login";
    $("gateUsername").value = "";
    $("gatePassword").value = "";
    hidePasswordField("gatePassword");
    $("gateError").hidden = true;
    renderGateForm();
  }

  document.querySelectorAll(".panel").forEach((panel) => {
    if (blocked) {
      panel.classList.add("is-hidden");
      return;
    }
    // 解除封鎖時回到目前選取的頁籤,而不是把所有面板都打開
    panel.classList.toggle("is-hidden", panel.id !== `panel-${currentTab}`);
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
        + "母豬資料與健檢紀錄會存在你的牧場下,換裝置也看得到。",
    autocomplete: "new-password",
  },
  claim: {
    title: "設定帳號密碼", submit: "設定並保留資料", endpoint: "/api/auth/claim",
    hint: "密碼至少 8 個英數字元(中文字算兩個,4 個中文字也可以)。"
        + "目前訪客身分下的資料都會完整保留,不會重新開始。",
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
  hidePasswordField("authPassword");
  $("authPanel").classList.remove("is-hidden");
  $("authUsername").focus();
}

function closeAuthPanel() {
  $("authPanel").classList.add("is-hidden");
}

// 表單清空時一併把密碼收回圓點狀態 —— 否則上一個人按過「顯示」之後,
// 下一次打開表單會直接以明文顯示正在輸入的密碼。
function hidePasswordField(id) {
  const field = $(id);
  if (field) field.type = "password";
  const toggle = document.querySelector(`[data-pw-toggle="${id}"]`);
  if (toggle) toggle.textContent = "顯示";
}

function showAuthError(message) {
  $("authError").textContent = message;
  $("authError").hidden = false;
}

// 共用的送出邏輯:登入引導(#loginGate)與頂部狀態列的表單(#authPanel)
// 都呼叫這個,不必各自實作一份請求與錯誤處理。
async function performAuth(endpoint, username, password) {
  const { ok, data } = await api(endpoint, postJson({ username, password }));
  // 註冊會帶回救援碼,而且**只有這一次**拿得到 —— 呼叫端要負責顯示。
  return { ok, error: data.error, recoveryCode: data.recoveryCode };
}

async function submitAuthForm() {
  const mode = $("authPanel").dataset.mode;
  const spec = AUTH_MODES[mode];
  if (!spec) return;

  $("authSubmit").disabled = true;
  try {
    const { ok, error, recoveryCode } = await performAuth(
      spec.endpoint, $("authUsername").value, $("authPassword").value
    );
    if (!ok) return showAuthError(error || "操作失敗,請稍後再試");
    closeAuthPanel();
    await refreshAccount();
    showRecoveryCode(recoveryCode);
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
    const { ok, error, recoveryCode } = await performAuth(
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
    showRecoveryCode(recoveryCode);
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
  // **整頁重新載入,不是只重讀資料。**
  //
  // 畫面上每一塊資料都存在模組層級的變數與已經畫好的 DOM 裡,登出時要
  // 全部清掉才算乾淨 —— 而那件事原本靠「refreshAccount() 裡那一整串
  // reload* 全部都成功」才成立。只要其中一個提早 return 或請求失敗
  // (被限流的 429、換帳號當下的 409、網路不穩),那一區就會留著上一個
  // 帳號的內容,下一個人登入後仍然看得到(實際回報過:換帳號後看到的
  // 還是前一個帳號的母豬,重新整理才正常)。
  //
  // 重新載入是唯一能保證「不留下任何東西」的做法,成本只是一次本來就
  // 很小的靜態檔載入(而且 service worker 對程式碼是網路優先,不會
  // 拿到舊版)。換帳號本來就不是高頻動作。
  location.reload();
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
  if (e.target.closest("#deleteAccountBtn")) deleteAccount();
  if (e.target.closest("#recoveryCopy")) copyRecoveryCode();
  if (e.target.closest("#recoveryDone")) $("recoveryPanel").classList.add("is-hidden");
  if (e.target.closest("#recoveryGenBtn")) generateRecoveryCode();
  if (e.target.closest("#gateForgot")) openForgotPanel();
  if (e.target.closest("#forgotClose")) $("forgotPanel").classList.add("is-hidden");
  if (e.target.closest("#forgotSubmit")) submitForgotForm();

  // 密碼的「顯示/隱藏」。看不到自己打了什麼,是「註冊完就再也登不進去」
  // 最常見的成因 —— 尤其密碼含中文時,選錯字在圓點底下完全看不出來。
  const toggle = e.target.closest("[data-pw-toggle]");
  if (toggle) {
    const field = $(toggle.dataset.pwToggle);
    if (!field) return;
    const shown = field.type === "text";
    field.type = shown ? "password" : "text";
    toggle.textContent = shown ? "顯示" : "隱藏";
    field.focus();
  }
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

// ── 救援碼 ──
//
// 明碼只有產生的那一刻存在(資料庫只存雜湊),所以拿到之後一定要當場
// 顯示,而且要讓使用者主動確認抄好了才關掉。
function showRecoveryCode(code) {
  if (!code || !$("recoveryPanel")) return;
  $("recoveryCodeText").textContent = code;
  $("recoveryPanel").classList.remove("is-hidden");
  window.scrollTo(0, 0);
}

async function copyRecoveryCode() {
  const code = $("recoveryCodeText")?.textContent || "";
  try {
    await navigator.clipboard.writeText(code);
    showBanner("救援碼已複製", "ok");
  } catch {
    // 剪貼簿在非 HTTPS 或使用者拒絕授權時會失敗。這不該讓人卡住 ——
    // 碼本來就看得到,請他自己抄就好。
    showBanner("複製不成功,請手動抄下畫面上的碼", "warn");
  }
}

// 產生新的救援碼(給救援碼弄丟、或這個功能出現前就註冊的帳號)。
async function generateRecoveryCode() {
  const field = $("recoveryGenPassword");
  const error = $("recoveryGenError");
  if (!field) return;
  error.hidden = true;

  if (!account.isGuest && !field.value) {
    error.textContent = "請輸入密碼以確認身分";
    error.hidden = false;
    return;
  }

  const { ok, data } = await api("/api/auth/recovery-code",
                                 postJson({ password: field.value }));
  if (!ok) {
    error.textContent = data.error || "產生失敗,請稍後再試";
    error.hidden = false;
    return;
  }
  field.value = "";
  hidePasswordField("recoveryGenPassword");
  showRecoveryCode(data.recoveryCode);
}

// ── 忘記密碼 ──
function openForgotPanel() {
  $("forgotUsername").value = $("gateUsername")?.value || "";
  $("forgotCode").value = "";
  $("forgotPassword").value = "";
  hidePasswordField("forgotPassword");
  $("forgotError").hidden = true;
  $("forgotPanel").classList.remove("is-hidden");
  $("forgotCode").focus();
}

async function submitForgotForm() {
  const error = $("forgotError");
  error.hidden = true;

  const { ok, data } = await api("/api/auth/recover", postJson({
    username: $("forgotUsername").value,
    code: $("forgotCode").value,
    password: $("forgotPassword").value,
  }));
  if (!ok) {
    error.textContent = data.error || "重設失敗,請稍後再試";
    error.hidden = false;
    return;
  }

  // 重設後刻意不自動登入(OWASP),所以這裡把新密碼填回登入表單、
  // 請他實際登入一次 —— 確認新密碼真的記住了,而不是下次要用才發現。
  $("forgotPanel").classList.add("is-hidden");
  showBanner("密碼已重設,請用新密碼登入", "ok");
  showRecoveryCode(data.recoveryCode);
}

// ── 刪除帳號 ──
//
// 整張卡只在登入後出現(未登入沒有帳號可刪),而且送出前一定要再問一次:
// 這是全站唯一不可復原的動作,而按鈕就在設定頁裡,誤觸的代價是整座牧場。
async function deleteAccount() {
  const field = $("deleteAccountPassword");
  const error = $("deleteAccountError");
  const btn = $("deleteAccountBtn");
  if (!field || !btn) return;

  const showError = (message) => {
    error.textContent = message;
    error.hidden = false;
  };
  error.hidden = true;

  if (!account.isGuest && !field.value) {
    return showError("請輸入密碼以確認身分");
  }
  // 訪客沒有密碼可驗,所以更需要這一問 —— 對他們來說按下去就沒了。
  if (!window.confirm(
    "確定要永久刪除這個帳號嗎?\n\n"
    + "整座牧場的母豬、公豬、所有生產事件與健檢紀錄都會一起消失,"
    + "沒有備份可以救回。\n\n這個動作無法復原。"
  )) return;

  btn.disabled = true;
  try {
    const { ok, data } = await api("/api/auth/delete",
                                   postJson({ password: field.value }));
    if (!ok) return showError(data.error || "刪除失敗,請稍後再試");
    // 帳號已經不存在,整頁重載回到登入畫面 —— 理由跟登出那邊一樣,
    // 畫面上不能留下任何一塊已經被刪掉的資料。
    location.reload();
  } finally {
    btn.disabled = false;
  }
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
          <button type="button" class="btn-soft btn-sm" data-history-load="${escapeHtml(r.id)}">
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

// ── 底部導覽 ──
//
// 六個頁籤,標籤縮成兩字 —— 375px 手機放不下六個四字標籤。
// 切換純粹是顯示/隱藏,沒有路由:重新整理回到第一頁是可接受的,
// 換來的是不必引入前端路由這一整層。
let currentTab = "tasks";

function showTab(name) {
  currentTab = name;
  document.querySelectorAll(".navbtn").forEach((b) => {
    b.classList.toggle("is-active", b.dataset.tab === name);
  });
  document.querySelectorAll(".panel").forEach((p) => {
    p.classList.toggle("is-hidden", p.id !== `panel-${name}`);
  });
  window.scrollTo(0, 0);
}

document.querySelectorAll(".navbtn").forEach((btn) => {
  btn.addEventListener("click", () => showTab(btn.dataset.tab));
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
      // 提示文字由後端提供(core/labels.py),前端不自己維護一份措辭。
      // 這裡只提示,不停用任何按鈕:v2 唯一的 AI 路徑是健檢後的改善建議,
      // 而健檢本身是純計算,額度用盡照樣要能按(憲法第二條)。
      showBanner(health.data.aiUnavailableNote, "warn");
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
    await Promise.all([
      reloadHistory(), reloadTasks(), reloadAlerts(), reloadSows(),
      reloadBoars(), reloadRecent(), reloadReview(), reloadDataProblems(), reloadMonthReport(), reloadSettings(),
    reloadCustomTaskSettings(),
    ]);
  }
}

function showBanner(text, tone) {
  $("banner").innerHTML = `<div class="notice notice-${tone}">${escapeHtml(text)}</div>`;
}

// ── 生產健檢 ──

// 其他參考因素:豬舍類型、飼養規模這類不在評級項目裡的背景資訊。
// 跟疾病諮詢的 history 一樣只存在瀏覽器記憶體,換一次健檢就該重填,
// 不必跨頁面保留,所以不寫 localStorage。
let referenceFactors = [];

function renderFactorList() {
  const list = $("factorList");
  if (!list) return;

  if (!referenceFactors.length) {
    list.innerHTML = `<li class="drug-empty">還沒有加入任何參考因素。</li>`;
    return;
  }
  list.innerHTML = referenceFactors
    .map((f) => `
      <li class="drug-item">
        <div>
          <div class="drug-name">${escapeHtml(f.name)}</div>
          ${f.value ? `<div class="drug-meta">${escapeHtml(f.value)}</div>` : ""}
        </div>
        <button type="button" class="drug-remove" data-id="${escapeHtml(f.id)}">移除</button>
      </li>`)
    .join("");
}

function submitNewFactor() {
  const name = $("factorName").value;
  if (!name.trim()) return $("factorName").focus();

  referenceFactors = addFactor(referenceFactors, { name, value: $("factorValue").value });
  renderFactorList();

  $("factorName").value = "";
  $("factorValue").value = "";
  $("factorName").focus();
}

const factorListEl = $("factorList");
const addFactorBtnEl = $("addFactorBtn");
if (factorListEl && addFactorBtnEl) {
  renderFactorList();
  addFactorBtnEl.addEventListener("click", submitNewFactor);
  factorListEl.addEventListener("click", (e) => {
    const btn = e.target.closest(".drug-remove");
    if (btn) {
      referenceFactors = removeFactor(referenceFactors, btn.dataset.id);
      renderFactorList();
    }
  });
}

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
    ? `<button class="btn-soft" id="saveHealthCheck">存入歷史紀錄</button>`
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
      <div class="advice-chat is-hidden" id="adviceChat">
        <div class="advice-chat-thread" id="adviceChatThread"></div>
        <div class="advice-chat-form">
          <textarea id="adviceChatInput" rows="2"
            placeholder="針對這份建議繼續討論,例如:這幾項應該先做哪個?"></textarea>
          <button class="btn-primary" id="adviceChatSend">送出</button>
        </div>
      </div>
    </div>`;
}

// 追問改善建議的對話歷史。只存在瀏覽器記憶體,不上傳保存 ——
// 若改由伺服器依牧場保存,同一座牧場的兩個人會看到彼此的提問內容。
// 伺服器端仍會自行裁切則數與長度,不信任這份資料(config.MAX_HISTORY_*)。
const MAX_ADVICE_HISTORY_TURNS = 20;
let adviceHistory = [];

async function requestAdvice(weaknesses) {
  adviceHistory = [];
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
      body: JSON.stringify({ weaknesses, referenceFactors }),
    });
    const data = await res.json().catch(() => ({}));

    if (res.ok) {
      body.innerHTML = renderMarkdown(data.advice || "");
      // 只有真的拿到建議內容才開放追問,否則使用者會對著一片空白發問
      if (data.advice) $("adviceChat")?.classList.remove("is-hidden");
    } else {
      body.innerHTML = `<div class="notice notice-warn">${escapeHtml(
        data.error || `伺服器錯誤(HTTP ${res.status}),請稍後再試`
      )}</div>`;
    }
  } catch (e) {
    body.innerHTML = `<div class="notice notice-warn">連線失敗:${escapeHtml(String(e))}</div>`;
  }
}

function appendAdviceChatBubble(role, html) {
  const thread = $("adviceChatThread");
  const bubble = document.createElement("div");
  bubble.className = `advice-chat-msg advice-chat-msg-${role}`;
  bubble.innerHTML = html;
  thread.appendChild(bubble);
  thread.scrollTop = thread.scrollHeight;
  return bubble;
}

async function sendAdviceChatMessage() {
  const input = $("adviceChatInput");
  const question = input.value.trim();
  if (!question) return input.focus();

  input.value = "";
  $("adviceChatSend").disabled = true;
  appendAdviceChatBubble("user", escapeHtml(question));
  const loading = appendAdviceChatBubble(
    "assistant", `<span class="loading"><span class="spinner"></span>思考中…</span>`
  );

  let answer = "";
  try {
    const res = await fetch("/api/advise-chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question, weaknesses: lastWeaknesses, referenceFactors, history: adviceHistory,
      }),
    });

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    const parser = new SseParser();
    let bubble = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      for (const event of parser.push(decoder.decode(value, { stream: true }))) {
        if (event.type === "delta") {
          loading.remove();
          answer += event.text;
          if (!bubble) bubble = appendAdviceChatBubble("assistant", "");
          bubble.innerHTML = renderMarkdown(trimDangling(answer)) + '<span class="cursor"></span>';
          $("adviceChatThread").scrollTop = $("adviceChatThread").scrollHeight;
        } else if (event.type === "error") {
          loading.remove();
          appendAdviceChatBubble("assistant",
            `<div class="notice notice-warn">${escapeHtml(event.error)}</div>`);
        }
      }
    }

    if (bubble) bubble.innerHTML = renderMarkdown(answer);
  } catch (e) {
    loading.remove();
    appendAdviceChatBubble("assistant",
      `<div class="notice notice-warn">連線失敗:${escapeHtml(String(e))}</div>`);
  } finally {
    $("adviceChatSend").disabled = false;
    if (answer) {
      adviceHistory.push({ role: "user", content: question });
      adviceHistory.push({ role: "assistant", content: answer });
      if (adviceHistory.length > MAX_ADVICE_HISTORY_TURNS) {
        adviceHistory = adviceHistory.slice(-MAX_ADVICE_HISTORY_TURNS);
      }
    }
  }
}

// adviceCard 每次健檢都整個重畫,用事件委派接住聊天輸入框的按鈕與 Enter 鍵。
$("healthResult")?.addEventListener("click", (e) => {
  if (e.target.closest("#adviceChatSend")) sendAdviceChatMessage();
});
$("healthResult")?.addEventListener("keydown", (e) => {
  if (e.target.id === "adviceChatInput" && e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendAdviceChatMessage();
  }
});

// recForm 每次開表單都整個重畫,用事件委派接住耳號輸入框的 Enter 鍵 ——
// 配種一次記多頭時,連續打耳號、按 Enter 加入是最快的操作方式,
// 不必每一筆都伸手去點「+」。
//
// #recSow 這個 id 單頭母豬的表單也在用(sowPickerField),那邊沒有
// 「加入清單」這回事,一定要先檢查目前這張表單是不是多頭模式,
// 否則會去操作根本不存在的 #recSowChips / #recSowAddErr。
$("recForm")?.addEventListener("keydown", (e) => {
  if (e.target.id === "recSow" && e.key === "Enter" && supportsMultiSow(recordCode)) {
    e.preventDefault();
    addSowTag();
  }
});

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  });
}


// ── 工作清單 ──
//
// 依工作類型分組,不按日期 —— 這個場跑批次生產,一週一批,整批母豬同一週
// 做同一件事(specs/v2-facts.md 第 7 條)。渲染在 lib/v2.js,那邊有測試。
let weekStart = null;

async function reloadTasks() {
  const wrap = $("taskGroups");
  if (!wrap || !account.loggedIn) return;

  const { ok, data } = await api(`/api/tasks${weekStart ? `?start=${weekStart}` : ""}`);
  if (!ok) return;

  weekStart = data.weekStart;
  $("weekLabel").textContent = formatWeek(data.weekStart, data.weekEnd);
  const total = data.groups.reduce((n, g) => n + g.tasks.length, 0);
  $("weekSummary").textContent = total
    ? `${data.groups.length} 類工作 ・ 共 ${total} 頭次`
    : "這週沒有推算出來的工作";

  wrap.innerHTML = data.groups.map(taskGroup).join("")
    || '<p class="hint">這週沒有工作。母豬資料還是空的話,可以到「設定」匯入。</p>';

  // 自訂工作:整張卡片在沒有排到任何一項時收起來,不留一個空框。
  const custom = data.custom || [];
  $("customTaskCard")?.classList.toggle("is-hidden", custom.length === 0);
  const box = $("customTasks");
  if (box) box.innerHTML = custom.map(customTaskRow).join("");
}

async function toggleCustomTask(taskId, due, done) {
  const { ok, data } = await api("/api/custom-tasks/done",
                                 postJson({ taskId, due, done }));
  if (!ok) {
    showBanner(data.error || "標記失敗", "warn");
    await reloadTasks();          // 勾選框已經被瀏覽器改掉了,重畫回真實狀態
  }
}

// ── 提醒 ──
async function reloadAlerts() {
  const wrap = $("alertList");
  if (!wrap || !account.loggedIn) return;
  const { ok, data } = await api("/api/alerts");
  if (!ok) return;

  const rows = buildAlerts(data);
  wrap.innerHTML = rows.map(alertRow).join("")
    || '<p class="hint">目前沒有需要處理的異常。</p>';
  $("navDot")?.classList.toggle("is-hidden",
    !rows.some((r) => r.tone === "urgent"));
}

// ── 母豬 ──
//
// 兩份陣列刻意分開:
// `sows` 只有在場的,紀錄表單的耳號選單用這份 —— 死亡/淘汰的母豬不該
//   出現在「配種」之類新事件的選單裡,選了送出去也只會被伺服器拒絕。
// `allSows` 含已離群的,母豬頁的清單/搜尋用這份 —— 死亡/淘汰後這頭
//   母豬還是要看得到、找得到,不能整個從畫面上消失(實際踩過的問題:
//   記成死亡後原本的預設列表跟搜尋都找不到她了)。
let sows = [];
let allSows = [];

// 母豬清單一次畫幾筆。
const SOW_LIST_LIMIT = 10;

async function reloadSows() {
  if (!$("sowList")) return;
  // 未登入時要**清空**,不能只是提早 return —— 提早 return 會把上一個
  // 帳號的母豬留在 allSows 與畫面上(reloadBoars 一直是清空的,這裡
  // 漏了,兩邊行為不一致正是換帳號看到別人資料的其中一條路徑)。
  if (!account.loggedIn) {
    sows = [];
    allSows = [];
    renderAnimalList();
    return;
  }
  const [active, all] = await Promise.all([api("/api/sows"), api("/api/sows?all=1")]);
  sows = active.ok ? active.data.sows : [];
  // 在場的排前面:大多數瀏覽情境還是想先看到目前在場的,而且這個場
  // 光在場就有 451 頭,早就超過下面的 100 筆顯示上限 —— 已離群的
  // 排到後面,靠搜尋找得到,不會擠掉原本就看得到的在場母豬。
  allSows = all.ok
    ? [...all.data.sows].sort((a, b) => (a.status === "active") === (b.status === "active")
        ? 0 : a.status === "active" ? -1 : 1)
    : [];
  renderAnimalList();
}

// 母豬/公豬清單切換(已確認的設計決定:公豬卡跟母豬卡同頁切換)。
let animalView = "sow";

function renderAnimalList() {
  return animalView === "boar" ? renderBoarList() : renderSowList();
}

function renderSowList() {
  const list = $("sowList");
  if (!list) return;

  const q = ($("sowSearch")?.value || "").trim().toLowerCase();
  const shown = q ? allSows.filter((s) => s.earTag.toLowerCase().includes(q)) : allSows;
  $("sowCount").textContent = q
    ? `符合 ${shown.length} 頭 / 在場 ${sows.length} 頭`
    : `在場 ${sows.length} 頭 ・ 歷史共 ${allSows.length} 頭`;

  if (!shown.length) {
    list.innerHTML = `<p class="hint">${allSows.length
      ? "沒有符合的耳號。" : "還沒有母豬資料,可以到「設定」匯入 PigCHAMP 檔案。"}</p>`;
    return;
  }
  // 只畫前 10 筆。451 頭全畫會讓搜尋時每次輸入都卡一下,而且捲很久也
  // 找不到 —— 這份清單的用法是「搜尋耳號找某一頭」,不是從頭讀到尾。
  list.innerHTML = shown.slice(0, SOW_LIST_LIMIT).map(sowRow).join("")
    + (shown.length > SOW_LIST_LIMIT
        ? `<p class="hint">只顯示前 ${SOW_LIST_LIMIT} 頭,請輸入耳號縮小範圍。</p>` : "");
}

function renderBoarList() {
  const list = $("sowList");
  if (!list) return;

  // 瀏覽/搜尋用 allBoars(含已死亡)—— 死亡的公豬還是要看得到、找得到,
  // 不能整個從畫面上消失。記錄表單的選單另外用在場的 boars(見
  // reloadBoars),兩者刻意分開,理由跟母豬那邊的 sows/allSows 一樣。
  const q = ($("sowSearch")?.value || "").trim().toLowerCase();
  const shown = q ? allBoars.filter((b) => b.earTag.toLowerCase().includes(q)) : allBoars;
  $("sowCount").textContent = q
    ? `符合 ${shown.length} 頭 / 在場 ${boars.length} 頭`
    : `在場 ${boars.length} 頭 ・ 歷史共 ${allBoars.length} 頭`;

  if (!shown.length) {
    list.innerHTML = `<p class="hint">${allBoars.length
      ? "沒有符合的耳號。" : "還沒有公豬資料,可以到「紀錄」記一筆種豬進場。"}</p>`;
    return;
  }
  list.innerHTML = shown.slice(0, SOW_LIST_LIMIT).map(boarRow).join("")
    + (shown.length > SOW_LIST_LIMIT
        ? `<p class="hint">只顯示前 ${SOW_LIST_LIMIT} 頭,請輸入耳號縮小範圍。</p>` : "");
}

// 目前開著的是哪一頭母豬的卡片。記錄或收回事件之後,若剛好是這一頭,
// 要重新整理卡片 —— 不然耳號、狀態、生產表現都會停在記錄之前的舊資料
// (實際踩到的情形:記成死亡或淘汰後,卡片上的耳號沒有變,因為原本只
// 重讀列表跟提醒,沒有重讀已經開著的這張卡)。
let openSowId = null;

async function openSow(sowId) {
  const { ok, data } = await api(`/api/sows/${sowId}`);
  if (!ok) return showBanner(data.error || "讀不到這頭母豬", "warn");
  openSowId = sowId;

  const s = data.sow;
  const box = $("sowDetail");
  box.classList.remove("is-hidden");
  // 驗孕記錄一律全部保留,其餘事件只留最新的部分 —— 見 lib/v2.js。
  const shownEvents = visibleEvents(data.events, TIMELINE_LIMIT);
  box.innerHTML = `
    <div class="card">
      <div class="head-top">
        <div>
          <div class="tag">${escapeHtml(s.earTag)}</div>
          <div class="breed">${escapeHtml(s.breed || "品種未填")}${
            s.birthDate ? ` ・ ${s.birthDate} 出生` : ""}</div>
        </div>
        <div class="parity"><b>${s.parity}</b><span>胎次</span></div>
      </div>
      ${statusPills(data.status)}
      <div class="meta">
        <div><span>父系耳號</span><br><b>${escapeHtml(s.sireTag || "—")}</b></div>
        <div><span>母系耳號</span><br><b>${escapeHtml(s.damTag || "—")}</b></div>
      </div>
    </div>
    ${performanceGrid(data.performance)}
    ${weanScoreCard(data.events)}
    <div class="card">
      <h3>事件時間軸</h3>
      <p class="hint">${timelineCaption(data.events.length, shownEvents.length)}</p>
      <div class="tl" style="margin-top:12px">
        ${pendingCheckRow(data.status)}
        ${shownEvents.map(eventRow).join("")}
      </div>
    </div>`;
  box.scrollIntoView({ behavior: "smooth", block: "start" });
}

// 目前開著的是哪一頭公豬的卡片,理由跟 openSowId 一樣。
let openBoarId = null;

async function openBoar(boarId) {
  const { ok, data } = await api(`/api/boars/${boarId}`);
  if (!ok) return showBanner(data.error || "讀不到這頭公豬", "warn");
  openBoarId = boarId;

  const b = data.boar;
  const box = $("sowDetail");
  box.classList.remove("is-hidden");
  const shownEvents = visibleEvents(data.events, TIMELINE_LIMIT);
  box.innerHTML = `
    <div class="card">
      <div class="head-top">
        <div>
          <div class="tag">${escapeHtml(b.earTag)}</div>
          <div class="breed">${escapeHtml(b.breed || "品種未填")}${
            b.entryDate ? ` ・ ${b.entryDate} 進場` : ""}</div>
        </div>
        ${b.status === "dead" ? '<span class="sow-exited-badge">已死亡</span>' : ""}
      </div>
      <div class="meta">
        <div><span>父系耳號</span><br><b>${escapeHtml(b.sireTag || "—")}</b></div>
        <div><span>母系耳號</span><br><b>${escapeHtml(b.damTag || "—")}</b></div>
      </div>
    </div>
    ${boarPerformanceGrid(data.performance)}
    <div class="card">
      <h3>事件時間軸</h3>
      <p class="hint">${timelineCaption(data.events.length, shownEvents.length)}</p>
      <div class="tl" style="margin-top:12px">
        ${shownEvents.map(eventRow).join("")}
      </div>
    </div>`;
  box.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ── 匯入 ──
let importText = "";

async function previewImport(file) {
  $("importResult").innerHTML = '<p class="hint">解析中…</p>';
  importText = await file.text();

  const { ok, data } = await api("/api/import/preview", postJson({ content: importText }));
  if (!ok) {
    $("importResult").innerHTML =
      `<div class="notice notice-warn">${escapeHtml(data.error || "解析失敗")}</div>`;
    return;
  }

  const codes = Object.entries(data.byCode).sort((a, b) => b[1] - a[1])
    .map(([c, n]) => `${escapeHtml(eventName(c))} ${n}`).join(" ・ ");

  $("importResult").innerHTML = `
    <div class="notice notice-info">
      偵測到 <b>${data.sows}</b> 頭母豬、<b>${data.boars}</b> 頭公豬、
      <b>${data.events}</b> 筆事件
      ${data.dateRange ? `<br>${data.dateRange[0]} ~ ${data.dateRange[1]}` : ""}
      <br><span class="hint">${codes}</span>
      ${data.badLineCount
        ? `<br><span class="hint">${data.badLineCount} 行無法解析,會略過</span>` : ""}
      ${data.semenCollections ? `<br><span class="hint">
        公豬會建起來(配種記錄要選),其中 ${data.semenCollections} 筆採精記錄會一併匯入${
          data.semenCollectionsSkipped
            ? `(另有 ${data.semenCollectionsSkipped} 筆耳號看不出對應哪頭公豬,略過)` : ""}
        </span>` : ""}
      ${data.semenQualityRows ? `<br><span class="hint">
        ${data.semenQualityRows} 筆精液品質(SP)記錄不在這個版本的匯入範圍內,不會寫入</span>` : ""}
    </div>
    ${data.oddBoarTags?.length ? `
      <div class="notice notice-warn" style="margin-top:12px">
        有 <b>${data.oddBoarTags.length}</b> 個公豬耳號看起來像日期,
        可能是原始檔案填錯欄位。<b>身分照樣建起來不會改動</b>,
        會出現在配種記錄的公豬選單裡;但這些耳號底下的採精記錄
        無法歸戶,不會匯入。
        <br><span class="hint">${escapeHtml(data.oddBoarTags.slice(0, 6).join("、"))}${
          data.oddBoarTags.length > 6 ? " …" : ""}</span>
      </div>` : ""}
    ${data.anomalies.length ? `
      <div class="label" style="margin-top:16px">可疑記錄 ${data.anomalies.length} 筆</div>
      <p class="hint">勾起來的不納入統計。<b>資料仍會存起來</b>,之後可以改回來。</p>
      ${data.anomalies.map((a) => `
        <label class="mine">
          <input type="checkbox" class="exclude-line" value="${a.line}" checked>
          <div class="mine-b">
            <div class="mine-n">${escapeHtml(a.earTag)} ・ ${escapeHtml(eventName(a.code))} ・ ${a.date}</div>
            <div class="mine-s">${escapeHtml(a.reason)}(第 ${a.line} 行)</div>
          </div>
        </label>`).join("")}` : ""}
    <button class="submit" id="importConfirm">確認匯入</button>`;
}

async function commitImport() {
  const excludeLines = [...document.querySelectorAll(".exclude-line:checked")]
    .map((el) => Number(el.value));
  const btn = $("importConfirm");
  btn.disabled = true;
  btn.textContent = "匯入中…";

  const { ok, data } = await api("/api/import",
                                 postJson({ content: importText, excludeLines }));
  if (!ok) {
    $("importResult").innerHTML =
      `<div class="notice notice-warn">${escapeHtml(data.error || "匯入失敗")}</div>`;
    return;
  }
  $("importResult").innerHTML = `
    <div class="notice notice-good">匯入完成:${data.sows} 頭母豬、${data.events} 筆事件${
      data.semenCollections ? `、${data.semenCollections} 筆採精記錄` : ""}${
      data.excluded ? `,其中 ${data.excluded} 筆不納入統計` : ""}。</div>`;
  importText = "";
  // 公豬清單也要重讀 —— 匯入會建立公豬身分(而且現在還會一併帶進
  // 採精記錄),不重讀的話公豬頁的清單跟紀錄頁的耳號選單會停在
  // 匯入前的樣子(第一次匯入時甚至是空的)。
  await Promise.all([reloadSows(), reloadBoars(), reloadTasks(), reloadAlerts()]);
}

// ── v2 事件接線 ──
//
// 一律用事件委派:上面幾個 render 都是整段 innerHTML 重畫,直接綁在元素上
// 的話重畫後就失效了。
document.addEventListener("click", (e) => {
  const fold = e.target.closest("[data-fold]");
  if (fold) {
    const box = $(fold.dataset.fold);
    const nowFolded = box?.classList.toggle("tags-fold");
    fold.textContent = nowFolded
      ? `展開全部 ${box.childElementCount} 頭 ›` : "收合 ⌃";
    return;
  }
  // 母豬/公豬清單的一列(lib/v2.js 的 .sow-row)。**不是**紀錄表單裡
  // 「一頭一列」的 .rec-row —— 兩者曾經同名,把表單那組改名時連這裡
  // 一起改掉,結果點耳號打不開母豬卡。
  // 記錄檢查的一列 —— 點進去看那頭母豬的完整時間軸,才判斷得出哪一筆
  // 才是記錯的。要先切到母豬頁,否則卡片畫在一個看不見的分頁裡。
  const dp = e.target.closest(".dp-row");
  if (dp) {
    if (e.target.closest(".dp-fix")) {
      fixingType = dp.dataset.type;
      return openFixForm(dp);
    }
    // 看她的完整時間軸才判斷得出哪一筆才是記錯的
    showTab("sows");
    return openSow(Number(dp.dataset.sow));
  }
  if (e.target.id === "dpFixCancel") {
    return $("dpFixForm").classList.add("is-hidden");
  }
  if (e.target.id === "dpFixSubmit") return submitFix();

  const row = e.target.closest(".sow-row");
  if (row) {
    if (row.dataset.boar) return openBoar(Number(row.dataset.boar));
    return openSow(Number(row.dataset.sow));
  }

  const tag = e.target.closest(".etag");
  if (tag) { showTab("sows"); return openSow(Number(tag.dataset.sow)); }

  const view = e.target.closest("[data-animal-view]");
  if (view) {
    animalView = view.dataset.animalView;
    document.querySelectorAll("[data-animal-view]")
      .forEach((b) => b.classList.toggle("is-active", b === view));
    $("animalHeading").textContent = animalView === "boar" ? "公豬資訊" : "母豬資訊";
    const search = $("sowSearch");
    if (search) search.value = "";
    $("sowDetail")?.classList.add("is-hidden");
    return renderAnimalList();
  }

  if (e.target.id === "importConfirm") return commitImport();
  if (e.target.id === "weekPrev") { weekStart = shiftDate(weekStart, -7); return reloadTasks(); }
  if (e.target.id === "weekNext") { weekStart = shiftDate(weekStart, 7); return reloadTasks(); }
  if (e.target.id === "mrPrev") {
    closeMonthPicker();
    monthReportMonth = shiftMonth(monthReportMonth, -1);
    return reloadMonthReport();
  }
  if (e.target.id === "mrNext") {
    closeMonthPicker();
    monthReportMonth = shiftMonth(monthReportMonth, 1);
    return reloadMonthReport();
  }
  if (e.target.id === "mrLabel") return toggleMonthPicker();
  if (e.target.id === "mrPickerYearPrev") { mrPickerYear -= 1; return renderMonthPicker(); }
  if (e.target.id === "mrPickerYearNext") { mrPickerYear += 1; return renderMonthPicker(); }
  const monthPick = e.target.closest("[data-mr-pick]");
  if (monthPick) {
    monthReportMonth = monthPick.dataset.mrPick;
    closeMonthPicker();
    return reloadMonthReport();
  }
});

$("sowSearch")?.addEventListener("input", renderAnimalList);
$("importPick")?.addEventListener("click", () => $("importFile")?.click());
$("importFile")?.addEventListener("change", (e) => {
  const file = e.target.files?.[0];
  e.target.value = "";      // 清空才能連續選同一個檔案
  if (file) previewImport(file);
});

// ── 紀錄頁 ──

// 跟 sows/allSows 同樣的分法:boars 只有在場的,記錄表單的耳號選單
// (配種/採精/種豬死亡)用這份 —— 已經死亡的公豬不該出現在這些選單裡。
// allBoars 含已死亡的,公豬頁的清單/搜尋用這份。
let boars = [];
let allBoars = [];
let pens = [];               // 移欄表單用,含即時佔用狀態
let recordCode = null;      // 目前開著的表單是哪一種事件
// 「已記錄」預設只顯示前幾筆,其餘收起來(使用者要求)。10 筆大約是
// 一個畫面的高度,足夠確認「剛剛那幾筆記進去了沒」。
const RECENT_COLLAPSED = 10;
let fixingEventId = null;   // 記錄檢查裡正在修正的那一筆
let fixingType = "";
let recentEvents = [];      // 最近 7 天的記錄,展開/收合共用同一份
let recentExpanded = false;

let recSowTags = [];        // 配種一次記多頭時,目前已加入清單的耳號
// 這次記錄是不是記在「耳號看不清楚」的母豬身上。用一個布林值而不是往
// recSowTags 塞一個特殊字串 —— 那個陣列裡放的都是真實耳號,混一個哨兵
// 值進去,哪天有人真的把豬取名叫那個字串就會壞掉。
let recUnknownSow = false;

async function reloadBoars() {
  // 未登入就別發這個請求。少了這道守衛,登入畫面上會固定丟一個 401 到
  // console —— 假錯誤最麻煩的地方是它會蓋掉真的錯誤,這次就是這樣多花了
  // 很多時間在追一個早就不存在的變數。
  if (!account.loggedIn) { boars = []; allBoars = []; return; }
  const [active, all] = await Promise.all([api("/api/boars"), api("/api/boars?all=1")]);
  boars = active.ok ? active.data.boars : [];
  allBoars = all.ok ? all.data.boars : [];
  renderAnimalList();
}

async function reloadPens() {
  if (!account.loggedIn) { pens = []; return; }
  const { ok, data } = await api("/api/pens");
  pens = ok ? data.pens : [];
}

function closeRecordForm() {
  recordCode = null;
  recSowTags = [];
  $("recForm").classList.add("is-hidden");
  $("recForm").innerHTML = "";
}

async function openRecordForm(code) {
  const spec = formFor(code);
  if (!spec) return;
  recordCode = code;
  recSowTags = [];
  recUnknownSow = false;

  // 欄位佔用狀態隨時在變,打開表單當下重抓一次才不會讓使用者選到
  // 其實已經有豬的欄位(其他事件的表單不需要這個,沒有額外請求)。
  if (code === "MV") await reloadPens();

  const box = $("recForm");
  box.classList.remove("is-hidden");
  box.innerHTML = `
    <div class="rec-head">
      <h3>${escapeHtml(spec.label)}</h3>
      <button type="button" class="btn-ghost" id="recCancel">取消</button>
    </div>
    ${SIDE_EFFECTS[code]
      ? `<p class="rec-warn-note">送出後:${escapeHtml(SIDE_EFFECTS[code])}</p>` : ""}
    ${createsNewAnimal(code) ? newAnimalFields()
      // 一頭一列時,耳號在各自的列裡,共用區不再畫選擇器 —— 種豬死亡
      // 例外:母豬/公豬的切換鈕仍然是整批共用的,只是不帶耳號欄位。
      : usesPerSowRows(code) ? (targetsEither(code) ? kindToggleOnly() : "")
      : targetsEither(code) ? eitherAnimalFields()
      : targetsBoar(code) ? boarPickerField()
      : targetsNothing(code) ? ""
      : supportsMultiSow(code) ? multiSowPickerField() : sowPickerField()}
    ${supportsMultiService(code) ? serviceRowsField()
      : `<label class="fld"><span>日期</span>
      <input type="date" id="recDate" value="${todayIso()}"></label>`}
    ${usesPerSowRows(code)
      ? spec.fields.filter((f) => f.shared).map((f) => fieldMarkup(f)).join("")
        + perSowRowsField(spec)
      : spec.fields.filter((f) => !f.perService).map(fieldMarkup).join("")}
    <p class="rec-err is-hidden" id="recErr"></p>
    <button type="button" class="btn-primary" id="recSubmit">記錄</button>`;

  if (supportsMultiService(code)) renumberServiceRows();
  if (usesPerSowRows(code)) renumberSowRows();
  renderUnknownButton();
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/** 一頭一列:分娩、離乳、寄養這些「每頭數字都不一樣」的事件。
 *
 * 使用者的原話是「記錄一隻送一隻太慢了,而且每送出一次都要等個幾秒」。
 * 配種那種「耳號加很多筆、欄位整批共用」的做法對這些事件行不通 ——
 * 活仔數、離乳頭數共用一組值等於把第一頭的數字複製給所有豬,那是憑空
 * 捏造資料(憲法第三條)。所以改成每頭一列,自己的耳號配自己的數字。
 *
 * 日期仍然整批共用:整批同一天離乳、當天記當天的分娩,是這個場的做法;
 * 每列各放一個日期會讓 375px 手機上的一列高到看不完。真的跨日就分兩次記。
 */
function perSowRowsField(spec) {
  return `
    <div id="recSowRows">${perSowRowMarkup(spec, 0)}</div>
    <button type="button" class="btn-ghost" id="recAddSowRow">+ 再加一頭</button>
    <datalist id="sowTags">
      ${sows.map((s) => `<option value="${escapeHtml(s.earTag)}"></option>`).join("")}
    </datalist>
    <datalist id="boarTags">
      ${boars.map((b) => `<option value="${escapeHtml(b.earTag)}"></option>`).join("")}
    </datalist>`;
}

/** 第 i 列。欄位 id 帶 __i 後綴,否則每列的 id 會撞在一起,
 * getElementById 只找得到第一列,第二頭以後的數字全部讀成第一頭的。
 */
function perSowRowMarkup(spec, i) {
  const sfx = `__${i}`;
  return `
    <div class="rec-row" data-row="${i}">
      <div class="rec-row-h">
        <span class="rec-row-n"></span>
        <button type="button" class="btn-ghost rec-row-del">移除</button>
      </div>
      ${rowAnimalPicker(spec)}
      ${spec.fields.filter((f) => !f.shared).map((f) => fieldMarkup(f, sfx)).join("")}
    </div>`;
}

/** 只有母豬/公豬切換鈕,沒有耳號欄位 —— 一頭一列的種豬死亡用。
 * 整批是同一種(一次死的通常是同一欄的),耳號則各列各填。
 */
function kindToggleOnly() {
  return `
    <div class="seg" id="recKind">
      <button type="button" class="seg-b is-active" data-kind="sow">母豬</button>
      <button type="button" class="seg-b" data-kind="boar">公豬</button>
    </div>`;
}

/** 列裡的動物選擇器,依這種事件記在誰身上而不同:
 *
 *   母豬事件(分娩、離乳、仔豬死亡、淘汰、移欄)→ 母豬耳號
 *   採精                                      → 公豬耳號
 *   種豬死亡(記在母豬或公豬,由上面的切換鈕決定)→ 兩種耳號都給建議
 *   肉豬死亡                                   → 沒有耳號,整列就是一頭
 *   種豬進場                                   → 耳號本身就是欄位之一
 */
function rowAnimalPicker(spec) {
  if (spec.target === "new" || spec.target === "none") return "";
  const boarSide = spec.target === "boar";
  const label = boarSide ? "公豬耳號"
    : spec.target === "either" ? "耳號" : "母豬耳號";
  const list = boarSide ? "boarTags" : "sowTags";
  return `
    <label class="fld"><span>${label}</span>
      <input list="${list}" class="rec-row-tag" inputmode="numeric"
             placeholder="輸入或選擇耳號" autocomplete="off"></label>`;
}

function renumberSowRows() {
  const rows = [...document.querySelectorAll("#recSowRows .rec-row")];
  rows.forEach((row, i) => {
    const label = row.querySelector(".rec-row-n");
    if (label) label.textContent = `第 ${i + 1} 頭`;
    const del = row.querySelector(".rec-row-del");
    if (del) del.classList.toggle("is-hidden", rows.length <= 1);
  });
}

function addSowRow() {
  const box = $("recSowRows");
  const spec = formFor(recordCode);
  if (!box || !spec) return;
  // 用目前最大的 data-row + 1,不是列數 —— 中間刪過列的話列數會重複,
  // 後綴撞在一起就等於兩列共用同一組欄位。
  const used = [...box.querySelectorAll(".rec-row")].map((r) => Number(r.dataset.row));
  box.insertAdjacentHTML("beforeend",
    perSowRowMarkup(spec, used.length ? Math.max(...used) + 1 : 0));
  renumberSowRows();
}

function removeSowRow(row) {
  const box = $("recSowRows");
  if (!row || !box || box.querySelectorAll(".rec-row").length <= 1) return;
  row.remove();
  renumberSowRows();
}

/** 讀出每一列。耳號空白的列直接略過 —— 按了「再加一頭」又不用時,
 * 不該逼使用者先移除才能送出。
 */
function readSowRows(spec) {
  return [...document.querySelectorAll("#recSowRows .rec-row")]
    .map((row) => {
      const raw = readRecordFields(spec, `__${row.dataset.row}`);
      // 種豬進場沒有「選一頭既有母豬」的欄位,耳號本身就是那一列的識別。
      const tag = createsNewAnimal(recordCode)
        ? (raw.earTag || "").trim()
        : (row.querySelector(".rec-row-tag")?.value || "").trim();
      return { tag, raw };
    })
    // 肉豬死亡沒有耳號,改用「這列有沒有填東西」判斷 —— 否則整批都會
    // 因為 tag 是空的而被濾掉。其餘事件仍以耳號為準:按了「再加一頭」
    // 又沒用時不該逼使用者先移除才能送出。
    .filter((r) => (spec.target === "none"
      ? Object.values(r.raw).some((v) => v !== "" && v !== false && v != null)
      : r.tag));
}

/** 配種列:一次發情連配的每一天各一列(日期 + 公豬)。
 *
 * 為什麼不是整張表單共用一個日期與一隻公豬:一頭母豬一次發情通常連配
 * 2–3 天、一天一次,而且**每天可能換不同公豬**(使用者說明)。共用一組
 * 的話,同一批配種要分成好幾次送出、母豬耳號每次重打一遍。
 *
 * 母豬耳號與發情穩定度仍然整批共用 —— 整批母豬是同步配的,而發情穩定度
 * 描述的是這次發情,不是某一天。
 *
 * 預設日期往回排到今天為止(最後一列是今天):使用者是整批配完才一次記
 * 進來的,所以最後一次通常就是今天,這樣多數情況一個字都不用改。
 */
function serviceRowsField() {
  const rows = Array.from({ length: DEFAULT_SERVICE_ROWS }, (_, i) =>
    serviceRowMarkup(dayOffsetIso(i - (DEFAULT_SERVICE_ROWS - 1))));
  return `
    <div class="svc-rows" id="recServices">${rows.join("")}</div>
    <button type="button" class="btn-ghost" id="recAddService">+ 再加一次配種</button>`;
}

function serviceRowMarkup(dateIso) {
  return `
    <div class="svc-row">
      <label class="fld"><span class="svc-n"></span>
        <input type="date" class="svc-date" value="${dateIso}"></label>
      <label class="fld"><span>公豬</span>
        <input list="boarTags" class="svc-boar" placeholder="輸入或選擇耳號"
               autocomplete="off"></label>
      <button type="button" class="btn-ghost svc-del" title="移除這一次">移除</button>
    </div>
    <datalist id="boarTags">
      ${boars.map((b) => `<option value="${escapeHtml(b.earTag)}"></option>`).join("")}
    </datalist>`;
}

/** 今天加減幾天的 ISO 日期。用本地時區,理由同 todayIso()。 */
function dayOffsetIso(offset) {
  const d = new Date();
  d.setDate(d.getDate() + offset);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** 讀出畫面上的每一次配種。空白日期的列直接略過 —— 使用者按了「再加
 * 一次」又決定不用時,不該逼他先移除才能送出。
 */
function readServiceRows() {
  return Array.from(document.querySelectorAll("#recServices .svc-row"))
    .map((row) => ({
      date: row.querySelector(".svc-date")?.value || "",
      boarTag: (row.querySelector(".svc-boar")?.value || "").trim(),
    }))
    .filter((s) => s.date);
}

/** 加一列。一次發情是連著幾天配的,所以新的一列預設接在最後一列的隔天。
 *
 * **但隔天如果已經是未來,就往前補一天、插在最前面**:使用者是整批配完
 * 才一次記進來的,最後一次通常就是今天,這時候要的是「原來還配了前一天」
 * 而不是一個還沒發生的明天。給未來日期等於預設就是錯的,他每次都得改。
 */
function addServiceRow() {
  const box = $("recServices");
  if (!box) return;
  const dates = readServiceRows().map((s) => s.date).sort();
  if (!dates.length) {
    box.insertAdjacentHTML("beforeend", serviceRowMarkup(todayIso()));
    return renumberServiceRows();
  }

  const shift = (iso, days) => {
    const d = new Date(`${iso}T00:00:00`);
    d.setDate(d.getDate() + days);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  };

  const after = shift(dates[dates.length - 1], 1);
  if (after <= todayIso()) {
    box.insertAdjacentHTML("beforeend", serviceRowMarkup(after));
  } else {
    box.insertAdjacentHTML("afterbegin", serviceRowMarkup(shift(dates[0], -1)));
  }
  renumberServiceRows();
}

function removeServiceRow(row) {
  const box = $("recServices");
  if (!row || !box || box.querySelectorAll(".svc-row").length <= 1) return;
  row.remove();
  renumberServiceRows();
}

/** 重新編號並決定移除鈕要不要出現(只剩一列時不給移除)。 */
function renumberServiceRows() {
  const rows = Array.from(document.querySelectorAll("#recServices .svc-row"));
  rows.forEach((row, i) => {
    const label = row.querySelector(".svc-n");
    if (label) label.textContent = `第 ${i + 1} 次`;
    const del = row.querySelector(".svc-del");
    if (del) del.classList.toggle("is-hidden", rows.length <= 1);
  });
}

function sowPickerField() {
  // datalist 讓耳號可以直接打也可以選 —— 451 頭的下拉選單捲不完,
  // 而牧場主記得住耳號,打字比找快。
  return `
    <label class="fld"><span>母豬耳號</span>
      <input list="sowTags" id="recSow" inputmode="numeric"
             placeholder="輸入或選擇耳號" autocomplete="off"></label>
    <datalist id="sowTags">
      ${sows.map((s) => `<option value="${escapeHtml(s.earTag)}"></option>`).join("")}
    </datalist>
    ${unknownSowButton()}`;
}

// 配種一次記多頭:耳號一個一個加進清單,公豬跟發情穩定度整批共用
// (spec.fields 那些欄位不變)。加入時就驗證耳號存在,而不是等送出才
// 一次噴一堆錯誤 —— 巡欄時打錯一個字元,馬上就知道,不必等打完一整批。
function multiSowPickerField() {
  return `
    <label class="fld"><span>母豬耳號(可連續加入多筆)</span>
      <div class="rec-tag-row">
        <input list="sowTags" id="recSow" inputmode="numeric"
               placeholder="輸入耳號後按 Enter 或 +" autocomplete="off">
        <button type="button" class="btn-soft" id="recSowAdd">+</button>
      </div></label>
    <datalist id="sowTags">
      ${sows.map((s) => `<option value="${escapeHtml(s.earTag)}"></option>`).join("")}
    </datalist>
    <p class="rec-err is-hidden" id="recSowAddErr"></p>
    <div class="chips" id="recSowChips"></div>
    ${unknownSowButton()}`;
}

/** 耳號磨損看不清楚時用的按鈕。**耳號用日期**(使用者決定):8/17 配的
 * 那頭就是「不明-0817」,連配二三天取第一天,跟預產期的算法一致。
 *
 * 日期在送出時才換算,不是按下去的當下 —— 使用者按完還可能回頭改日期,
 * 當下算好的話耳號會跟實際記錄的日期對不上。
 */
function unknownSowButton() {
  return `
    <button type="button" class="btn-ghost rec-unknown" id="recUnknownBtn"></button>
    <p class="fld-h">耳號看不清楚時用。系統會用配種日期當她的暫時耳號,
       之後看得到耳號再併回真正那一頭。</p>`;
}

/** 送出時才算的暫時耳號:MMDD。配種取第一次的日期(跟預產期同一天),
 * 其他事件取事件當天。
 */
function unknownTagFor(when) {
  const [, m, d] = when.split("-");
  return `${m}${d}`;
}

function renderUnknownButton() {
  const btn = $("recUnknownBtn");
  if (!btn) return;
  btn.textContent = recUnknownSow ? "✓ 不明母豬(已選)" : "耳號看不清楚?記為不明母豬";
  btn.classList.toggle("is-active", recUnknownSow);
}

function renderSowChips() {
  const box = $("recSowChips");
  if (!box) return;
  box.innerHTML = recSowTags.map((tag) => `
    <button type="button" class="chip" data-remove-tag="${escapeHtml(tag)}"
    >${escapeHtml(tag)} ×</button>`).join("");
}

/** 把輸入框裡目前的耳號加進清單。驗證耳號真的存在、不重複加入 ——
 * 兩者都不算致命錯誤(只是這一個字元沒加成功),用同一個小提示欄位
 * 顯示,不必動用整張表單共用的 #recErr。
 */
function addSowTag() {
  const field = $("recSow");
  const err = $("recSowAddErr");
  if (!field) return;
  const tag = field.value.trim();
  err.classList.add("is-hidden");

  if (!tag) return;
  if (!sows.some((s) => s.earTag === tag)) {
    err.textContent = `找不到耳號 ${tag}`;
    err.classList.remove("is-hidden");
    return;
  }
  if (recSowTags.includes(tag)) {
    err.textContent = `${tag} 已經加過了`;
    err.classList.remove("is-hidden");
    return;
  }

  recSowTags.push(tag);
  field.value = "";
  renderSowChips();
  field.focus();
}

function removeSowTag(tag) {
  recSowTags = recSowTags.filter((t) => t !== tag);
  renderSowChips();
}

function boarPickerField() {
  return `
    <label class="fld"><span>公豬耳號</span>
      <input list="boarPickTags" id="recBoar" placeholder="輸入或選擇耳號"
             autocomplete="off"></label>
    <datalist id="boarPickTags">
      ${boars.map((b) => `<option value="${escapeHtml(b.earTag)}"></option>`).join("")}
    </datalist>`;
}

function newAnimalFields() {
  return `
    <div class="seg" id="recKind">
      <button type="button" class="seg-b is-active" data-kind="sow">母豬</button>
      <button type="button" class="seg-b" data-kind="boar">公豬</button>
    </div>`;
}

// 種豬死亡:記在母豬還是公豬身上,由記錄當下選 —— 跟 newAnimalFields
// 用同一顆切換鈕,差別是這裡選了之後耳號選單要跟著換(選母豬就找
// 母豬、選公豬就找公豬),不是像種豬進場那樣兩邊欄位長得一樣。
function eitherAnimalFields() {
  return `
    <div class="seg" id="recKind">
      <button type="button" class="seg-b is-active" data-kind="sow">母豬</button>
      <button type="button" class="seg-b" data-kind="boar">公豬</button>
    </div>
    <div id="recAnimalPicker">${sowPickerField()}</div>`;
}

/** 畫一個欄位。`sfx` 是列的後綴 —— 「一頭一列」的表單(分娩、離乳……)
 * 同一種欄位會出現好幾次,id 撞在一起的話 getElementById 只找得到第一個,
 * 第二頭以後的數字會全部讀成第一頭的。讀值的 readRecordFields() 要帶
 * 同一個後綴,兩邊必須對稱。
 */
function fieldMarkup(field, sfx = "") {
  const hint = field.hint ? `<em class="fld-h">${escapeHtml(field.hint)}</em>` : "";

  if (field.type === "boar") {
    return `
      <label class="fld"><span>${escapeHtml(field.label)}</span>
        <input list="boarTags" id="f_${field.key}${sfx}" placeholder="輸入或選擇耳號"
               autocomplete="off"></label>
      <datalist id="boarTags">
        ${boars.map((b) => `<option value="${escapeHtml(b.earTag)}"></option>`).join("")}
      </datalist>`;
  }
  if (field.type === "checkbox") {
    // 跟 bool 不一樣:bool 是必須二選一的問題(有懷孕/沒懷孕),這裡是
    // 有預設值的勾選框 —— 不勾就是「沒有」,不必強迫使用者每筆都選一次
    // (使用者決定)。
    return `
      <label class="fld fld-checkbox">
        <input type="checkbox" id="f_${field.key}${sfx}">
        <span>${escapeHtml(field.label)}</span>
      </label>${hint}`;
  }
  if (field.type === "bool") {
    return `
      <div class="fld"><span>${escapeHtml(field.label)}</span>
        <div class="seg" data-field="${field.key}${sfx}">
          <button type="button" class="seg-b" data-val="true">${escapeHtml(field.yes)}</button>
          <button type="button" class="seg-b" data-val="false">${escapeHtml(field.no)}</button>
        </div>
      </div>`;
  }
  if (field.type === "score") {
    // 1~5 的按鈕而不是輸入框:巡欄時單手操作,而且按鈕本身就說明了範圍
    return `
      <div class="fld"><span>${escapeHtml(field.label)}</span>
        <div class="seg score" data-field="${field.key}${sfx}">
          ${[1, 2, 3, 4, 5].map((n) =>
            `<button type="button" class="seg-b" data-val="${n}">${n}</button>`).join("")}
        </div>${hint}
      </div>`;
  }
  if (field.type === "choice") {
    // 選項可以是純字串,也可以是 {value,label}(值跟顯示文字不同時,
    // 例如區域:存的是 mating,顯示的是「配種區」)。
    // 選到「其他」時要跳出打字框讓使用者說清楚實際原因,不能就這樣存一個
    // 不具體的「其他」——見 lib/record.js 的 OTHER_REASON。預設藏著,
    // 點到「其他」才由下面 document 的委派點擊處理器切換顯示。
    return `
      <div class="fld"><span>${escapeHtml(field.label)}</span>
        <div class="chips" data-field="${field.key}${sfx}">
          ${field.options.map((o) => {
            const opt = typeof o === "string" ? { value: o, label: o } : o;
            return `<button type="button" class="chip" data-val="${escapeHtml(opt.value)}"
                    >${escapeHtml(opt.label)}</button>`;
          }).join("")}
        </div>
        ${hasOtherOption(field)
          ? `<input type="text" id="f_${field.key}_other${sfx}" class="fld-other is-hidden"
                    placeholder="請輸入實際原因">`
          : ""}${hint}
      </div>`;
  }
  if (field.type === "pen") {
    // 直接打欄位編號,不是從清單選 —— 一區動輒幾百個欄位,要求先建好
    // 清單才能用根本不會有人做(使用者要求)。datalist 只是輔助,
    // 打過的編號會出現在建議裡,但永遠可以打一個新的。
    return `
      <label class="fld"><span>${escapeHtml(field.label)}</span>
        <input list="penNames" id="f_${field.key}${sfx}" placeholder="輸入欄位編號"
               autocomplete="off"></label>
      <datalist id="penNames">
        ${[...new Set(pens.map((p) => p.name))]
          .map((n) => `<option value="${escapeHtml(n)}"></option>`).join("")}
      </datalist>${hint}`;
  }
  if (field.type === "tri") {
    // 跟 score 同樣的按鈕群,只是選項是 {value, label} —— 存的值(穩定
    // 判斷)跟按鈕上顯示的符號分開,符號以後想換不必動到已存的資料。
    return `
      <div class="fld"><span>${escapeHtml(field.label)}</span>
        <div class="seg tri" data-field="${field.key}${sfx}">
          ${field.options.map((o) =>
            `<button type="button" class="seg-b" data-val="${escapeHtml(o.value)}"
             >${escapeHtml(o.label)}</button>`).join("")}
        </div>${hint}
      </div>`;
  }
  const type = field.type === "date" ? "date"
             : field.type === "int" || field.type === "decimal" ? "number" : "text";
  // 有預設值的欄位(例如單睪/賀尼亞頭數預設 0,使用者決定)直接把值畫
  // 進輸入框 —— 使用者不改就是這個數字,不是留白等著被當成沒填。
  return `
    <label class="fld"><span>${escapeHtml(field.label)}</span>
      <input type="${type}" id="f_${field.key}${sfx}"
             ${field.default !== undefined ? `value="${escapeHtml(String(field.default))}"` : ""}
             ${field.type === "decimal" ? 'step="0.1"' : ""}
             ${field.type === "int" ? 'inputmode="numeric"' : ""}></label>${hint}`;
}

function todayIso() {
  // 本地時區的今天。toISOString() 會先轉 UTC,台灣的凌晨 8 點前會退成昨天
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** 從表單讀出使用者填的值。分段按鈕與 chip 的值存在 .is-active 上。
 *
 * choice 欄位選到「其他」時,真正要存的不是「其他」這兩個字,是打字框
 * 裡使用者說明的實際原因 —— 沒打字就當沒填,讓 buildDetail() 既有的
 * 必填檢查去擋(「請填寫原因」),不必另外寫一套錯誤訊息。
 */
function readRecordFields(spec, sfx = "") {
  const raw = {};
  for (const field of spec.fields) {
    // perService 的欄位不在共用區,而是每一列配種各一個 ——
    // 由 readServiceRows() 讀,這裡讀不到也不該讀。
    if (field.perService) continue;
    if (field.type === "checkbox") {
      // 原生勾選框讀 .checked,不是 .is-active —— 沒勾就是預設的
      // false,不是「還沒填」。
      raw[field.key] = $(`f_${field.key}${sfx}`)?.checked ?? false;
    } else if (["bool", "score", "choice", "tri"].includes(field.type)) {
      const picked = document.querySelector(
        `[data-field="${field.key}${sfx}"] .is-active`);
      let val = picked ? picked.dataset.val : "";
      if (field.type === "choice" && val === OTHER_REASON) {
        val = ($(`f_${field.key}_other${sfx}`)?.value || "").trim();
      }
      raw[field.key] = val;
    } else {
      raw[field.key] = $(`f_${field.key}${sfx}`)?.value ?? "";
    }
  }
  return raw;
}

function showRecordError(message) {
  const box = $("recErr");
  box.textContent = message;
  box.classList.remove("is-hidden");
}

async function submitRecord() {
  const spec = formFor(recordCode);
  if (!spec) return;

  // 配種是「一次發情連配好幾天」,日期與公豬在各自的配種列上,沒有
  // 整張表單共用的 recDate。其餘事件維持單一日期。
  const services = supportsMultiService(recordCode) ? readServiceRows() : null;
  if (services) {
    if (!services.length) return showRecordError("請至少填一次配種的日期");
    const dates = services.map((s) => s.date);
    if (new Set(dates).size !== dates.length) {
      // 同一頭母豬同一天同一隻公豬會被判成重複而合併成一筆,使用者
      // 會以為記了兩次。與其讓它靜靜消失,不如當場講清楚。
      return showRecordError("有兩次配種填了同一天,請確認日期");
    }
  }
  const when = services ? services.map((s) => s.date).sort()[0] : $("recDate")?.value;
  if (!when) return showRecordError("請選擇日期");

  // 一頭一列。種豬進場要先判斷 —— 它也是一頭一列,但走的是「建立新的豬」
  // 那條路(送去 /api/sows),不是在既有母豬身上記一筆。順序反過來的話
  // 進場會被當成一般事件,去查一頭還不存在的母豬而報「找不到耳號」。
  if (usesPerSowRows(recordCode)) {
    return createsNewAnimal(recordCode)
      ? submitNewAnimalRows(spec, when)
      : submitPerSowRows(spec, when);
  }

  const raw = readRecordFields(spec);
  const { detail, problems } = buildDetail(recordCode, raw);
  if (problems.length) return showRecordError(problems[0]);

  // 每一次配種各自組一份 detail —— 公豬是逐次的(每天可能換不同隻),
  // 但仍然走 buildDetail 驗證,不是繞過它直接塞進去。
  let serviceEvents = null;
  if (services) {
    serviceEvents = [];
    for (const s of services) {
      const built = buildDetail(recordCode, { ...raw, boar_tag: s.boarTag });
      if (built.problems.length) return showRecordError(built.problems[0]);
      serviceEvents.push({ date: s.date, detail: built.detail });
    }
  }

  if (createsNewAnimal(recordCode)) {
    const kind = document.querySelector("#recKind .is-active")?.dataset.kind || "sow";
    const path = kind === "boar" ? "/api/boars" : "/api/sows";
    const { ok, data } = await api(path, postJson({
      earTag: detail.earTag, breed: detail.breed,
      birthDate: detail.birthDate, entryDate: when,
      sireTag: detail.sire_tag, damTag: detail.dam_tag,
    }));
    if (!ok) return showRecordError(data.error || "記錄失敗");
    closeRecordForm();
    showBanner(`${detail.earTag} 已進場`, "ok");
    await Promise.all([reloadSows(), reloadBoars(), reloadRecent()]);
    return;
  }

  // 肉豬死亡不掛在任何一頭豬身上 —— 沒有耳號可選,也不影響母豬/公豬
  // 清單、工作清單或提醒,所以送出後只需要重讀「已記錄」。
  if (targetsNothing(recordCode)) {
    const { ok, data } = await api("/api/market-deaths", postJson({
      date: when, reason: detail.reason, weightKg: detail.weight_kg,
    }));
    if (!ok) return showRecordError(data.error || "記錄失敗");
    closeRecordForm();
    showBanner(`${spec.label}已記錄`, "ok");
    await reloadRecent();
    return;
  }

  // 種豬死亡(target: "either")記在母豬還是公豬,由記錄當下的切換鈕
  // 決定;採精(target: "boar")固定是公豬。兩者都走公豬事件的路徑。
  const isBoarTarget = targetsBoar(recordCode) || (targetsEither(recordCode)
    && document.querySelector("#recKind .is-active")?.dataset.kind === "boar");

  if (isBoarTarget) {
    const boarTag = ($("recBoar")?.value || "").trim();
    const boar = boars.find((b) => b.earTag === boarTag);
    if (!boar) return showRecordError(boarTag ? `找不到耳號 ${boarTag}` : "請選擇公豬");

    const { ok, data } = await api("/api/boar-events", postJson({
      boarId: boar.id, type: recordCode, date: when, detail,
    }));
    if (!ok) return showRecordError(data.error || "記錄失敗");

    closeRecordForm();
    showBanner(`${boarTag} ${spec.label}已記錄`, "ok");
    // 種豬死亡會改變狀態跟耳號(民國年後綴),公豬清單要跟著重讀 ——
    // 採精沒有這個連帶效果,但重讀一次不影響正確性,程式碼簡單很多。
    await Promise.all([reloadBoars(), reloadRecent()]);
    if (openBoarId === boar.id) await openBoar(boar.id);
    return;
  }

  if (supportsMultiSow(recordCode)) {
    return submitMultiSowRecord(spec, when, detail, serviceEvents);
  }

  // 不明母豬:配種以外的事件沒有配種日期可用,耳號取事件當天(使用者決定)。
  const tag = recUnknownSow ? "不明母豬" : ($("recSow")?.value || "").trim();
  const sow = recUnknownSow ? null : sows.find((s) => s.earTag === tag);
  if (!recUnknownSow && !sow) {
    return showRecordError(tag ? `找不到耳號 ${tag}` : "請選擇母豬");
  }

  const { ok, data } = await api("/api/sow-events", postJson({
    ...(recUnknownSow ? { unknownTag: unknownTagFor(when) } : { sowId: sow.id }),
    type: recordCode, date: when, detail,
  }));
  if (!ok) return showRecordError(data.error || "記錄失敗");

  closeRecordForm();
  showBanner(`${tag} ${spec.label}已記錄`, "ok");
  // 記錄會改變狀態(胎次、產房、耳號),所以整批重讀而不是只補一列。
  await Promise.all([
    reloadSows(), reloadRecent(), reloadTasks(), reloadAlerts(),
  ]);
  // 剛記的這頭若正好是開著的那張卡,一併重新整理 —— 否則死亡/淘汰後
  // 耳號的民國年後綴、狀態、生產表現都會停在記錄之前的樣子。
  // 不明母豬時 sow 是 null(那頭豬是伺服器現建的,前端沒有她的 id)。
  if (sow && openSowId === sow.id) await openSow(sow.id);
}

/** 種豬進場一次建好幾頭。品種與進場日期整批共用,其餘每頭一列。
 *
 * 跟一般事件不同的地方:這裡是**建立新的豬**,不是在既有的豬身上記一筆,
 * 所以送去 /api/sows 或 /api/boars,而且耳號重複會被伺服器擋下來 ——
 * 那正是想要的行為,不必在前端再擋一次。
 */
async function submitNewAnimalRows(spec, when) {
  const rows = readSowRows(spec);
  if (!rows.length) return showRecordError("請至少填一頭的耳號");

  const dupes = rows.map((r) => r.tag).filter((t, i, a) => a.indexOf(t) !== i);
  if (dupes.length) return showRecordError(`${dupes[0]} 填了兩次`);

  const kind = document.querySelector("#recKind .is-active")?.dataset.kind || "sow";
  const path = kind === "boar" ? "/api/boars" : "/api/sows";
  const shared = readRecordFields(spec);      // 共用欄位(品種)

  // 只把「列裡真的有的欄位」蓋到共用值上。整份 row.raw 直接展開的話,
  // 列裡不存在的共用欄位(品種)會被讀成空字串而把共用值蓋掉 —— 實測
  // 三頭都建成功但品種全空,就是這個。
  const rowKeys = spec.fields.filter((f) => !f.shared).map((f) => f.key);
  const jobs = [];
  for (const row of rows) {
    const merged = { ...shared };
    for (const k of rowKeys) merged[k] = row.raw[k];
    const { detail, problems } = buildDetail(recordCode, merged);
    if (problems.length) return showRecordError(`${row.tag}:${problems[0]}`);
    jobs.push({ tag: row.tag, detail });
  }

  const results = await Promise.all(jobs.map(async (job) => {
    const { ok, data } = await api(path, postJson({
      earTag: job.detail.earTag, breed: job.detail.breed,
      birthDate: job.detail.birthDate, entryDate: when,
      sireTag: job.detail.sire_tag, damTag: job.detail.dam_tag,
    }));
    return { tag: job.tag, ok, error: data?.error };
  }));

  const failed = results.filter((r) => !r.ok);
  const done = results.length - failed.length;
  if (done) await Promise.all([reloadSows(), reloadBoars(), reloadRecent()]);
  if (failed.length) {
    const prefix = done ? `已建立 ${done} 頭,` : "";
    return showRecordError(
      `${prefix}${failed.length} 頭失敗:` +
      failed.map((f) => `${f.tag}(${f.error || "建立失敗"})`).join("、"));
  }

  closeRecordForm();
  showBanner(`已進場 ${done} 頭`, "ok");
}

/** 一頭一列的送出。每列各自驗證、各自送一筆,**互相獨立** —— 跟配種
 * 一次記多頭同一個道理:一列填錯不該讓已經填好的其他幾頭整批重打。
 *
 * 驗證在送出**之前**全部做完:數字打錯時當場指出是第幾頭,而不是先寫了
 * 三筆進資料庫才在第四筆報錯,留下一個做到一半的狀態。
 */
async function submitPerSowRows(spec, when) {
  const rows = readSowRows(spec);
  const noTag = spec.target === "none";
  if (!rows.length) {
    return showRecordError(noTag ? "請至少填一頭" : "請至少填一頭的耳號");
  }

  if (!noTag) {
    const dupes = rows.map((r) => r.tag).filter((t, i, a) => a.indexOf(t) !== i);
    if (dupes.length) return showRecordError(`${dupes[0]} 填了兩次`);
  }

  // 種豬死亡記在母豬還是公豬,由整批共用的切換鈕決定;採精固定公豬。
  const onBoar = targetsBoar(recordCode) || (targetsEither(recordCode)
    && document.querySelector("#recKind .is-active")?.dataset.kind === "boar");
  const herd = onBoar ? boars : sows;
  const shared = spec.fields.some((f) => f.shared) ? readRecordFields(spec) : {};
  const rowKeys = spec.fields.filter((f) => !f.shared).map((f) => f.key);

  const jobs = [];
  for (const [i, row] of rows.entries()) {
    let animal = null;
    if (!noTag) {
      animal = herd.find((a) => a.earTag === row.tag);
      if (!animal) return showRecordError(`第 ${i + 1} 頭:找不到耳號 ${row.tag}`);
    }
    // 共用欄位(移欄的區域)墊底,列裡真的有的欄位蓋上去 —— 整份 raw
    // 直接展開會讓列裡不存在的共用欄位被讀成空字串而蓋掉共用值。
    const merged = { ...shared };
    for (const k of rowKeys) merged[k] = row.raw[k];
    const { detail, problems } = buildDetail(recordCode, merged);
    if (problems.length) {
      return showRecordError(`${row.tag || `第 ${i + 1} 頭`}:${problems[0]}`);
    }
    jobs.push({ tag: row.tag || `第 ${i + 1} 頭`, id: animal?.id, detail });
  }

  const results = await Promise.all(jobs.map(async (job) => {
    const body = noTag
      ? { date: when, reason: job.detail.reason, weightKg: job.detail.weight_kg }
      : onBoar
        ? { boarId: job.id, type: recordCode, date: when, detail: job.detail }
        : { sowId: job.id, type: recordCode, date: when, detail: job.detail };
    const path = noTag ? "/api/market-deaths"
      : onBoar ? "/api/boar-events" : "/api/sow-events";
    const { ok, data } = await api(path, postJson(body));
    return { tag: job.tag, ok, error: data?.error };
  }));

  const failed = results.filter((r) => !r.ok);
  const done = results.length - failed.length;

  if (done) {
    await Promise.all([reloadSows(), reloadBoars(), reloadRecent(),
                       reloadTasks(), reloadAlerts()]);
  }
  if (failed.length) {
    const prefix = done ? `已記錄 ${done} 頭,` : "";
    return showRecordError(
      `${prefix}${failed.length} 頭失敗:` +
      failed.map((f) => `${f.tag}(${f.error || "記錄失敗"})`).join("、"));
  }

  closeRecordForm();
  showBanner(`已記錄 ${done} 頭${spec.label}`, "ok");
}

/** 配種一次記多頭的送出邏輯。每一頭各自送一筆 /api/sow-events(互相
 * 獨立,一筆失敗不影響其他筆)。**不是全部成功才關表單** —— 成功的
 * 先從清單移除、真的寫進去了,只把失敗的留著讓使用者修正重送,不然
 * 一筆打錯字就要整批重打一次,而且會讓人誤以為「這批一筆都沒記到」。
 */
async function submitMultiSowRecord(spec, when, detail, services) {
  // 打了字但忘記按 Enter/+ 的話,視為要加入,不要悄悄漏掉這一筆。
  addSowTag();
  if (recSowTags.length === 0 && !recUnknownSow) {
    return showRecordError("請至少加入一頭母豬耳號");
  }

  // 配種:每頭母豬 × 每一次配種各一筆(兩頭配兩天 = 4 筆),每一次各有
  // 自己的日期與公豬。其餘事件沒有 services,維持一頭一筆。
  const times = services && services.length ? services : [{ date: when, detail }];

  // 不明母豬:耳號用第一次配種的日期(跟預產期同一天),所以連配二三天
  // 會落在同一頭身上,而不是每天各建一頭。
  const first = times.map((t) => t.date).sort()[0];
  const targets = recSowTags.map((tag) => ({ tag, unknown: false }));
  if (recUnknownSow) targets.push({ tag: "不明母豬", unknown: true });

  const results = await Promise.all(targets.map(async ({ tag, unknown }) => {
    const sow = unknown ? null : sows.find((s) => s.earTag === tag);
    if (!unknown && !sow) return { tag, ok: false, error: "耳號已經不存在" };

    // 同一頭母豬的幾次配種**依序**送,不併行 —— 併行的話伺服器端幾筆
    // 同時寫入會各自去清同一個牧場的事件快取,徒增競爭;而且哪一筆先
    // 寫進去也影響不了結果,沒必要搶。
    for (const t of times) {
      const { ok, data } = await api("/api/sow-events", postJson({
        ...(unknown ? { unknownTag: unknownTagFor(first) } : { sowId: sow.id }),
        type: recordCode, date: t.date, detail: t.detail,
      }));
      if (!ok) return { tag, ok: false, error: data?.error };
    }
    return { tag, ok: true };
  }));

  const failed = results.filter((r) => !r.ok);
  const succeededTags = results.filter((r) => r.ok).map((r) => r.tag);

  recSowTags = recSowTags.filter((tag) => !succeededTags.includes(tag));
  renderSowChips();

  if (succeededTags.length) {
    // 記錄會改變狀態(懷孕待驗等),所以整批重讀而不是只補一列。
    await Promise.all([reloadSows(), reloadRecent(), reloadTasks(), reloadAlerts()]);
  }

  if (failed.length) {
    const prefix = succeededTags.length ? `已記錄 ${succeededTags.length} 頭,` : "";
    return showRecordError(
      `${prefix}${failed.length} 頭失敗:` +
      failed.map((f) => `${f.tag}(${f.error || "記錄失敗"})`).join("、")
    );
  }

  closeRecordForm();
  showBanner(`已記錄 ${succeededTags.length} 頭${spec.label}`, "ok");
}

async function reloadRecent() {
  const box = $("recDone");
  if (!box || !account.loggedIn) return;
  const { ok, data } = await api("/api/recent-events?days=7");
  if (!ok) return;

  recentEvents = data.events;
  $("recDoneCount").textContent = recentEvents.length
    ? `最近 7 天 ${recentEvents.length} 筆` : "";
  renderRecentList();
  renderPendingIdentity();
}

/** 「已記錄」清單。**預設只畫前 10 筆**(使用者要求)—— 一天記三四十筆
 * 是常態,整批攤開會把紀錄頁的表單推到看不到的地方,而使用者來這一區
 * 通常只是要確認「剛剛那筆記進去了沒」,那一定在最前面。
 *
 * 這裡是重畫而不是用 CSS 遮住多的部分(工作清單的 .tags-fold 那種做法)——
 * 那邊每一格高度一樣,固定 max-height 剛好切在整行;這裡每一列高度不一,
 * 補登的列還會多一行日期,固定高度一定會切在某一列中間。
 */
function renderRecentList() {
  const box = $("recDone");
  if (!box) return;
  if (!recentEvents.length) {
    box.innerHTML = '<p class="hint">最近 7 天還沒有記錄。</p>';
    return;
  }

  const shown = recentExpanded
    ? recentEvents : recentEvents.slice(0, RECENT_COLLAPSED);
  const rest = recentEvents.length - shown.length;

  box.innerHTML = shown.map(recordedRow).join("")
    + (recentEvents.length > RECENT_COLLAPSED
      ? `<button class="foldbtn" id="recDoneFold">${
          recentExpanded ? "收合 ⌃" : `展開其餘 ${rest} 筆 ›`}</button>`
      : "");
}

/** 待確認身分:耳號看不清楚時用日期先記著的母豬。
 *
 * 資料來自已經載入的 sows,不另外打 API —— 這份清單跟母豬清單是同一份
 * 資料的不同切法,多打一次只是讓開頁面時的平行請求再多一支。
 *
 * 只有牧場主看得到:認回耳號是在合併兩頭豬的資料,弄錯了要一筆一筆拆
 * 回來(伺服器端另外把關,這裡只是不給按)。
 */
function renderPendingIdentity() {
  const card = $("recPendingCard");
  const box = $("recPending");
  if (!card || !box) return;

  const pending = account.isOwner ? sows.filter((s) => s.isUnknown) : [];
  card.classList.toggle("is-hidden", pending.length === 0);
  if (!pending.length) return;

  $("recPendingCount").textContent = `${pending.length} 頭`;
  // 不沿用 .done-row —— 那是 flex 的一列,這裡要的是「名稱在上、輸入在下」
  // 的兩行版面,覆寫它的 display 會讓內層的 flex 設定失效而把文字擠成
  // 一行一個字(實際發生過)。給它自己的 class 乾淨得多。
  box.innerHTML = pending.map((s) => `
    <div class="pend-row">
      <div class="pend-t">${escapeHtml(s.earTag)}</div>
      <div class="pend-s">記錄時耳號看不清楚</div>
      <div class="rec-tag-row">
        <input list="sowTags" class="pend-tag" inputmode="numeric"
               placeholder="真正的耳號" autocomplete="off">
        <button type="button" class="btn-soft pend-go"
                data-identify="${s.id}">確認</button>
      </div>
    </div>`).join("");
}

/** 把一頭「不明-MMDD」認回真正的耳號 —— 她的記錄會併進那一頭。 */
async function identifyUnknownSow(sowId, row) {
  const tag = (row?.querySelector(".pend-tag")?.value || "").trim();
  if (!tag) return showBanner("請填寫真正的耳號", "warn");

  const { ok, data } = await api("/api/sows/identify", postJson({
    sowId, earTag: tag,
  }));
  if (!ok) return showBanner(data.error || "無法確認身分", "warn");

  showBanner(`已併回 ${tag}(${data.moved} 筆記錄)`, "ok");
  // 併完之後這頭豬不見了、那頭豬多了記錄,週期跟著變 —— 整批重讀。
  await Promise.all([reloadSows(), reloadRecent(), reloadTasks(), reloadAlerts()]);
}

async function undoRecord(eventId, kind, animalId) {
  // 種豬進場打錯耳號時整頭收回(這頭豬根本不該存在),跟收回一筆事件是
  // 不同的 API —— 走 /api/sows or /api/boars 本身,不是 sow-events/
  // boar-events(那頭豬還在,只是少一筆記錄)。
  const path = kind === "boar" ? `/api/boar-events/${eventId}`
    : kind === "sow" ? `/api/sow-events/${eventId}`
    : kind === "boar-entry" ? `/api/boars/${eventId}`
    : kind === "sow-entry" ? `/api/sows/${eventId}`
    : `/api/market-deaths/${eventId}`;               // "market-death"
  const { ok, data } = await api(path, { method: "DELETE" });
  if (!ok) return showBanner(data.error || "收不回來", "warn");

  if (kind === "market-death") {
    // 沒有耳號、沒有動物身分,不影響任何一張卡、工作或提醒。
    await reloadRecent();
    return;
  }

  if (kind === "sow-entry" || kind === "boar-entry") {
    // 這頭豬已經整個消失了,不會是「目前開著的那張卡」還存在的情況 ——
    // 不嘗試重開,只重讀清單。
    await Promise.all([reloadSows(), reloadBoars(), reloadRecent()]);
    return;
  }

  if (kind === "boar") {
    // 公豬事件(採精)沒有連帶效果,不必牽動工作/提醒/欄位。
    await reloadRecent();
    if (openBoarId === animalId) await openBoar(animalId);
    return;
  }

  await Promise.all([
    reloadSows(), reloadRecent(), reloadTasks(), reloadAlerts(),
  ]);
  // 收回的若是目前開著的那張卡的事件,同樣要重新整理 —— 理由跟
  // submitRecord() 那邊一樣。
  if (openSowId === animalId) await openSow(animalId);
}

// ── 值得檢視 ──

async function reloadReview() {
  const box = $("reviewList");
  if (!box || !account.loggedIn) return;

  const { ok, data, status } = await api("/api/review");
  if (!ok) {
    // 員工看不到這份名單(憲法第十一條),整張卡收起來而不是留一個錯誤訊息
    $("reviewCard")?.classList.toggle("is-hidden", status === 403);
    return;
  }

  $("reviewCaveat").textContent = data.caveat;
  $("reviewCount").textContent = `${data.sows.length} 頭`;
  box.innerHTML = data.sows.map(reviewRow).join("")
    || '<p class="hint">目前沒有需要特別看一眼的母豬。</p>';
}

/** 記錄檢查:生理上不可能、幾乎一定是打錯耳號的記錄。
 *
 * 沒問題時整張卡收起來 —— 一張永遠寫著「沒有問題」的卡,看久了就不會
 * 再看,真的出問題那天也一樣會被略過。
 */
async function reloadDataProblems() {
  const box = $("dataProblemList");
  const card = $("dataProblemCard");
  if (!box || !card || !account.loggedIn) return;

  const { ok, data, status } = await api("/api/data-problems");
  if (!ok) {
    // 員工看不到(要處理這些得刪改既有記錄,不是員工的權限)
    card.classList.add("is-hidden");
    return;
  }

  card.classList.toggle("is-hidden", data.problems.length === 0);
  if (!data.problems.length) {
    box.innerHTML = "";           // 修好之後不要把舊的那幾列留在 DOM 裡
    $("dpFixForm")?.classList.add("is-hidden");
    return;
  }

  $("dataProblemCount").textContent = data.total > data.problems.length
    ? `${data.problems.length} / ${data.total} 筆` : `${data.total} 筆`;
  box.innerHTML = data.problems.map((p) => `
    <div class="dp-row" data-problem="${p.eventId}" data-sow="${p.sowId}"
         data-type="${escapeHtml(p.type)}" data-date="${escapeHtml(p.date)}">
      <div class="dp-t">${escapeHtml(p.earTag)}</div>
      <div class="dp-w">${escapeHtml(p.why)}</div>
      <div class="dp-detail is-hidden">${escapeHtml(JSON.stringify(p.detail))}</div>
      <div class="dp-acts">
        <button type="button" class="btn-ghost dp-fix">修正這筆</button>
        <button type="button" class="btn-ghost dp-open">看她的記錄</button>
      </div>
    </div>`).join("");
}

/** 修正一筆異常記錄。日期與內容都能改 —— 兩種錯法都常見:離乳日期打錯
 * (哺乳變成 90 天),或數字打錯(單窩 56 隻,其實是 5 或 6)。
 *
 * 改的是**同一筆**,不是刪掉重記:重記會換一個新的 id,母豬卡上的位置
 * 與收回按鈕都會跳掉,而且原本是誰記的也會跟著消失。
 */
function openFixForm(row) {
  const code = row.dataset.type;
  const spec = formFor(code);
  const box = $("dpFixForm");
  if (!spec || !box) return;

  fixingEventId = Number(row.dataset.problem);
  box.classList.remove("is-hidden");
  box.innerHTML = `
    <div class="rec-head">
      <h3>修正:${escapeHtml(row.querySelector(".dp-t").textContent)} ${
        escapeHtml(spec.label)}</h3>
      <button type="button" class="btn-ghost" id="dpFixCancel">取消</button>
    </div>
    <p class="hint">${escapeHtml(row.querySelector(".dp-w").textContent)}</p>
    <label class="fld"><span>日期</span>
      <input type="date" id="dpFixDate" value="${escapeHtml(row.dataset.date)}"></label>
    ${spec.fields.filter((f) => !f.shared && !f.perService)
       .map((f) => fieldMarkup(f, "_fix")).join("")}
    <p class="rec-err is-hidden" id="dpFixErr"></p>
    <button type="button" class="btn-primary" id="dpFixSubmit">儲存修正</button>`;

  // 表單畫好之後才把現有的值填回去 —— fieldMarkup 不接受預設值。
  let current = {};
  try { current = JSON.parse(row.querySelector(".dp-detail").textContent); }
  catch { current = {}; }
  fillRecordFields(spec, "_fix", current);
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/** 把既有的值填回表單。修正一筆記錄時要看得到它現在長什麼樣子 ——
 * 給一張空表單等於逼使用者把沒要改的欄位重打一次,漏打的那幾格就會
 * 變成把原本有的資料清掉。
 */
function fillRecordFields(spec, sfx, detail) {
  for (const field of spec.fields) {
    const val = detail[field.key];
    if (val === undefined || val === null) continue;
    if (field.type === "checkbox") {
      const box = $(`f_${field.key}${sfx}`);
      if (box) box.checked = Boolean(val);
    } else if (["bool", "score", "choice", "tri"].includes(field.type)) {
      const group = document.querySelector(`[data-field="${field.key}${sfx}"]`);
      group?.querySelectorAll("[data-val]").forEach((b) =>
        b.classList.toggle("is-active", b.dataset.val === String(val)));
    } else {
      const input = $(`f_${field.key}${sfx}`);
      if (input) input.value = val;
    }
  }
}

async function submitFix() {
  const err = $("dpFixErr");
  const body = { eventId: fixingEventId };

  const when = $("dpFixDate")?.value;
  if (when) body.date = when;

  // 表單已經帶出這筆的現況,所以送的是**完整的一筆**,驗證跟新增走
  // 同一套。不必去分辨「哪幾格被改過」—— 那種判斷遇到有預設值的欄位
  // 就會出錯(單睪/賀尼亞頭數預設 0,看起來永遠像是被填過的)。
  const spec = formFor(fixingType);
  if (!spec) return;
  const built = buildDetail(fixingType, readRecordFields(spec, "_fix"));
  if (built.problems.length) {
    err.textContent = built.problems[0];
    return err.classList.remove("is-hidden");
  }
  body.detail = built.detail;

  const { ok, data } = await api("/api/sow-events/fix", postJson(body));
  if (!ok) {
    err.textContent = data.error || "修正失敗";
    return err.classList.remove("is-hidden");
  }

  $("dpFixForm").classList.add("is-hidden");
  showBanner("已修正", "ok");
  await Promise.all([reloadDataProblems(), reloadSows(), reloadRecent(),
                     reloadTasks(), reloadAlerts()]);
}

// ── 生產月報 ──
//
// null 代表「用伺服器判斷的當月」;一旦拿到回應就固定成該月字串,
// 之後靠 mrPrev/mrNext 平移 —— 不然月份導覽的起點每次都會被拉回今天。
let monthReportMonth = null;

async function reloadMonthReport() {
  const box = $("mrGrid");
  if (!box || !account.loggedIn) return;

  const { ok, data, status } = await api(
    `/api/monthly-report${monthReportMonth ? `?month=${monthReportMonth}` : ""}`);
  if (!ok) {
    // 員工看不到月報(憲法第十一條),整張卡收起來而不是留一個錯誤訊息
    $("monthReportCard")?.classList.toggle("is-hidden", status === 403);
    return;
  }

  monthReportMonth = data.start.slice(0, 7);
  $("mrLabel").textContent = formatMonth(data.start);
  $("mrHerdSize").textContent = data.herdSize
    ? `平均在場 ${data.herdSize.toFixed(1)} 頭` : "本月無在場母豬記錄";
  box.innerHTML = monthReportGrid(data.metrics);
  $("mrBasis").textContent = data.basis;
}

// 月份選擇器:點月份文字本身開起來,一次跳到目標月份 —— 不然要看半年前
// 的月報得靠 ‹ › 一格一格點,每點一次都要重新計算一次(即時算、不存
// 快照),效率很差。mrPickerYear 是選擇器目前顯示哪一年,跟已經載入的
// monthReportMonth 分開 —— 瀏覽年份不該連帶重新計算月報。
let mrPickerYear = null;

function renderMonthPicker() {
  $("mrPickerYearText").textContent = `${mrPickerYear} 年`;
  $("mrPickerGrid").innerHTML = monthPickerGrid(mrPickerYear, monthReportMonth);
}

function toggleMonthPicker() {
  const picker = $("mrPicker");
  if (!picker) return;
  if (!picker.classList.contains("is-hidden")) {
    picker.classList.add("is-hidden");
    return;
  }
  mrPickerYear = yearOfMonth(monthReportMonth) || new Date().getFullYear();
  renderMonthPicker();
  picker.classList.remove("is-hidden");
}

function closeMonthPicker() {
  $("mrPicker")?.classList.add("is-hidden");
}

// ── 設定 ──

async function reloadSettings() {
  const box = $("setFields");
  if (!box || !account.loggedIn) return;

  const { ok, data, status } = await api("/api/settings");
  if (!ok) {
    $("setCard")?.classList.toggle("is-hidden", status === 403);
    return;
  }
  settingFields = data.fields;
  box.innerHTML = data.fields
    .map((f) => settingRow(f, data.settings[f.key], data.defaults[f.key]))
    .join("");
}

async function saveSettings() {
  const payload = {};
  for (const field of settingFields) {
    const raw = $(`set_${field.key}`)?.value;
    if (raw !== undefined && raw !== "") payload[field.key] = Number(raw);
  }

  const { ok, data } = await api("/api/settings", postJson({ settings: payload }));
  if (!ok) return showBanner(data.error || "設定沒有存起來", "warn");

  showBanner("設定已儲存", "ok");
  // 參數變了,推算出來的東西全部要重算
  await Promise.all([
    reloadSettings(), reloadTasks(), reloadAlerts(), reloadReview(), reloadDataProblems(), reloadMonthReport(),
  ]);
}

let settingFields = [];

// ── 自訂工作(設定頁) ──

async function reloadCustomTaskSettings() {
  const card = $("customSetCard");
  if (!card || !account.loggedIn) return;
  // 跟其他設定一樣是牧場主的事 —— 員工在「工作」頁看得到、勾得動,
  // 但不能新增或刪除。
  card.classList.toggle("is-hidden", !account.isOwner);
  if (!account.isOwner) return;

  const { ok, data } = await api("/api/custom-tasks");
  if (!ok) return;
  $("customTaskList").innerHTML = data.tasks.map(customTaskSetting).join("")
    || '<p class="hint">還沒有自訂工作。</p>';
}

async function addCustomTask() {
  const name = ($("ctaskName")?.value || "").trim();
  const startDate = $("ctaskStart")?.value;
  const repeat = $("ctaskRepeat")?.value || "once";

  if (!name) return showBanner("請填寫工作名稱", "warn");
  if (!startDate) return showBanner("請選擇起始日期", "warn");

  const { ok, data } = await api("/api/custom-tasks",
                                 postJson({ name, startDate, repeat }));
  if (!ok) return showBanner(data.error || "新增失敗", "warn");

  $("ctaskName").value = "";
  showBanner(`已新增「${name}」`, "ok");
  await Promise.all([reloadCustomTaskSettings(), reloadTasks()]);
}

async function deleteCustomTask(taskId) {
  const { ok, data } = await api(`/api/custom-tasks/${taskId}`, { method: "DELETE" });
  if (!ok) return showBanner(data.error || "刪除失敗", "warn");
  await Promise.all([reloadCustomTaskSettings(), reloadTasks()]);
}

// 分段按鈕、chip、收回、記錄 —— 全部走事件委派,因為這些元素是動態畫的。
document.addEventListener("click", (e) => {
  const seg = e.target.closest(".seg-b");
  if (seg) {
    seg.parentElement.querySelectorAll(".seg-b")
      .forEach((b) => b.classList.toggle("is-active", b === seg));
    // 種豬死亡:換了母豬/公豬,耳號選單要跟著換 —— 種豬進場的同一顆
    // 切換鈕不需要這個,兩邊欄位本來就長得一樣,沒有 #recAnimalPicker。
    const picker = seg.closest("#recKind") && $("recAnimalPicker");
    if (picker) {
      picker.innerHTML = seg.dataset.kind === "boar" ? boarPickerField() : sowPickerField();
    }
    return;
  }
  // 配種一次記多頭的耳號 chip 借了同一套外觀(.chip),但語意不是
  // 「選一個」而是「刪掉這一個」,不能被這裡的單選切換攔截 ——
  // 用 data-remove-tag 排除,讓它落到下面 removeSowTag 那一段。
  const chip = e.target.closest(".chip:not([data-remove-tag])");
  if (chip) {
    chip.parentElement.querySelectorAll(".chip")
      .forEach((c) => c.classList.toggle("is-active", c === chip));
    // 選到「其他」才顯示打字框(見 fieldMarkup 的 choice 分支);換回別的
    // 固定選項就藏起來 —— 讀值時(readRecordFields)只有目前選到「其他」
    // 才會去看這個框,藏起來的舊字不會被誤用,純粹是畫面乾淨。
    const otherInput = chip.closest(".fld")?.querySelector(".fld-other");
    if (otherInput) {
      const isOther = chip.dataset.val === OTHER_REASON;
      otherInput.classList.toggle("is-hidden", !isOther);
      if (isOther) otherInput.focus();
    }
    return;
  }
  const rec = e.target.closest("[data-rec]");
  if (rec) return openRecordForm(rec.dataset.rec);

  const undo = e.target.closest("[data-undo]");
  if (undo) return undoRecord(Number(undo.dataset.undo), undo.dataset.kind,
                              Number(undo.dataset.animal));

  const delTask = e.target.closest("[data-del-task]");
  if (delTask) return deleteCustomTask(Number(delTask.dataset.delTask));

  if (e.target.id === "recCancel") return closeRecordForm();
  if (e.target.id === "recSubmit") return submitRecord();
  if (e.target.id === "recSowAdd") return addSowTag();
  if (e.target.id === "recAddService") return addServiceRow();
  if (e.target.id === "recAddSowRow") return addSowRow();
  if (e.target.id === "recDoneFold") {
    recentExpanded = !recentExpanded;
    return renderRecentList();
  }

  const delSowRow = e.target.closest(".rec-row-del");
  if (delSowRow) return removeSowRow(delSowRow.closest(".rec-row"));
  const identify = e.target.closest("[data-identify]");
  if (identify) {
    return identifyUnknownSow(Number(identify.dataset.identify),
                              identify.closest(".pend-row"));
  }
  if (e.target.id === "recUnknownBtn") {
    recUnknownSow = !recUnknownSow;
    return renderUnknownButton();
  }

  const delService = e.target.closest(".svc-del");
  if (delService) return removeServiceRow(delService.closest(".svc-row"));

  const removeTag = e.target.closest("[data-remove-tag]");
  if (removeTag) return removeSowTag(removeTag.dataset.removeTag);
  if (e.target.id === "setSave") return saveSettings();
  if (e.target.id === "ctaskAdd") return addCustomTask();
});

// 勾選框走 change 而不是 click —— click 在鍵盤操作與部分輔助技術下不會
// 觸發,那些使用者會勾得動卻存不進去。
document.addEventListener("change", (e) => {
  const box = e.target.closest(".ctask-box");
  if (box) {
    toggleCustomTask(Number(box.dataset.task), box.dataset.due, box.checked);
    box.closest(".ctask")?.classList.toggle("is-done", box.checked);
  }
});

// 真的把 App 跑起來。這行漏掉時畫面不會報錯,只是所有標籤停在「載入中…」,
// 看起來像伺服器沒回應 —— 有測試把關(test_app_bootstraps_itself)。
// 不必等 DOMContentLoaded:module 本來就是 defer,執行時 DOM 已經齊了。
init();
