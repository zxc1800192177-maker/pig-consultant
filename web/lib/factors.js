// 生產健檢的「其他參考因素」。純邏輯部分(新增/移除),
// 跟 DOM 接線分開才能在 node:test 下測試。
//
// 跟藥品庫不同,這裡不存 localStorage —— 這些因素只跟「這一次健檢」有關,
// 換一次健檢就該重填,不必跨頁面保留,所以只在記憶體(app.js 的 state)裡活著。

let idCounter = 0;

// 每次呼叫都要是不同的 id,否則清單裡刪除單一項目時會連帶刪掉同批新增的其他項目。
function makeId() {
  idCounter += 1;
  return `${Date.now()}-${idCounter}`;
}

export function addFactor(factors, { name, value } = {}) {
  const trimmedName = (name || "").trim();
  if (!trimmedName) return factors; // 沒有名稱的因素不構成一筆有效紀錄

  const entry = {
    id: makeId(),
    name: trimmedName,
    value: (value || "").trim(),
  };
  return [...factors, entry];
}

export function removeFactor(factors, id) {
  return factors.filter((f) => f.id !== id);
}
