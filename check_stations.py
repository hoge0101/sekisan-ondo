"""stations.json から、気温を観測していない観測地点を取り除く。

気象庁の観測地点には、降水量や風向風速だけを観測していて気温を測っていない地点が
含まれている。そうした地点を選ぶと全行が「---」になり計算できないため、
あらかじめ一覧から外しておく。

判定は地点ごとに気象庁へ問い合わせるので時間がかかる。実行時のみ必要で、
アプリの動作には不要。

    python check_stations.py --sample 20   # 少数で試す(書き込みなし)
    python check_stations.py --dry-run     # 全件判定するが書き込まない
    python check_stations.py               # 全件判定して stations.json を更新
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import app

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIONS_PATH = os.path.join(BASE_DIR, "stations.json")

WORKERS = 5      # 気象庁への同時接続数。増やしすぎない
RETRIES = 3      # 通信エラーで誤って「気温なし」と判定しないための再試行


def latest_complete_month():
    """観測が終わっている直近の年月を返す"""
    d = date.today().replace(day=1)
    while True:
        year, month = (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)
        if app._month_is_complete(year, month):
            return year, month
        d = date(year, month, 1)


def _fetch(func, *args):
    """通信エラーは再試行する。それでも駄目なら例外を投げて判定を中断する"""
    for attempt in range(RETRIES):
        try:
            return func(*args)
        except Exception:
            if attempt == RETRIES - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def has_temperature(prec_no, block_no, year, month):
    """その地点が気温を観測しているかを返す。(判定, 理由)"""
    # 平年値があれば確実に使える。多くの地点はここで判定できる
    if _fetch(app.get_normal_daily_temps, prec_no, block_no, month):
        return True, "平年値あり"

    # 平年値が無くても、実測が取れるなら過去の計算には使える
    if _fetch(app.get_monthly_data, prec_no, block_no, year, month):
        return True, "実測のみ"

    return False, "気温なし"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, help="先頭からこの件数だけ判定する")
    parser.add_argument("--dry-run", action="store_true", help="判定するが書き込まない")
    args = parser.parse_args()

    with open(STATIONS_PATH, encoding="utf-8") as f:
        data = json.load(f)

    targets = [
        (prec_no, pref["name"], station)
        for prec_no, pref in data.items()
        for station in pref["stations"]
    ]
    if args.sample:
        targets = targets[: args.sample]

    year, month = latest_complete_month()
    print(f"判定対象: {len(targets)}地点  (実測の確認に {year}年{month}月 を使用)")
    print()

    started = time.time()
    done = [0]

    def check(item):
        prec_no, pref_name, station = item
        ok, reason = has_temperature(prec_no, station["block_no"], year, month)
        done[0] += 1
        if done[0] % 50 == 0:
            elapsed = time.time() - started
            rate = done[0] / elapsed
            remain = (len(targets) - done[0]) / rate if rate else 0
            print(f"  {done[0]}/{len(targets)} 件  経過{elapsed:.0f}秒  残り約{remain:.0f}秒")
        return prec_no, pref_name, station, ok, reason

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(check, targets))

    removed = [r for r in results if not r[3]]
    print()
    print(f"完了: {time.time() - started:.0f}秒")
    print(f"  気温あり: {len(results) - len(removed)}地点")
    print(f"  気温なし: {len(removed)}地点")
    print()

    if removed:
        print("--- 取り除く地点 ---")
        for prec_no, pref_name, station, _, _ in removed:
            print(f"  {pref_name} / {station['name']} ({station['block_no']})")

    if args.sample or args.dry_run:
        print()
        print("(書き込みは行っていません)")
        return

    drop = {(r[0], r[2]["block_no"]) for r in removed}
    for prec_no, pref in data.items():
        pref["stations"] = [
            s for s in pref["stations"] if (prec_no, s["block_no"]) not in drop
        ]

    empty = [p["name"] for p in data.values() if not p["stations"]]
    if empty:
        print()
        print(f"警告: 地点が全て消えた都道府県があります: {empty}", file=sys.stderr)

    with open(STATIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print()
    print(f"stations.json を更新しました ({len(removed)}地点を削除)")


if __name__ == "__main__":
    main()
