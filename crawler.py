"""
TWSE 每日收盤行情自動爬取 (TWSE Daily Closing Quotes Crawler)

資料來源 (Data sources)：依序嘗試，任一成功即停止
    1. 證交所 OpenAPI  https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL
    2. 證交所官網 JSON  https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=json
    3. 證交所官網 CSV   https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=csv

設計重點 (Design notes)：
    1. 冪等 (idempotent)：以 (date, code) 為主鍵，同一天重跑不會產生重複資料。
    2. 非交易日自動略過：API 在假日仍會回傳「上一個交易日」的資料，
       因此比對回傳的日期是否等於今天，不相等就正常結束、不寫入。
    3. 多來源備援 + 診斷日誌：單一來源擋掉時自動換下一個；失敗時印出
       HTTP 狀態碼、Content-Type、回應長度與前 200 字，方便在 Actions log 裡判讀。
    4. 離開代碼 (exit code)：0 = 成功或略過；1 = 失敗（GitHub Actions 會標記紅色並寄信）。

用法 (Usage)：
    python crawler.py            # 正常模式，非交易日會略過
    python crawler.py --force    # 忽略日期檢查，強制寫入（本機測試用）
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------- 設定 (Config)

API_URLS = [
    "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
    "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=json",
    "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=csv",
]

# 用一般瀏覽器的標頭，避免被來源端當成機器人擋掉
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/csv, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.twse.com.tw/",
}

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "stock.db"
TPE = ZoneInfo("Asia/Taipei")          # 台北時區 (Taipei timezone, UTC+8，無日光節約時間)

# 官網中文欄位 → OpenAPI 英文欄位
CH_TO_EN = {
    "日期": "Date",
    "證券代號": "Code",
    "證券名稱": "Name",
    "成交股數": "TradeVolume",
    "成交金額": "TradeValue",
    "開盤價": "OpeningPrice",
    "最高價": "HighestPrice",
    "最低價": "LowestPrice",
    "收盤價": "ClosingPrice",
    "漲跌價差": "Change",
    "漲跌(+/-)": "ChangeSign",   # 官網把正負號獨立成一欄，需與漲跌價差合併
    "成交筆數": "Transaction",
}

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS daily_price (
    date         TEXT    NOT NULL,   -- 西元日期 YYYY-MM-DD
    code         TEXT    NOT NULL,   -- 證券代號 (stock code)
    name         TEXT,               -- 證券名稱
    volume       INTEGER,            -- 成交股數 (trade volume)
    amount       INTEGER,            -- 成交金額 (trade value)
    open_price   REAL,               -- 開盤價 (opening price)
    high_price   REAL,               -- 最高價 (highest price)
    low_price    REAL,               -- 最低價 (lowest price)
    close_price  REAL,               -- 收盤價 (closing price)
    change       REAL,               -- 漲跌價差 (change)
    transactions INTEGER,            -- 成交筆數 (transactions)
    PRIMARY KEY (date, code)
);
"""
INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_code_date ON daily_price(code, date);"

