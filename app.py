from flask import Flask, render_template, request
import requests
from bs4 import BeautifulSoup
from datetime import date, timedelta
import json

app = Flask(__name__)

# 起動時に観測地点一覧を読み込んでおく
with open("stations.json", "r", encoding="utf-8") as f:
    STATIONS = json.load(f)


def get_monthly_data(prec_no, area_code, year, month):
    url = f"https://www.data.jma.go.jp/stats/etrn/view/daily_s1.php?prec_no={prec_no}&block_no={area_code}&year={year}&month={month}&day=1&view="
    response = requests.get(url)
    soup = BeautifulSoup(response.text, "html.parser")

    target_table = soup.find("table", class_="data2_s")
    if target_table is None:
        return {}

    rows = target_table.find_all("tr")

    result = {}
    for row in rows[3:]:
        cells = row.find_all(["td", "th"])
        cell_texts = [cell.get_text(strip=True) for cell in cells]
        if len(cell_texts) > 6:
            try:
                day = int(cell_texts[0])
                temp = float(cell_texts[6])
                d = date(int(year), int(month), day)
                result[d] = temp
            except ValueError:
                pass
    return result


def get_normal_monthly_temp(prec_no, area_code, month):
    """指定した観測地点の、指定した月の平年値(平均気温)を取得する"""
    url = f"https://www.data.jma.go.jp/stats/etrn/view/nml_sfc_ym.php?prec_no={prec_no}&block_no={area_code}"
    response = requests.get(url)
    soup = BeautifulSoup(response.text, "html.parser")

    tables = soup.find_all("table")
    if not tables:
        return None

    biggest_table = max(tables, key=lambda t: len(t.find_all("tr")))
    rows = biggest_table.find_all("tr")

    target_month_label = f"{month}月"

    for row in rows:
        cells = row.find_all(["td", "th"])
        cell_texts = [cell.get_text(strip=True) for cell in cells]
        if cell_texts and cell_texts[0] == target_month_label:
            try:
                return float(cell_texts[4])
            except (ValueError, IndexError):
                return None
    return None


def get_cumulative_temperature(prec_no, area_code, start_date, end_date, base_temp=0):
    today = date.today()

    all_temps = {}
    fetch_end = min(end_date, today)
    if start_date <= fetch_end:
        current = date(start_date.year, start_date.month, 1)
        while current <= fetch_end:
            monthly_data = get_monthly_data(prec_no, area_code, current.year, current.month)
            all_temps.update(monthly_data)
            if current.month == 12:
                current = date(current.year + 1, 1, 1)
            else:
                current = date(current.year, current.month + 1, 1)

    normal_temp_cache = {}

    total = 0
    d = start_date
    while d <= end_date:
        if d <= today:
            temp = all_temps.get(d)
        else:
            month = d.month
            if month not in normal_temp_cache:
                normal_temp_cache[month] = get_normal_monthly_temp(prec_no, area_code, month)
            temp = normal_temp_cache[month]

        if temp is not None and temp > base_temp:
            total += (temp - base_temp)

        d += timedelta(days=1)

    return round(total, 1)


@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    error = None

    if request.method == "POST":
        prec_no = request.form["prec_no"]
        area_code = request.form["area_code"]
        start_date = date.fromisoformat(request.form["start_date"])
        end_date = date.fromisoformat(request.form["end_date"])
        base_temp = float(request.form.get("base_temp") or 0)

        if start_date > end_date:
            error = "開始日は終了日より前の日付を指定してください"
        else:
            result = get_cumulative_temperature(prec_no, area_code, start_date, end_date, base_temp)

    return render_template("index.html", result=result, stations=STATIONS, error=error)


if __name__ == "__main__":
    app.run(debug=True)