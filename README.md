# twse-daily-crawler

每日自動爬取臺灣證券交易所（TWSE）收盤行情，存入 SQLite 資料庫。

由 GitHub Actions 於**每個工作日台北時間 17:00** 自動執行，非交易日自動略過。

## 資料來源

臺灣證券交易所 OpenAPI — 個股日成交資訊
<https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL>

## 專案結構

```
.github/workflows/daily_crawl.yml   排程設定
crawler.py                          爬蟲主程式
query_example.py                    資料查詢驗證工具
requirements.txt                    套件清單
data/stock.db                       SQLite 資料庫（自動產生、自動更新）
```

## 本機執行

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python crawler.py                # 正常模式，非交易日會略過
python crawler.py --force        # 忽略日期檢查，強制寫入（測試用）

python query_example.py          # 檢視資料總覽
python query_example.py 2330     # 檢視個股
```

## 資料表結構

`daily_price`，主鍵為 `(date, code)`，重複執行不會產生重複資料。

| 欄位 | 型別 | 說明 |
|---|---|---|
| date | TEXT | 西元日期 YYYY-MM-DD |
| code | TEXT | 證券代號 |
| name | TEXT | 證券名稱 |
| volume | INTEGER | 成交股數 |
| amount | INTEGER | 成交金額 |
| open_price | REAL | 開盤價 |
| high_price | REAL | 最高價 |
| low_price | REAL | 最低價 |
| close_price | REAL | 收盤價 |
| change | REAL | 漲跌價差 |
| transactions | INTEGER | 成交筆數 |

## 設定提醒

儲存庫需開啟寫入權限，否則 Actions 無法把資料庫推回：
**Settings → Actions → General → Workflow permissions → Read and write permissions**

## 授權與用途

教學示範用途。資料版權屬臺灣證券交易所所有，請遵守其使用條款。
