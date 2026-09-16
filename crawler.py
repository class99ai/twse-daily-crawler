"""
TWSE 每日收盤行情自動爬取 (TWSE Daily Closing Quotes Crawler)

資料來源 (Data source)：
    臺灣證券交易所 OpenAPI - 個股日成交資訊
    https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL

設計重點 (Design notes)：
    1. 冪等 (idempotent)：以 (date, code) 為主鍵，同一天重跑不會產生重複資料。
    2. 非交易日自動略過：API 在假日仍會回傳「上一個交易日」的資料，
       因此比對回傳的日期是否等於今天，不相等就正常結束、不寫入。
    3. 失敗重試 (retry)：網路不穩時自動重試 3 次。
    4. 離開代碼 (exit code)：0 = 成功或略過；1 = 失敗（GitHub Actions 會標記紅色並寄信）。

用法 (Usage)：
    python crawler.py            # 正常模式，非交易日會略過
    python crawler.py --force    # 忽略日期檢查，強制寫入（本機測試用）
"""

from __future__ import annotations

import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------- 設定 (Config)

API_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "stock.db"
TPE = ZoneInfo("Asia/Taipei")          # 台北時區 (Taipei timezone, UTC+8，無日光節約時間)
HEADERS = {"User-Agent": "Mozilla/5.0 (TWSE daily crawler; teaching demo)"}

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


def roc_to_ad(roc: str) -> str:
    """民國日期字串轉西元 (ROC date -> AD date)：'1150915' -> '2026-09-15'。"""
    roc = roc.strip()
    year = int(roc[:-4]) + 1911
    return f"{year}-{roc[-4:-2]}-{roc[-2:]}"


def to_number(value, cast=float):
    """
    把 API 回傳的字串轉成數字。
    TWSE 會用 '--'、''、'0.00' 等表示無資料，且數字含千分位逗號。
    轉換失敗一律回傳 None，讓資料庫存 NULL，不要讓整支程式掛掉。
    """
    if value is None:
        return None
    text = str(value).replace(",", "").replace("+", "").strip()
    if text in ("", "--", "-", "X", "N/A"):
        return None
    try:
        return cast(text)
    except ValueError:
        return None


def fetch(url: str = API_URL, retries: int = 3, timeout: int = 30) -> list[dict]:
    """抓取 API，失敗時以遞增間隔重試。"""
    for attempt in range(1, retries + 1):
        try:
            log(f"抓取 API（第 {attempt} 次）：{url}")
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list) or not data:
                raise ValueError("API 回傳空資料或格式不符")
            log(f"抓取成功，共 {len(data)} 筆")
            return data
        except Exception as exc:                      # noqa: BLE001
            log(f"抓取失敗：{exc}")
            if attempt == retries:
                raise
            wait = 5 * attempt
            log(f"{wait} 秒後重試…")
            time.sleep(wait)
    return []                                          # 理論上不會執行到


def transform(raw: list[dict]) -> list[tuple]:
    """把 API 的 JSON 清洗成可寫入資料庫的 tuple 清單。"""
    rows: list[tuple] = []
    for item in raw:
        code = (item.get("Code") or "").strip()
        if not code:
            continue
        rows.append((
            roc_to_ad(item["Date"]),
            code,
            (item.get("Name") or "").strip(),
            to_number(item.get("TradeVolume"), int),
            to_number(item.get("TradeValue"), int),
            to_number(item.get("OpeningPrice")),
            to_number(item.get("HighestPrice")),
            to_number(item.get("LowestPrice")),
            to_number(item.get("ClosingPrice")),
            to_number(item.get("Change")),
            to_number(item.get("Transaction"), int),
        ))
    return rows


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
    data_date = roc_to_ad(raw[0]["Date"])
    log(f"API 資料日期：{data_date}")

    if data_date != today and not force:
        log("資料日期不是今天 → 判斷為非交易日或資料尚未更新，本次略過不寫入。")
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
