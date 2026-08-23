# Covered Call 觀察站

個人實驗用：單一標的（預設 TSLA）Covered Call 參考介面。
延遲選擇權資料來自 Yahoo（yfinance），僅供觀察，不是下單行情。

## 本機

雙擊 `start.bat`，保持黑窗開著，瀏覽器開 http://127.0.0.1:5174/
右上角顯示 `yahoo-delayed` 即為延遲真實資料。

## 免費雲端（Render）

見 `雲端怎麼上線.txt`。Free Web Service，啟動指令：

`gunicorn -b 0.0.0.0:$PORT --timeout 120 server:app`

閒置會睡著，第一次打開可能要等半分鐘左右。
