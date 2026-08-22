import requests
from bs4 import BeautifulSoup


def get_normal_monthly_temp(area_code, month):
    """指定した観測地点の、指定した月の平年値(平均気温)を取得する"""
    url = f"https://www.data.jma.go.jp/stats/etrn/view/nml_sfc_ym.php?prec_no=44&block_no={area_code}"
    response = requests.get(url)
    soup = BeautifulSoup(response.text, "html.parser")

    tables = soup.find_all("table")
    biggest_table = max(tables, key=lambda t: len(t.find_all("tr")))
    rows = biggest_table.find_all("tr")

    target_month_label = f"{month}月"

    for row in rows:
        cells = row.find_all(["td", "th"])
        cell_texts = [cell.get_text(strip=True) for cell in cells]
        if cell_texts and cell_texts[0] == target_month_label:
            try:
                return float(cell_texts[4])  # 平均気温の列
            except (ValueError, IndexError):
                return None
    return None


# テスト実行
temp = get_normal_monthly_temp("47662", 8)
print(f"8月の平年気温: {temp}")