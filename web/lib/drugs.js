// 牧場主自己的藥品庫。純邏輯部分(讀寫序列化、新增/移除),
// 跟 DOM 接線分開才能在 node:test 下測試 —— localStorage 不存在於無瀏覽器環境。
//
// 只存在瀏覽器,不上傳保存到伺服器的資料庫,跟對話歷史(app.js 的 history)
// 是同一種隱私考量:這是牧場主自己擁有的藥品清單,送出問題時才隨請求附上。

const STORAGE_KEY = "pigConsultant.myDrugs";

export function loadMyDrugs(storage) {
  try {
    const raw = storage.getItem(STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    // 壞掉的資料不該讓整個藥品庫功能掛掉,當作沒存過即可
    return [];
  }
}

export function saveMyDrugs(storage, drugs) {
  storage.setItem(STORAGE_KEY, JSON.stringify(drugs));
}

let idCounter = 0;

// 每次呼叫都要是不同的 id,否則清單裡刪除單一項目時會連帶刪掉同批新增的其他項目。
function makeId() {
  idCounter += 1;
  return `${Date.now()}-${idCounter}`;
}

export function addDrug(drugs, { name, dosageNote, withdrawalDays } = {}) {
  const trimmedName = (name || "").trim();
  if (!trimmedName) return drugs; // 沒有名字的藥品不构成一筆有效紀錄

  const entry = {
    id: makeId(),
    name: trimmedName,
    dosageNote: (dosageNote || "").trim(),
    withdrawalDays:
      typeof withdrawalDays === "number" && Number.isFinite(withdrawalDays) && withdrawalDays >= 0
        ? withdrawalDays
        : null,
  };
  return [...drugs, entry];
}

export function removeDrug(drugs, id) {
  return drugs.filter((d) => d.id !== id);
}
