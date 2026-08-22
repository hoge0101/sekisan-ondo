import requests
from bs4 import BeautifulSoup
import re
import json
import time

BASE_URL = "https://www.data.jma.go.jp/stats/etrn/select/prefecture00.php"

def get_prefecture_list():
    """都道府県・地方の一覧(prec_noと名前)を取得する"""
    url = f"{BASE_URL}?prec_no=&block_no=&year=&month=&day=&view="
    response = requests.get(url)
    soup = BeautifulSoup(response.text, "html.parser")

    prefectures = {}
    area_tags = soup.find_all("area")
    for tag in area_tags:
        href = tag.get("href", "")
        match = re.search(r"prec_no=(\d+)", href)
        if match:
            prec_no = match.group(1)
            name = tag.get("alt", "")
            if name:
                prefectures[prec_no] = name
    return prefectures


def get_stations_in_prefecture(prec_no):
    """指定した都道府県内の観測地点一覧(block_noと名前)を取得する"""
    url = f"https://www.data.jma.go.jp/stats/etrn/select/prefecture.php?prec_no={prec_no}"
    response = requests.get(url)
    soup = BeautifulSoup(response.text, "html.parser")

    seen = set()  # 重複チェック用
    stations = []
    area_tags = soup.find_all("area")
    for tag in area_tags:
        href = tag.get("href", "") or tag.get("onmouseover", "")
        match = re.search(r"block_no=(\d+)", href)
        if match:
            block_no = match.group(1)
            name = tag.get("alt", "")

            # "全地点"のような集計データは除外
            if not name or "全地点" in name:
                continue

            # 重複チェック(block_noで判定)
            if block_no in seen:
                continue
            seen.add(block_no)

            stations.append({"block_no": block_no, "name": name})
    return stations


def main():
    print("都道府県一覧を取得中...")
    prefectures = get_prefecture_list()
    print(f"{len(prefectures)}件の都道府県・地方を取得")

    all_data = {}
    for prec_no, pref_name in prefectures.items():
        print(f"取得中: {pref_name} ({prec_no})")
        stations = get_stations_in_prefecture(prec_no)
        all_data[prec_no] = {
            "name": pref_name,
            "stations": stations
        }
        time.sleep(1)

    with open("stations.json", "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)

    print("stations.json に保存しました")


if __name__ == "__main__":
    main()