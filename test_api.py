import requests
from bs4 import BeautifulSoup
from datetime import date, timedelta

def get_monthly_data(area_code, year, month):
    """指定した年月の、日別平均気温を {日付: 気温} の辞書で返す"""
    url = f"https://www.data.jma.go.jp/stats/etrn/view/daily_s1.php?prec_no=44&block_no={area_code}&year={year}&month={month}&day=1&view="
    
    response = requests.get(url)
    soup = BeautifulSoup(response.text, "html.parser")
    
    tables = soup.find_all("table")
    target_table = tables[5]
    rows = target_table.find_all("tr")
    
    result = {}
    for row in rows[3:]:
        cells = row.find_all(["td", "th"])
        cell_texts = [cell.get_text(strip=True) for cell in cells]
        
        if len(cell_texts) > 6:
            day_str = cell_texts[0]
            temp_str = cell_texts[6]
            try:
                day = int(day_str)
                temp = float(temp_str)
                d = date(int(year), int(month), day)
                result[d] = temp
            except ValueError:
                pass
    
    return result


def get_cumulative_temperature(area_code, start_date, end_date, base_temp=0):
    """開始日〜終了日の積算温度を計算する
    base_temp: 基準温度(この温度を下回る日は積算せず、上回る日は基準温度を引いてから積算)
    """
    all_temps = {}
    
    current = date(start_date.year, start_date.month, 1)
    while current <= end_date:
        monthly_data = get_monthly_data(area_code, current.year, current.month)
        all_temps.update(monthly_data)
        
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    
    total = 0
    d = start_date
    while d <= end_date:
        if d in all_temps:
            temp = all_temps[d]
            if temp > base_temp:
                total += (temp - base_temp)
        d += timedelta(days=1)
    
    return total


# テスト実行
start = date(2026, 7, 25)
end = date(2026, 8, 10)

total1 = get_cumulative_temperature("47662", start, end)
print(f"基準温度なし: {round(total1, 1)}")

total2 = get_cumulative_temperature("47662", start, end, base_temp=10)
print(f"基準温度10℃: {round(total2, 1)}")