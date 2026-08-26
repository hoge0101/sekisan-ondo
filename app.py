from flask import Flask, render_template, request
import requests
from bs4 import BeautifulSoup
from datetime import date, timedelta
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


app = Flask(__name__)

# 起動時に観測地点一覧を読み込んでおく
with open(os.path.join(BASE_DIR, "stations.json"), "r", encoding="utf-8") as f:
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
    """期間内の日ごとの実測気温と平年値を返す。
    {date: {"actual": 実測気温(昨日まで、それ以降はNone), "normal": 平年値(全ての日)}}
    """
    today = date.today()
    yesterday = today - timedelta(days=1)

    all_temps = {}
    fetch_end = min(end_date, yesterday)
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
        actual = all_temps.get(d)

        month = d.month
        if month not in normal_daily_cache:
            normal_daily_cache[month] = get_normal_daily_temps(prec_no, area_code, month)
        normal = normal_daily_cache[month].get(d.day)

        result[d] = {"actual": actual, "normal": normal}
        d += timedelta(days=1)

    return result


def get_cumulative_series(prec_no, area_code, start_date, end_date, base_temp=0, correction=0):
    """日ごとの実測気温・平年値・積算温度の推移をリストで返す。
    積算には実測値(昨日まで)があればそれを、無ければ平年値を使う。
    correction: 観測地点の気温と実際の設置場所の気温のズレを補正する一律オフセット(℃)
    """
    daily_temps = get_daily_temperatures(prec_no, area_code, start_date, end_date)

    series = []
    total = 0
    for d, info in daily_temps.items():
        actual = info["actual"]
        normal = info["normal"]
        if actual is not None:
            actual += correction
        if normal is not None:
            normal += correction

        temp = actual if actual is not None else normal
        if temp is not None and temp > base_temp:
            total += (temp - base_temp)

        series.append({
            "date": d,
            "actual": actual,
            "normal": normal,
            "is_actual": actual is not None,
            "cumulative": round(total, 1),
        })

    return series


def get_cumulative_series_until_target(prec_no, area_code, start_date, target_temp, base_temp=0, correction=0, max_days=1095):
    """開始日から積算温度がtarget_tempに達するまでの日ごとの推移を返す。
    (series, reached)のタプルを返す。max_days(既定3年)以内に達しなければreached=Falseになる。
    """
    today = date.today()
    yesterday = today - timedelta(days=1)

    monthly_cache = {}
    normal_daily_cache = {}

    def get_actual(d):
        key = (d.year, d.month)
        if key not in monthly_cache:
            monthly_cache[key] = get_monthly_data(prec_no, area_code, d.year, d.month)
        return monthly_cache[key].get(d)

    def get_normal(d):
        if d.month not in normal_daily_cache:
            normal_daily_cache[d.month] = get_normal_daily_temps(prec_no, area_code, d.month)
        return normal_daily_cache[d.month].get(d.day)

    series = []
    total = 0
    reached = False
    d = start_date
    for _ in range(max_days):
        actual = get_actual(d) if d <= yesterday else None
        normal = get_normal(d)
        if actual is not None:
            actual += correction
        if normal is not None:
            normal += correction

        temp = actual if actual is not None else normal
        if temp is not None and temp > base_temp:
            total += (temp - base_temp)

        series.append({
            "date": d,
            "actual": actual,
            "normal": normal,
            "is_actual": actual is not None,
            "cumulative": round(total, 1),
        })

        if total >= target_temp:
            reached = True
            break

        d += timedelta(days=1)

    return series, reached


def get_station_name(prec_no, area_code):
    """「宗谷地方 稚内」のような表示用の地点名を返す"""
    pref = STATIONS.get(prec_no)
    if not pref:
        return None
    for station in pref["stations"]:
        if station["block_no"] == area_code:
            return f'{pref["name"]} {station["name"]}'
    return pref["name"]


