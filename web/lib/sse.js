// 串流事件解析。
//
// 網路封包不會剛好切在事件邊界上,可能收到半個事件。
// 解析器保留未完成的尾巴,等下一批資料補齊 —— 漏掉一段就等於使用者少看一段回答。

export class SseParser {
  constructor() {
    this.buffer = "";
  }

  // 餵入一段原始文字,回傳這次能組出的完整事件陣列。
  push(chunk) {
    this.buffer += chunk;
    const parts = this.buffer.split("\n\n");
    this.buffer = parts.pop();

    const events = [];
    for (const part of parts) {
      const line = part.replace(/^data:\s?/, "").trim();
      if (!line) continue;
      try {
        events.push(JSON.parse(line));
      } catch {
        // 壞掉的一筆不該中斷後續事件
      }
    }
    return events;
  }
}
