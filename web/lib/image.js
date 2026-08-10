// 拍照上傳前的縮圖。
//
// 手機直接拍出來的照片通常 3~8MB,不縮圖有兩個問題:上傳慢(豬舍裡的
// 訊號不一定好),而且超過伺服器端的請求上限會直接被擋掉。
//
// 一律重新編碼成 JPEG 還順便解掉 iPhone 的 HEIC —— 後端不支援那個格式,
// 但 canvas 解碼後輸出的就是 JPEG,問題在這一步自然消失。

// 超過這個尺寸對文字辨識沒有幫助,只是讓每次呼叫更貴。
export const MAX_EDGE = 1568;

// 藥品標示是密密麻麻的小字,壓得太狠會糊掉、讀錯劑量。
export const JPEG_QUALITY = 0.85;

// 等比例縮到最長邊不超過 max。
//
// 小圖不放大 —— 放大不會生出原本就不存在的細節,只是讓檔案變大、
// 上傳變慢,對辨識毫無幫助。
export function fitWithin(width, height, max = MAX_EDGE) {
  const longest = Math.max(width, height);
  if (!Number.isFinite(longest) || longest <= 0) return { width: 0, height: 0 };
  if (longest <= max) return { width: Math.round(width), height: Math.round(height) };

  const scale = max / longest;
  return {
    // 四捨五入後至少留 1 像素:極端長寬比(例如 4000×2 的細長條)
    // 算出來的短邊會是 0,canvas 給 0 會直接拋錯。
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  };
}

// 把 data URL 的前綴切掉,只留 base64 本體 —— 後端收的是純 base64。
export function stripDataUrl(dataUrl) {
  const comma = String(dataUrl || "").indexOf(",");
  return comma === -1 ? "" : dataUrl.slice(comma + 1);
}

// File → 縮圖過的 JPEG base64。需要 DOM,不在 node:test 下測試,
// 純計算的部分(fitWithin/stripDataUrl)才是有測試把關的。
export function fileToJpegBase64(file, max = MAX_EDGE, quality = JPEG_QUALITY) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();

    img.onload = () => {
      // 一定要釋放,否則每拍一張就漏一份原圖的記憶體 —— 手機上連拍
      // 十張很快就會被系統終止分頁。
      URL.revokeObjectURL(url);
      try {
        const { width, height } = fitWithin(img.naturalWidth, img.naturalHeight, max);
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext("2d");
        // 白底:JPEG 沒有透明通道,PNG 的透明區域不填白會變成黑色,
        // 剛好把深色的標示文字蓋掉。
        ctx.fillStyle = "#fff";
        ctx.fillRect(0, 0, width, height);
        ctx.drawImage(img, 0, 0, width, height);
        resolve(stripDataUrl(canvas.toDataURL("image/jpeg", quality)));
      } catch (e) {
        reject(e);
      }
    };

    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("讀不到這張照片"));
    };

    img.src = url;
  });
}
