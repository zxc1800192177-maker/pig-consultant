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
  alertRow, buildAlerts, eventName, eventRow, formatWeek, performanceGrid,
  pendingCheckRow, reviewRow, settingRow, shiftDate, sowRow, statusPills, taskGroup,
  timelineCaption, TIMELINE_LIMIT, visibleEvents,
} from "./lib/v2.js";
import {
  SIDE_EFFECTS, buildDetail, createsNewAnimal, formFor, recordedRow,
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
  // 登入/登出後全部重讀。少列一項的話,換帳號後那一區會留著上一個
  // 使用者的資料 —— 跨牧場的資料外洩就是這樣發生的。
  await Promise.all([
    reloadHistory(), reloadTasks(), reloadAlerts(), reloadSows(),
    reloadBoars(), reloadRecent(), reloadReview(), reloadSettings(),
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
      reloadBoars(), reloadRecent(), reloadReview(), reloadSettings(),
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
let sows = [];

async function reloadSows() {
  if (!$("sowList") || !account.loggedIn) return;
  const { ok, data } = await api("/api/sows");
  sows = ok ? data.sows : [];
  renderSowList();
}

function renderSowList() {
  const list = $("sowList");
  if (!list) return;

  const q = ($("sowSearch")?.value || "").trim().toLowerCase();
  const shown = q ? sows.filter((s) => s.earTag.toLowerCase().includes(q)) : sows;
  $("sowCount").textContent = q
    ? `符合 ${shown.length} 頭 / 在場 ${sows.length} 頭`
    : `在場 ${sows.length} 頭`;

  if (!shown.length) {
    list.innerHTML = `<p class="hint">${sows.length
      ? "沒有符合的耳號。" : "還沒有母豬資料,可以到「設定」匯入 PigCHAMP 檔案。"}</p>`;
    return;
  }
  // 只畫前 100 筆:451 頭全畫會讓搜尋時每次輸入都卡一下。
  list.innerHTML = shown.slice(0, 100).map(sowRow).join("")
    + (shown.length > 100 ? `<p class="hint">只顯示前 100 頭,請用搜尋縮小範圍。</p>` : "");
}

async function openSow(sowId) {
  const { ok, data } = await api(`/api/sows/${sowId}`);
  if (!ok) return showBanner(data.error || "讀不到這頭母豬", "warn");

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
      ${data.boarEvents && !data.boarEventsImported ? `<br><span class="hint">
        公豬會建起來(配種記錄要選),但採精與精液品質那
        ${data.boarEvents} 筆事件目前還不會匯入</span>` : ""}
    </div>
    ${data.oddBoarTags?.length ? `
      <div class="notice notice-warn" style="margin-top:12px">
        有 <b>${data.oddBoarTags.length}</b> 個公豬耳號看起來像日期,
        可能是原始檔案填錯欄位。<b>資料照樣匯入不會改動</b>,
        但它們會出現在配種記錄的公豬選單裡。
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
      data.excluded ? `,其中 ${data.excluded} 筆不納入統計` : ""}。</div>`;
  importText = "";
  await Promise.all([reloadSows(), reloadTasks(), reloadAlerts()]);
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
  const row = e.target.closest(".sow-row");
  if (row) return openSow(Number(row.dataset.sow));

  const tag = e.target.closest(".etag");
  if (tag) { showTab("sows"); return openSow(Number(tag.dataset.sow)); }

  if (e.target.id === "importConfirm") return commitImport();
  if (e.target.id === "weekPrev") { weekStart = shiftDate(weekStart, -7); return reloadTasks(); }
  if (e.target.id === "weekNext") { weekStart = shiftDate(weekStart, 7); return reloadTasks(); }
});

$("sowSearch")?.addEventListener("input", renderSowList);
$("importPick")?.addEventListener("click", () => $("importFile")?.click());
$("importFile")?.addEventListener("change", (e) => {
  const file = e.target.files?.[0];
  e.target.value = "";      // 清空才能連續選同一個檔案
  if (file) previewImport(file);
});

// ── 紀錄頁 ──

let boars = [];
let recordCode = null;      // 目前開著的表單是哪一種事件

async function reloadBoars() {
  // 未登入就別發這個請求。少了這道守衛,登入畫面上會固定丟一個 401 到
  // console —— 假錯誤最麻煩的地方是它會蓋掉真的錯誤,這次就是這樣多花了
  // 很多時間在追一個早就不存在的變數。
  if (!account.loggedIn) { boars = []; return; }
  const { ok, data } = await api("/api/boars");
  boars = ok ? data.boars : [];
}

function closeRecordForm() {
  recordCode = null;
  $("recForm").classList.add("is-hidden");
  $("recForm").innerHTML = "";
}

function openRecordForm(code) {
  const spec = formFor(code);
  if (!spec) return;
  recordCode = code;

  const box = $("recForm");
  box.classList.remove("is-hidden");
  box.innerHTML = `
    <div class="rec-head">
      <h3>${escapeHtml(spec.label)}</h3>
      <button type="button" class="btn-ghost" id="recCancel">取消</button>
    </div>
    ${SIDE_EFFECTS[code]
      ? `<p class="rec-warn-note">送出後:${escapeHtml(SIDE_EFFECTS[code])}</p>` : ""}
    ${createsNewAnimal(code) ? newAnimalFields() : sowPickerField()}
    <label class="fld"><span>日期</span>
      <input type="date" id="recDate" value="${todayIso()}"></label>
    ${spec.fields.map(fieldMarkup).join("")}
    <p class="rec-err is-hidden" id="recErr"></p>
    <button type="button" class="btn-primary" id="recSubmit">記錄</button>`;

  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
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
    </datalist>`;
}

function newAnimalFields() {
  return `
    <div class="seg" id="recKind">
      <button type="button" class="seg-b is-active" data-kind="sow">母豬</button>
      <button type="button" class="seg-b" data-kind="boar">公豬</button>
    </div>`;
}

function fieldMarkup(field) {
  const hint = field.hint ? `<em class="fld-h">${escapeHtml(field.hint)}</em>` : "";

  if (field.type === "boar") {
    return `
      <label class="fld"><span>${escapeHtml(field.label)}</span>
        <input list="boarTags" id="f_${field.key}" placeholder="輸入或選擇耳號"
               autocomplete="off"></label>
      <datalist id="boarTags">
        ${boars.map((b) => `<option value="${escapeHtml(b.earTag)}"></option>`).join("")}
      </datalist>`;
  }
  if (field.type === "bool") {
    return `
      <div class="fld"><span>${escapeHtml(field.label)}</span>
        <div class="seg" data-field="${field.key}">
          <button type="button" class="seg-b" data-val="true">${escapeHtml(field.yes)}</button>
          <button type="button" class="seg-b" data-val="false">${escapeHtml(field.no)}</button>
        </div>
      </div>`;
  }
  if (field.type === "score") {
    // 1~5 的按鈕而不是輸入框:巡欄時單手操作,而且按鈕本身就說明了範圍
    return `
      <div class="fld"><span>${escapeHtml(field.label)}</span>
        <div class="seg score" data-field="${field.key}">
          ${[1, 2, 3, 4, 5].map((n) =>
            `<button type="button" class="seg-b" data-val="${n}">${n}</button>`).join("")}
        </div>${hint}
      </div>`;
  }
  if (field.type === "choice") {
    return `
      <div class="fld"><span>${escapeHtml(field.label)}</span>
        <div class="chips" data-field="${field.key}">
          ${field.options.map((o) =>
            `<button type="button" class="chip" data-val="${escapeHtml(o)}"
             >${escapeHtml(o)}</button>`).join("")}
        </div>${hint}
      </div>`;
  }
  const type = field.type === "date" ? "date"
             : field.type === "int" ? "number" : "text";
  return `
    <label class="fld"><span>${escapeHtml(field.label)}</span>
      <input type="${type}" id="f_${field.key}"
             ${field.type === "int" ? 'inputmode="numeric"' : ""}></label>${hint}`;
}

function todayIso() {
  // 本地時區的今天。toISOString() 會先轉 UTC,台灣的凌晨 8 點前會退成昨天
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** 從表單讀出使用者填的值。分段按鈕與 chip 的值存在 .is-active 上。 */
function readRecordFields(spec) {
  const raw = {};
  for (const field of spec.fields) {
    if (["bool", "score", "choice"].includes(field.type)) {
      const picked = document.querySelector(
        `[data-field="${field.key}"] .is-active`);
      raw[field.key] = picked ? picked.dataset.val : "";
    } else {
      raw[field.key] = $(`f_${field.key}`)?.value ?? "";
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

  const when = $("recDate")?.value;
  if (!when) return showRecordError("請選擇日期");

  const raw = readRecordFields(spec);
  const { detail, problems } = buildDetail(recordCode, raw);
  if (problems.length) return showRecordError(problems[0]);

  if (createsNewAnimal(recordCode)) {
    const kind = document.querySelector("#recKind .is-active")?.dataset.kind || "sow";
    const path = kind === "boar" ? "/api/boars" : "/api/sows";
    const { ok, data } = await api(path, postJson({
      earTag: detail.earTag, breed: detail.breed,
      birthDate: detail.birthDate, entryDate: when,
    }));
    if (!ok) return showRecordError(data.error || "記錄失敗");
    closeRecordForm();
    showBanner(`${detail.earTag} 已進場`, "ok");
    await Promise.all([reloadSows(), reloadBoars(), reloadRecent()]);
    return;
  }

  const tag = ($("recSow")?.value || "").trim();
  const sow = sows.find((s) => s.earTag === tag);
  if (!sow) return showRecordError(tag ? `找不到耳號 ${tag}` : "請選擇母豬");

  const { ok, data } = await api("/api/sow-events", postJson({
    sowId: sow.id, type: recordCode, date: when, detail,
  }));
  if (!ok) return showRecordError(data.error || "記錄失敗");

  closeRecordForm();
  showBanner(`${tag} ${spec.label}已記錄`, "ok");
  // 記錄會改變狀態(胎次、產房、耳號),所以整批重讀而不是只補一列
  await Promise.all([reloadSows(), reloadRecent(), reloadTasks(), reloadAlerts()]);
}

async function reloadRecent() {
  const box = $("recDone");
  if (!box || !account.loggedIn) return;
  const { ok, data } = await api("/api/recent-events?days=7");
  if (!ok) return;

  $("recDoneCount").textContent = data.events.length
    ? `最近 7 天 ${data.events.length} 筆` : "";
  box.innerHTML = data.events.map(recordedRow).join("")
    || '<p class="hint">最近 7 天還沒有記錄。</p>';
}

async function undoRecord(eventId) {
  const { ok, data } = await api(`/api/sow-events/${eventId}`, { method: "DELETE" });
  if (!ok) return showBanner(data.error || "收不回來", "warn");
  await Promise.all([reloadSows(), reloadRecent(), reloadTasks(), reloadAlerts()]);
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
  await Promise.all([reloadSettings(), reloadTasks(), reloadAlerts(), reloadReview()]);
}

let settingFields = [];

// 分段按鈕、chip、收回、記錄 —— 全部走事件委派,因為這些元素是動態畫的。
document.addEventListener("click", (e) => {
  const seg = e.target.closest(".seg-b");
  if (seg) {
    seg.parentElement.querySelectorAll(".seg-b")
      .forEach((b) => b.classList.toggle("is-active", b === seg));
    return;
  }
  const chip = e.target.closest(".chip");
  if (chip) {
    chip.parentElement.querySelectorAll(".chip")
      .forEach((c) => c.classList.toggle("is-active", c === chip));
    return;
  }
  const rec = e.target.closest("[data-rec]");
  if (rec) return openRecordForm(rec.dataset.rec);

  const undo = e.target.closest("[data-undo]");
  if (undo) return undoRecord(Number(undo.dataset.undo));

  if (e.target.id === "recCancel") return closeRecordForm();
  if (e.target.id === "recSubmit") return submitRecord();
  if (e.target.id === "setSave") return saveSettings();
});

// 真的把 App 跑起來。這行漏掉時畫面不會報錯,只是所有標籤停在「載入中…」,
// 看起來像伺服器沒回應 —— 有測試把關(test_app_bootstraps_itself)。
// 不必等 DOMContentLoaded:module 本來就是 defer,執行時 DOM 已經齊了。
init();