INSERT_SQL = """
INSERT OR REPLACE INTO daily_price
    (date, code, name, volume, amount,
     open_price, high_price, low_price, close_price, change, transactions)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""


# ------------------------------------------------------- 工具函式 (Helpers)

def log(msg: str) -> None:
    """統一輸出格式，方便在 GitHub Actions 的 log 裡辨認。"""
    now = datetime.now(TPE).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def to_date(raw: str) -> str:
    """
    把各種日期字串統一轉成西元 YYYY-MM-DD。
    支援民國 7 碼 '1150915'、西元 8 碼 '20260915'、以及 '115/09/15'。
    """
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(digits) == 8:                       # 西元 20260915
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    if len(digits) == 7:                       # 民國 1150915
        return f"{int(digits[:3]) + 1911}-{digits[3:5]}-{digits[5:]}"
    raise ValueError(f"無法解析的日期格式：{raw!r}")


def to_number(value, cast=float):
    """
    把字串轉成數字。來源會用 '--'、''、'X' 表示無資料，數字含千分位逗號。
    轉換失敗一律回傳 None，讓資料庫存 NULL，不要讓整支程式掛掉。
    """
    if value is None:
        return None
    text = str(value).replace(",", "").replace("+", "").replace("　", "").strip()
    if text in ("", "--", "-", "X", "N/A"):
        return None
    try:
        return cast(text)
    except ValueError:
        return None


# ------------------------------------------------- 各來源格式的轉換 (Normalizers)

def from_openapi(payload: list) -> list[dict]:
    """OpenAPI 直接就是 [{Date, Code, ...}, ...]。"""
    return payload


def from_fields_data(payload: dict) -> list[dict]:
    """官網 JSON 格式：{"stat": "OK", "date": "20260916", "fields": [...], "data": [[...], ...]}。"""
    if payload.get("stat") and payload["stat"] != "OK":
        raise ValueError(f"來源回報狀態：{payload['stat']}")
    fields = payload.get("fields") or []
    rows = payload.get("data") or []
    default_date = payload.get("date")
    out = []
    for row in rows:
        item = {}
        for field, cell in zip(fields, row):
            key = CH_TO_EN.get(field.strip())
            if key:
                item[key] = cell
        if "Date" not in item and default_date:
            item["Date"] = default_date
        out.append(item)
    return out


def from_csv(text: str) -> list[dict]:
    """官網 CSV 格式：找出含「證券代號」的標題列，之後每列一檔股票。"""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header_idx = next((i for i, ln in enumerate(lines) if "證券代號" in ln), None)
    if header_idx is None:
        raise ValueError("CSV 中找不到「證券代號」標題列")
    reader = csv.reader(io.StringIO("\n".join(lines[header_idx:])))
    header = [h.strip().strip('"').replace("=", "") for h in next(reader)]
    out = []
    for row in reader:
        if len(row) < len(header):
            continue
        item = {}
        for field, cell in zip(header, row):
            key = CH_TO_EN.get(field)
            if key:
                item[key] = cell.strip().strip('"').replace("=", "")
        if item.get("Code"):
            out.append(item)
    return out


def parse_body(body: str) -> list[dict]:
    """依內容自動判斷是 JSON 陣列、JSON 物件，還是 CSV。"""
    head = body.lstrip()[:1]
    if head == "[":
        return from_openapi(json.loads(body))
    if head == "{":
        return from_fields_data(json.loads(body))
    return from_csv(body)


# ----------------------------------------------------------- 抓取 (Fetching)

def fetch() -> list[dict]:
    """依序嘗試每個來源，每個來源最多重試 2 次。全部失敗才拋出例外。"""
    last_error = None
    for url in API_URLS:
        for attempt in (1, 2):
            try:
                log(f"抓取：{url}（第 {attempt} 次）")
                resp = requests.get(url, headers=HEADERS, timeout=30)
                ctype = resp.headers.get("Content-Type", "?")
                body = resp.text
                log(f"  HTTP {resp.status_code} | {ctype} | {len(body)} bytes")

                if resp.status_code != 200:
                    raise ValueError(f"HTTP {resp.status_code}")
                if not body.strip():
                    raise ValueError("回應是空的（來源端可能擋掉了這個 IP）")

                data = parse_body(body)
                if not data:
                    raise ValueError("解析後沒有任何資料列")
                log(f"  解析成功，共 {len(data)} 筆")
                return data

            except Exception as exc:                   # noqa: BLE001
                last_error = exc
                log(f"  失敗：{type(exc).__name__}: {exc}")
                try:
                    log(f"  回應前 200 字：{resp.text[:200]!r}")
                except Exception:                      # noqa: BLE001
                    pass
                if attempt == 1:
                    time.sleep(5)

        log("  換下一個來源…")

    raise RuntimeError(f"所有來源都失敗，最後一個錯誤：{last_error}")


# ------------------------------------------------------ 清洗與儲存 (Transform & Save)

def transform(raw: list[dict]) -> list[tuple]:
    """把來源資料清洗成可寫入資料庫的 tuple 清單。"""
    rows: list[tuple] = []
    for item in raw:
        code = str(item.get("Code") or "").strip()
        if not code:
            continue

        # 官網把「漲跌(+/-)」與「漲跌價差」拆成兩欄，這裡合併回一個帶正負號的數字。
        # 注意：正負號欄位可能是 HTML 標籤（如 <p style=color:green>-</p>），所以用 in 判斷。
        change = to_number(item.get("Change"))
        sign = str(item.get("ChangeSign") or "")
        if change is not None and "-" in sign:
            change = -change

        rows.append((
            to_date(item["Date"]),
            code,
            str(item.get("Name") or "").strip(),
            to_number(item.get("TradeVolume"), int),
            to_number(item.get("TradeValue"), int),
            to_number(item.get("OpeningPrice")),
            to_number(item.get("HighestPrice")),
            to_number(item.get("LowestPrice")),
            to_number(item.get("ClosingPrice")),
            change,
            to_number(item.get("Transaction"), int),
        ))
    return rows


def already_have(data_date: str, db_path: Path = DB_PATH) -> bool:
    """資料庫裡是否已經有這一天的資料？"""
    if not db_path.exists():
        return False
    with sqlite3.connect(db_path) as conn:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='daily_price'"
        ).fetchone()
        if not exists:
            return False
        n = conn.execute(
            "SELECT COUNT(*) FROM daily_price WHERE date = ?", (data_date,)
        ).fetchone()[0]
    return n > 0


def save(rows: list[tuple], db_path: Path = DB_PATH) -> int:
    """寫入 SQLite。回傳資料表總筆數。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(CREATE_SQL)
        conn.execute(INDEX_SQL)
        conn.executemany(INSERT_SQL, rows)
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0]
    return total


# ------------------------------------------------------------ 主流程 (Main)

def main() -> int:
    force = "--force" in sys.argv
    today = datetime.now(TPE).strftime("%Y-%m-%d")
    log(f"台北時間今日：{today}（force={force}）")

    raw = fetch()
    data_date = to_date(raw[0]["Date"])
    log(f"來源資料日期：{data_date}")
    if data_date == today:
        log("  → 這是今天的收盤資料")
    else:
        log("  → 來源還沒更新到今天（非交易日，或證交所尚未發布），這是最近一個交易日的資料")

    # 判斷要不要寫入，看的是「資料庫有沒有這一天」，不是「這一天是不是今天」。
    # 這樣不管遇到週末、國定假日、颱風假，或證交所延遲發布，行為都正確。
    if already_have(data_date) and not force:
        log(f"資料庫已經有 {data_date} 的資料 → 略過，不重複寫入。")
        return 0

    rows = transform(raw)
    if not rows:
        log("清洗後沒有任何有效資料，視為失敗。")
        return 1

    total = save(rows)
    closes = [r[8] for r in rows if r[8] is not None]
    log(f"寫入完成：本次 {len(rows)} 檔；資料庫累計 {total} 筆")
    if closes:
        log(f"收盤價區間：{min(closes)} ~ {max(closes)}")
    log(f"資料庫位置：{DB_PATH}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                           # noqa: BLE001
        log(f"執行失敗：{type(exc).__name__}: {exc}")
        sys.exit(1)
