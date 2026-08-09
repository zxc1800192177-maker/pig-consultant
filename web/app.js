// 畫面接線。純邏輯都在 lib/ 底下,那些有單元測試;這裡只做 DOM 操作。

import { renderMarkdown, trimDangling, escapeHtml } from "./lib/markdown.js";
import { formatShortfall, formatValue, gradeTone } from "./lib/format.js";
import { SseParser } from "./lib/sse.js";
import { isSpeechRecognitionSupported, mergeTranscript, splitFinalAndInterim } from "./lib/speech.js";
import { addDrug, loadMyDrugs, removeDrug, saveMyDrugs } from "./lib/drugs.js";

const $ = (id) => document.getElementById(id);

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

// ── 我的藥品庫 ──
// 跟對話歷史一樣只存在瀏覽器裡,不上傳保存;送出問題時才隨請求附上。
// 顧問會優先引用這裡的內容給劑量,不會自己生成數字(見 core/dosage.py)。
let myDrugs = loadMyDrugs(localStorage);

function renderDrugList() {
  const list = $("drugList");
  if (!myDrugs.length) {
    list.innerHTML = `<li class="drug-empty">還沒有加入任何藥品。</li>`;
    return;
  }
  list.innerHTML = myDrugs
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

$("addDrugBtn").addEventListener("click", () => {
  myDrugs = addDrug(myDrugs, {
    name: $("drugName").value,
    dosageNote: $("drugNote").value,
    withdrawalDays: readOptionalNumber($("drugWithdrawal")),
  });
  saveMyDrugs(localStorage, myDrugs);
  renderDrugList();
  $("drugName").value = "";
  $("drugNote").value = "";
  $("drugWithdrawal").value = "";
  $("drugName").focus();
});

$("drugList").addEventListener("click", (e) => {
  const btn = e.target.closest(".drug-remove");
  if (!btn) return;
  myDrugs = removeDrug(myDrugs, btn.dataset.id);
  saveMyDrugs(localStorage, myDrugs);
  renderDrugList();
});

renderDrugList();

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
  try {
    const health = await (await fetch("/api/health")).json();
    $("sourceLabel").textContent = health.source;
    if (!health.aiAvailable) {
      // 提示文字由後端提供(core/labels.py),前端不自己維護一份措辭
      showBanner(health.aiUnavailableNote, "warn");
      $("askBtn").disabled = true;
    }
  } catch {
    showBanner("無法連線到本機伺服器,請確認 server.py 正在執行。", "warn");
  }

  const meta = await (await fetch("/api/metrics")).json();
  metricDefs = meta.metrics;
  $("disclaimer").textContent = meta.disclaimer;
  renderMetricFields();
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
  const res = await fetch("/api/grade", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ values: collectValues() }),
  });
  const data = await res.json();

  if (!res.ok) {
    $("healthResult").innerHTML = `<div class="card"><div class="notice notice-warn">${data.errors
      .map((e) => escapeHtml(e.message))
      .join("<br>")}</div></div>`;
    return;
  }

  lastWeaknesses = data.weaknesses;
  renderHealthResult(data);
  requestAdvice(data.weaknesses);
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

  $("healthResult").innerHTML = `
    ${warnings}
    <div class="card">
      <div class="section-label tag-computed">計算結果 · ${escapeHtml(data.source)}</div>
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
const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;

if (isSpeechRecognitionSupported(window)) {
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
        myDrugs,
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
