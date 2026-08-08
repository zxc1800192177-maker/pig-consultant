// Markdown 渲染測試。
//
// AI 回傳 Markdown,前端要轉成 HTML。這一層最重要的是安全:
// 模型輸出可能含有 HTML 標籤,直接塞進 innerHTML 會被當成標籤執行。

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { renderMarkdown, trimDangling } from "../../web/lib/markdown.js";

describe("跳脫", () => {
  it("HTML 標籤不得被當成標籤執行", () => {
    const html = renderMarkdown("<script>alert(1)</script>");
    assert.ok(!html.includes("<script>"));
    assert.ok(html.includes("&lt;script&gt;"));
  });

  it("屬性中的引號也要跳脫", () => {
    const html = renderMarkdown('<img src="x" onerror="alert(1)">');
    assert.ok(!html.includes("onerror=\"alert"));
  });

  it("跳脫發生在轉換之前,不會破壞正常內容", () => {
    assert.ok(renderMarkdown("**粗體**").includes("<strong>粗體</strong>"));
  });
});

describe("標題", () => {
  it("# 轉成 h1", () => {
    assert.ok(renderMarkdown("# 標題").includes("<h1>標題</h1>"));
  });

  it("## 轉成 h2", () => {
    assert.ok(renderMarkdown("## 標題").includes("<h2>標題</h2>"));
  });

  it("超過三層一律用 h3,避免字級失控", () => {
    assert.ok(renderMarkdown("##### 標題").includes("<h3>標題</h3>"));
  });
});

describe("強調", () => {
  it("**粗體**", () => {
    assert.ok(renderMarkdown("**藥品**").includes("<strong>藥品</strong>"));
  });

  it("*斜體*", () => {
    assert.ok(renderMarkdown("*Erysipelothrix*").includes("<em>Erysipelothrix</em>"));
  });

  it("`程式碼`", () => {
    assert.ok(renderMarkdown("`20 mg/kg`").includes("<code>20 mg/kg</code>"));
  });
});

describe("清單", () => {
  it("項目符號轉成 ul", () => {
    const html = renderMarkdown("- 甲\n- 乙");
    assert.ok(html.includes("<ul>"));
    assert.equal((html.match(/<li>/g) || []).length, 2);
  });

  it("數字清單轉成 ol", () => {
    assert.ok(renderMarkdown("1. 甲\n2. 乙").includes("<ol>"));
  });

  it("清單結束後要關閉標籤", () => {
    const html = renderMarkdown("- 甲\n\n段落");
    assert.ok(html.includes("</ul>"));
  });
});

describe("表格", () => {
  const table = [
    "| 藥物 | 用法 |",
    "|---|---|",
    "| Penicillin | 肌肉注射 |",
    "| Amoxicillin | 口服 |",
  ].join("\n");

  it("表頭進 th", () => {
    assert.ok(renderMarkdown(table).includes("<th>藥物</th>"));
  });

  it("資料列進 td", () => {
    assert.ok(renderMarkdown(table).includes("<td>Penicillin</td>"));
  });

  it("包在可橫向捲動的容器裡,避免整頁被撐開", () => {
    assert.ok(renderMarkdown(table).includes("table-scroll"));
  });

  it("沒有分隔線就不是表格", () => {
    const html = renderMarkdown("| 這不是 | 表格 |");
    assert.ok(!html.includes("<table>"));
  });
});

describe("串流途中的未閉合語法", () => {
  it("移除尾端未閉合的粗體符號", () => {
    assert.equal(trimDangling("風險評估 **休藥"), "風險評估 ");
  });

  it("移除尾端未閉合的行內程式碼", () => {
    assert.equal(trimDangling("劑量 `20 mg"), "劑量 ");
  });

  it("已閉合的語法不受影響", () => {
    assert.equal(trimDangling("**休藥期** 說明"), "**休藥期** 說明");
  });

  it("純文字不受影響", () => {
    assert.equal(trimDangling("一般文字"), "一般文字");
  });
});

describe("邊界", () => {
  it("空字串", () => {
    assert.equal(renderMarkdown(""), "");
  });

  it("null 不應丟例外", () => {
    assert.doesNotThrow(() => renderMarkdown(null));
  });
});
