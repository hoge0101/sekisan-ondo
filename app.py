from flask import Flask, render_template, request
import requests
from bs4 import BeautifulSoup
from datetime import date, timedelta

app = Flask(__name__)


def get_monthly_data(area_code, year, month):
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
            try:
                day = int(cell_texts[0])
                temp = float(cell_texts[6])
                d = date(int(year), int(month), day)
                result[d] = temp
            except ValueError:
                pass
    return result


def get_cumulative_temperature(area_code, start_date, end_date, base_temp=0):
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
    return round(total, 1)


@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    if request.method == "POST":
        area_code = request.form["area_code"]
        start_date = date.fromisoformat(request.form["start_date"])
        end_date = date.fromisoformat(request.form["end_date"])
        base_temp = float(request.form.get("base_temp") or 0)

        result = get_cumulative_temperature(area_code, start_date, end_date, base_temp)

    return render_template("index.html", result=result)


if __name__ == "__main__":
    app.run(debug=True)