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
    # 観測地点コードが5桁なら官署(daily_s1.php)、4桁ならアメダス(daily_a1.php)
    if len(area_code) == 5:
        url = f"https://www.data.jma.go.jp/stats/etrn/view/daily_s1.php?prec_no={prec_no}&block_no={area_code}&year={year}&month={month}&day=1&view="
        temp_col = 6
    else:
        url = f"https://www.data.jma.go.jp/stats/etrn/view/daily_a1.php?prec_no={prec_no}&block_no={area_code}&year={year}&month={month}&day=1&view="
        temp_col = 4

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
        if len(cell_texts) > temp_col:
            try:
                day = int(cell_texts[0])
                temp = float(cell_texts[temp_col])
                d = date(int(year), int(month), day)
                result[d] = temp
            except ValueError:
                pass
    return result


def get_normal_daily_temps(prec_no, area_code, month):
    """指定した観測地点の、指定した月の日別平年値(平均気温)を取得する。{日: 気温}のdictを返す"""
    # 観測地点コードが5桁なら官署(nml_sfc_d.php)、4桁ならアメダス(nml_amd_d.php)
    if len(area_code) == 5:
        url = f"https://www.data.jma.go.jp/stats/etrn/view/nml_sfc_d.php?prec_no={prec_no}&block_no={area_code}&year=&month={month}&day=1&view="
    else:
        url = f"https://www.data.jma.go.jp/stats/etrn/view/nml_amd_d.php?prec_no={prec_no}&block_no={area_code}&year=&month={month}&day=1&view="
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
        if len(cell_texts) > 2:
            try:
                day = int(cell_texts[0].rstrip("日"))
                temp = float(cell_texts[2])
                result[day] = temp
            except ValueError:
                pass
    return result


def get_daily_temperatures(prec_no, area_code, start_date, end_date):
    """期間内の日ごとの気温を返す。{date: {"temp": 気温, "is_actual": 実測かどうか}}
    実測値が無い日(未来日、またはまだ観測データが公開されていない日)は日別平年値で代用する。
    """
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

    normal_daily_cache = {}

    result = {}
    d = start_date
    while d <= end_date:
        temp = all_temps.get(d)
        is_actual = temp is not None

        if temp is None:
            month = d.month
            if month not in normal_daily_cache:
                normal_daily_cache[month] = get_normal_daily_temps(prec_no, area_code, month)
            temp = normal_daily_cache[month].get(d.day)

        result[d] = {"temp": temp, "is_actual": is_actual}
        d += timedelta(days=1)

    return result


def get_cumulative_series(prec_no, area_code, start_date, end_date, base_temp=0):
    """日ごとの気温・積算温度の推移をリストで返す"""
    daily_temps = get_daily_temperatures(prec_no, area_code, start_date, end_date)

    series = []
    total = 0
    for d, info in daily_temps.items():
        temp = info["temp"]
        if temp is not None and temp > base_temp:
            total += (temp - base_temp)
        series.append({
            "date": d,
            "temp": temp,
            "is_actual": info["is_actual"],
            "cumulative": round(total, 1),
        })

    return series


def build_chart_svg(series, width=1000, height=500):
    """積算温度の推移を折れ線グラフのSVGとして描画する"""
    if not series:
        return ""

    padding_left = 70
    padding_right = 30
    padding_top = 30
    padding_bottom = 60
    plot_w = width - padding_left - padding_right
    plot_h = height - padding_top - padding_bottom

    max_cum = max(s["cumulative"] for s in series)
    if max_cum <= 0:
        y_max = 10
    else:
        magnitude = 10 ** (len(str(int(max_cum))) - 1)
        y_max = ((int(max_cum) // magnitude) + 1) * magnitude

    n = len(series)

    def x_pos(i):
        if n == 1:
            return padding_left + plot_w / 2
        return padding_left + (i / (n - 1)) * plot_w

    def y_pos(value):
        return padding_top + plot_h - (value / y_max) * plot_h

    points = [(x_pos(i), y_pos(s["cumulative"])) for i, s in enumerate(series)]
    polyline_points = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)

    svg_parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" class="chart">']

    grid_steps = 5
    for i in range(grid_steps + 1):
        value = y_max / grid_steps * i
        y = y_pos(value)
        svg_parts.append(
            f'<line x1="{padding_left}" y1="{y:.1f}" x2="{width - padding_right}" y2="{y:.1f}" '
            f'stroke="#ddd" stroke-width="1" />'
        )
        svg_parts.append(
            f'<text x="{padding_left - 10}" y="{y + 5:.1f}" font-size="16" text-anchor="end" fill="#333">{value:.0f}</text>'
        )

    max_labels = 12
    label_step = max(1, -(-n // max_labels))
    for i, s in enumerate(series):
        if i % label_step == 0 or i == n - 1:
            x = x_pos(i)
            label = f'{s["date"].month}/{s["date"].day}'
            svg_parts.append(
                f'<text x="{x:.1f}" y="{height - padding_bottom + 25}" font-size="15" '
                f'text-anchor="middle" fill="#333">{label}</text>'
            )

    svg_parts.append(f'<polyline points="{polyline_points}" fill="none" stroke="#2b8ac4" stroke-width="3" />')

    for (x, y), s in zip(points, series):
        fill = "#2b8ac4" if s["is_actual"] else "#ffffff"
        svg_parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{fill}" stroke="#2b8ac4" stroke-width="2" />'
        )

    svg_parts.append("</svg>")
    return "".join(svg_parts)


@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    daily_series = None
    chart_svg = None
    error = None
    form = request.form if request.method == "POST" else {}

    if request.method == "POST":
        prec_no = request.form["prec_no"]
        area_code = request.form["area_code"]
        start_date = date.fromisoformat(request.form["start_date"])
        end_date = date.fromisoformat(request.form["end_date"])
        base_temp = float(request.form.get("base_temp") or 0)

        if start_date > end_date:
            error = "開始日は終了日より前の日付を指定してください"
        else:
            daily_series = get_cumulative_series(prec_no, area_code, start_date, end_date, base_temp)
            result = daily_series[-1]["cumulative"] if daily_series else 0
            chart_svg = build_chart_svg(daily_series)

    return render_template(
        "index.html",
        result=result,
        daily_series=daily_series,
        chart_svg=chart_svg,
        stations=STATIONS,
        error=error,
        form=form,
    )


if __name__ == "__main__":
    app.run(debug=True)