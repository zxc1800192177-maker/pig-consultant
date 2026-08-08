# 部署到 Render(對外上線)

給客戶用的正式環境。本機/demo 不需要看這份文件 —— 直接 `python server.py` 即可,
會自動使用 claude.ai 訂閱額度。

## 為什麼要換成 API 計費

個人 claude.ai 訂閱只授權本人使用,不得用來服務外部客戶。
一旦網址是「客戶隨時都能連」,就必須改用 Anthropic API(按用量計費)。
程式已經處理好這個切換:**有沒有設定 `ANTHROPIC_API_KEY` 這個環境變數,決定走哪一條路**,
不需要改任何程式碼(見 `ai/transport_selection.py`)。

## 步驟

### 1. 推上 GitHub

```bash
git init
git add .
git commit -m "Initial commit"
```

到 [github.com/new](https://github.com/new) 建一個新的儲存庫(Public 或 Private 皆可),
建好後照畫面指示,把上面這個本機儲存庫推上去(畫面會給你確切指令,類似):

```bash
git remote add origin https://github.com/<你的帳號>/<儲存庫名稱>.git
git push -u origin main
```

`.env`(你的金鑰)已經被 `.gitignore` 排除,不會被推上去。

### 2. 在 Render 建立服務

1. 到 [render.com](https://render.com) 註冊(可以用 GitHub 帳號登入)
2. 「New +」→「Web Service」
3. 選擇你剛剛推上去的 GitHub 儲存庫
4. Render 會偵測到 `render.yaml`,自動帶入大部分設定
5. 在「Environment」分頁,填入 `ANTHROPIC_API_KEY`(貼你的金鑰,這步只有你在 Render 網頁上操作)
6. 按「Create Web Service」

### 3. 確認

服務啟動後,Render 會給一個網址,類似 `https://pig-consultant.onrender.com`。

打開 `https://<你的網址>/api/health`,應該看到:

```json
{"aiAvailable": true, "gradingAvailable": true, "source": "..."}
```

若 `aiAvailable` 是 `false`,回 Render 後台確認 `ANTHROPIC_API_KEY` 有沒有填對。

## 之後要更新程式怎麼辦

改完程式碼後:

```bash
git add .
git commit -m "描述這次改了什麼"
git push
```

Render 偵測到 GitHub 有新的 commit 會自動重新部署,不需要手動做任何事。

## 費用與用量保護

- 按實際 token 用量計費,不是訂閱制。用量可在 [console.anthropic.com](https://console.anthropic.com) 查看。
- `MAX_AI_REQUESTS_PER_DAY`(預設 500)是程式內建的安全氣囊,超過會回應「今日已達上限」,
  避免因為 bug 或濫用不小心燒光預算。**這不是計費上限本身** ——
  真正想設每月花費上限,請到 console.anthropic.com 的帳單設定另外設定。
- Render 免費方案的服務閒置一段時間會休眠,下次連線時第一個請求會慢一點(喚醒時間)。
  真的要穩定給客戶用,建議升級到付費方案(約 $7/月起)。

## 網域

Render 預設會給一個 `*.onrender.com` 的網址,可以直接用。
要用自己的網域(例如 `pigadvisor.tw`),到 Render 後台的「Custom Domains」設定,
需要你自己持有網域並依指示改 DNS。
