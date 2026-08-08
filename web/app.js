// 畫面接線。純邏輯都在 lib/ 底下,那些有單元測試;這裡只做 DOM 操作。

import { renderMarkdown, trimDangling, escapeHtml } from "./lib/markdown.js";
import { formatShortfall, formatValue, gradeTone } from "./lib/format.js";
import { SseParser } from "./lib/sse.js";

const $ = (id) => document.getElementById(id);

let metricDefs = [];
let lastWeaknesses = [];   // 供疾病諮詢當背景資訊(US-1 驗收條件 7)

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
      showBanner(
        "AI 諮詢目前無法使用(CLI 尚未登入或額度用盡)。生產健檢不受影響,仍可正常使用。",
        "warn"
      );
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
      <tr>
        <td>${escapeHtml(g.name)}</td>
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
      body: JSON.stringify({ question, weaknesses: lastWeaknesses }),
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
    if (answer) $("consultBody").innerHTML = renderMarkdown(answer);
    setConsultBusy(false);
  }
}

function handleConsultEvent(event, getAnswer, setAnswer) {
  if (event.type === "meta") {
    // 通報提示是計算出來的,在 AI 開口之前就先呈現(憲法第一條)
    const parts = [];
    if (event.escalation) {
      parts.push(`<div class="notice notice-alert">${escapeHtml(event.escalation.notice)}</div>`);
    }
    parts.push(`<div class="notice notice-info">${escapeHtml(event.baselineNotice)}</div>`);
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
