# 字型來源

PDF 匯出(`pdf_report.py`)要嵌入實際字型,不能沿用 reportlab 內建的
「標準 14 種 CJK 字型」—— 那些不嵌真正的字型檔,只靠 PDF 閱讀器自己找字
替換,實測在這台機器上完全對不上號,「豬豬顧問」印出來變成「梓榴 菅」
這種毫無關係的字。改成直接嵌入真正的字型檔,PDF 帶著自己的字走,
不管拿到哪台機器、哪個作業系統打開都是同一批字。

## 這兩個檔案是什麼

`NotoSansTC-Regular.ttf`、`NotoSansTC-Bold.ttf` 是 Google 的
[Noto Sans TC](https://github.com/googlefonts/noto-cjk)(思源黑體繁體中文)
裁切過的版本,授權是 SIL Open Font License 1.1(見同目錄的 `LICENSE.txt`,
免費商用、可修改、可重新散布)。

## 為什麼是裁切過的,不是原始檔案

原始檔案(`Sans/OTF/TraditionalChinese/NotoSansCJKtc-{Regular,Bold}.otf`)
每個約 16–17 MB,兩個weight 加起來超過 30 MB —— 這個專案原本刻意不用任何
第三方套件(見 `requirements.txt`),字型這件事本身就已經是對這個原則的
一次讓步,不該再讓步到把整個 CJK Unicode 範圍(兩萬多字,含這個場永遠
不會用到的日文人名用字、生僻字)一起塞進版本庫。

裁切分兩步:

1. **限制字集**:窮舉所有 Big5 雙位元組組合解得出來的字(約 13,700 字,
   涵蓋台灣實務上幾乎所有會用到的繁體字,天生對得上這個場的資料語境),
   再聯集這個專案原始碼與使用者真實資料裡實際出現的每一個漢字。
2. **原始檔案是 CFF(PostScript 外框)格式,reportlab 的 TTFont 讀不懂**
   (`postscript outlines are not supported`)。用 `otf2ttf`(fontTools 的
   外框轉換工具)轉成 TrueType(glyf)外框,同時砍掉 GSUB/GPOS/BASE 這些
   複雜文字排版才需要的表 —— 純文字表格用不到連字、變體選擇這些功能。

裁完兩個 weight 各約 4.5 MB,合計約 9 MB。

## 重新產生的方法(字集需要更新時)

```bash
# 1. 下載原始字型(SIL OFL,https://github.com/googlefonts/noto-cjk)
# 2. 收集字集:窮舉 Big5 可解出的字 + 專案原始碼與資料裡實際出現的字
# 3. 用 fontTools.subset 裁切,--drop-tables 去掉不需要的排版表
# 4. 用 otf2ttf 把 CFF 外框轉成 TrueType 外框
```