def build_summary(series):
    """結果画面に出すサマリー(期間・平均気温・実測/平年値の内訳)を組み立てる"""
    if not series:
        return None

    used_temps = [
        (s["actual"] if s["actual"] is not None else s["normal"])
        for s in series
    ]
    used_temps = [t for t in used_temps if t is not None]

    actual_days = sum(1 for s in series if s["is_actual"])

    return {
        "days": len(series),
        "start_date": series[0]["date"],
        "end_date": series[-1]["date"],
        "avg_temp": round(sum(used_temps) / len(used_temps), 1) if used_temps else None,
        "actual_days": actual_days,
        "normal_days": len(series) - actual_days,
    }


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
    baseline = y_pos(0)

    svg_parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" class="chart">']

    # 積算(=熱の蓄積)を、下は涼しい青・上は暖かい橙で表現する
    svg_parts.append(
        '<defs>'
        '<linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0%" stop-color="#f0913a" stop-opacity="0.42" />'
        '<stop offset="55%" stop-color="#4a90d9" stop-opacity="0.20" />'
        '<stop offset="100%" stop-color="#4a90d9" stop-opacity="0.02" />'
        '</linearGradient>'
        '<linearGradient id="lineGrad" x1="0" y1="1" x2="1" y2="0">'
        '<stop offset="0%" stop-color="#4a90d9" />'
        '<stop offset="100%" stop-color="#ee7d2d" />'
        '</linearGradient>'
        '</defs>'
    )

    grid_steps = 5
    for i in range(grid_steps + 1):
        value = y_max / grid_steps * i
        y = y_pos(value)
        svg_parts.append(
            f'<line x1="{padding_left}" y1="{y:.1f}" x2="{width - padding_right}" y2="{y:.1f}" '
            f'stroke="#e8edf3" stroke-width="1" />'
        )
        svg_parts.append(
            f'<text x="{padding_left - 12}" y="{y + 5:.1f}" font-size="15" text-anchor="end" fill="#8a95a3">{value:.0f}</text>'
        )

    max_labels = 12
    label_step = max(1, -(-n // max_labels))
    label_indices = list(range(0, n, label_step))
    if label_indices[-1] != n - 1:
        if n - 1 - label_indices[-1] < label_step / 2:
            label_indices[-1] = n - 1  # 最後のラベルが近すぎる場合は置き換える
        else:
            label_indices.append(n - 1)

    for i in label_indices:
        s = series[i]
        x = x_pos(i)
        label = f'{s["date"].month}/{s["date"].day}'
        svg_parts.append(
            f'<text x="{x:.1f}" y="{height - padding_bottom + 26}" font-size="14" '
            f'text-anchor="middle" fill="#8a95a3">{label}</text>'
        )

    # 実測値と平年値の境目に区切り線を入れる
    last_actual = max((i for i, s in enumerate(series) if s["is_actual"]), default=None)
    if last_actual is not None and last_actual < n - 1:
        bx = (x_pos(last_actual) + x_pos(last_actual + 1)) / 2
        svg_parts.append(
            f'<line x1="{bx:.1f}" y1="{padding_top}" x2="{bx:.1f}" y2="{baseline:.1f}" '
            f'stroke="#c3ccd8" stroke-width="1.5" stroke-dasharray="5 4" />'
        )
        svg_parts.append(
            f'<text x="{bx + 7:.1f}" y="{padding_top + 14}" font-size="13" fill="#a8b2c0">これ以降は平年値</text>'
        )

    area_path = (
        f'M {points[0][0]:.1f},{baseline:.1f} '
        + " ".join(f"L {x:.1f},{y:.1f}" for x, y in points)
        + f' L {points[-1][0]:.1f},{baseline:.1f} Z'
    )
    svg_parts.append(f'<path d="{area_path}" fill="url(#areaGrad)" />')

    svg_parts.append(
        f'<polyline points="{polyline_points}" fill="none" stroke="url(#lineGrad)" '
        f'stroke-width="3.5" stroke-linejoin="round" stroke-linecap="round" />'
    )

    # 点が多すぎるとつぶれて重くなるので、日数が少ないときだけ描く
    if n <= 90:
        for (x, y), s in zip(points, series):
            fill = "#ee7d2d" if s["is_actual"] else "#ffffff"
            svg_parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{fill}" stroke="#ee7d2d" stroke-width="2" />'
            )

    svg_parts.append("</svg>")
    return "".join(svg_parts)


@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    daily_series = None
    chart_svg = None
    reached_date = None
    summary = None
    station_name = None
    error = None
    form = request.form if request.method == "POST" else {}

    if request.method == "POST":
        prec_no = request.form["prec_no"]
        area_code = request.form["area_code"]
        start_date = date.fromisoformat(request.form["start_date"])
        base_temp = float(request.form.get("base_temp") or 0)
        correction = float(request.form.get("correction") or 0)
        end_mode = request.form.get("end_mode", "date")

        if end_mode == "target":
            target_temp = float(request.form.get("target_temp") or 0)
            if target_temp <= 0:
                error = "目標積算温度は0より大きい値を指定してください"
            else:
                daily_series, reached = get_cumulative_series_until_target(
                    prec_no, area_code, start_date, target_temp, base_temp, correction
                )
                if not reached:
                    error = "指定期間(3年)内に目標積算温度へ到達しませんでした。条件を見直してください。"
                elif daily_series:
                    reached_date = daily_series[-1]["date"]
                result = daily_series[-1]["cumulative"] if daily_series else 0
                chart_svg = build_chart_svg(daily_series)
        else:
            end_date = date.fromisoformat(request.form["end_date"])
            if start_date > end_date:
                error = "開始日は終了日より前の日付を指定してください"
            else:
                daily_series = get_cumulative_series(prec_no, area_code, start_date, end_date, base_temp, correction)
                result = daily_series[-1]["cumulative"] if daily_series else 0
                chart_svg = build_chart_svg(daily_series)

        if daily_series:
            summary = build_summary(daily_series)
            station_name = get_station_name(prec_no, area_code)

    return render_template(
        "index.html",
        result=result,
        daily_series=daily_series,
        chart_svg=chart_svg,
        reached_date=reached_date,
        summary=summary,
        station_name=station_name,
        stations=STATIONS,
        error=error,
        form=form,
    )


if __name__ == "__main__":
    app.run()