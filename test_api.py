import requests
from bs4 import BeautifulSoup

def get_daily_avg_temps(area_code, year, month):
    """指定した地点・年・月の、日別平均気温のリストを取得する"""
    url = f"https://www.data.jma.go.jp/stats/etrn/view/daily_s1.php?prec_no=44&block_no={area_code}&year={year}&month={month}&day=1&view="
    
    response = requests.get(url)
    soup = BeautifulSoup(response.text, "html.parser")
    
    tables = soup.find_all("table")
    target_table = tables[5]
    rows = target_table.find_all("tr")
    
    temps = []
    # 4行目(インデックス3)以降がデータ行
    for row in rows[3:]:
        cells = row.find_all(["td", "th"])
        cell_texts = [cell.get_text(strip=True) for cell in cells]
        
        if len(cell_texts) > 6:
            temp_str = cell_texts[6]  # 気温平均の列
            try:
                temps.append(float(temp_str))
            except ValueError:
                pass  # "--"などの欠測値はスキップ
    
    return temps

# テスト実行
temps = get_daily_avg_temps("47662", "2026", "8")
print(temps)
print(f"合計: {sum(temps)}")