"""
驗證用小工具：把 data/stock.db 的內容撈出來看看。
用法：
    python query_example.py           # 顯示總覽
    python query_example.py 2330      # 顯示某檔股票的歷史收盤
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "stock.db"


def main() -> None:
    if not DB_PATH.exists():
        print(f"找不到資料庫：{DB_PATH}\n請先執行 python crawler.py")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if len(sys.argv) > 1:
        code = sys.argv[1]
        rows = conn.execute(
            "SELECT date, name, open_price, high_price, low_price, close_price, change "
            "FROM daily_price WHERE code = ? ORDER BY date DESC LIMIT 20",
            (code,),
        ).fetchall()
        if not rows:
            print(f"查無代號 {code} 的資料")
            return
        print(f"=== {code} {rows[0]['name']} 最近 {len(rows)} 個交易日 ===")
        print(f"{'日期':<12}{'開':>9}{'高':>9}{'低':>9}{'收':>9}{'漲跌':>9}")
        for r in rows:
            print(f"{r['date']:<12}{r['open_price'] or 0:>9.2f}{r['high_price'] or 0:>9.2f}"
                  f"{r['low_price'] or 0:>9.2f}{r['close_price'] or 0:>9.2f}{r['change'] or 0:>9.2f}")
    else:
        total = conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0]
        days = conn.execute("SELECT COUNT(DISTINCT date) FROM daily_price").fetchone()[0]
        rng = conn.execute("SELECT MIN(date), MAX(date) FROM daily_price").fetchone()
        print(f"總筆數：{total:,}")
        print(f"交易日數：{days}")
        print(f"日期範圍：{rng[0]} ~ {rng[1]}")
        print("\n最新交易日成交金額前 10 名：")
        rows = conn.execute(
            "SELECT code, name, close_price, change, amount FROM daily_price "
            "WHERE date = (SELECT MAX(date) FROM daily_price) "
            "ORDER BY amount DESC LIMIT 10"
        ).fetchall()
        for r in rows:
            print(f"  {r['code']:<8}{r['name']:<12}收 {r['close_price']:>8.2f}"
                  f"  漲跌 {r['change'] or 0:>7.2f}  成交金額 {r['amount']:>15,}")

    conn.close()


if __name__ == "__main__":
    main()
