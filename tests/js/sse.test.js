// 串流解析測試。
//
// 網路封包不會剛好切在事件邊界上,解析器必須能處理半截的資料。
// 這裡漏掉一段就等於使用者少看到一段回答。

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { SseParser } from "../../web/lib/sse.js";

describe("完整事件", () => {
  it("解析單一事件", () => {
    const p = new SseParser();
    const events = p.push('data: {"type":"delta","text":"你好"}\n\n');
    assert.deepEqual(events, [{ type: "delta", text: "你好" }]);
  });

  it("一次收到多個事件", () => {
    const p = new SseParser();
    const events = p.push(
      'data: {"type":"meta"}\n\ndata: {"type":"delta","text":"甲"}\n\n'
    );
    assert.equal(events.length, 2);
    assert.equal(events[1].text, "甲");
  });
});

describe("分段抵達", () => {
  it("事件被切成兩半也要正確組回", () => {
    const p = new SseParser();
    assert.deepEqual(p.push('data: {"type":"delta",'), []);
    const events = p.push('"text":"完整"}\n\n');
    assert.deepEqual(events, [{ type: "delta", text: "完整" }]);
  });

  it("逐字元餵入也不漏事件", () => {
    const raw = 'data: {"type":"delta","text":"逐字"}\n\n';
    const p = new SseParser();
    const collected = [];
    for (const ch of raw) collected.push(...p.push(ch));
    assert.deepEqual(collected, [{ type: "delta", text: "逐字" }]);
  });

  it("尚未收到分隔符時不吐出半截事件", () => {
    const p = new SseParser();
    assert.deepEqual(p.push('data: {"type":"delta","text":"未完"}'), []);
  });
});

describe("容錯", () => {
  it("略過壞掉的 JSON,不中斷後續事件", () => {
    const p = new SseParser();
    const events = p.push('data: 壞掉\n\ndata: {"type":"done"}\n\n');
    assert.deepEqual(events, [{ type: "done" }]);
  });

  it("空白事件不產出", () => {
    const p = new SseParser();
    assert.deepEqual(p.push("\n\n"), []);
  });

  it("沒有 data: 前綴的行也能解析", () => {
    const p = new SseParser();
    assert.deepEqual(p.push('{"type":"done"}\n\n'), [{ type: "done" }]);
  });
});

describe("狀態隔離", () => {
  it("兩個解析器互不影響", () => {
    const a = new SseParser();
    const b = new SseParser();
    a.push('data: {"type":"delta",');
    assert.deepEqual(b.push('data: {"type":"done"}\n\n'), [{ type: "done" }]);
  });
});
