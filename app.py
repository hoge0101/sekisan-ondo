from flask import Flask, render_template, request, Response, send_from_directory
from werkzeug.exceptions import HTTPException
import requests
from bs4 import BeautifulSoup
from datetime import date, timedelta
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 検索エンジンに正規URLを伝えるために使う(環境変数で差し替え可)
SITE_URL = os.environ.get("SITE_URL", "https://sekisan-ondo.onrender.com/")


app = Flask(__name__)

# 起動時に観測地点一覧を読み込んでおく
with open(os.path.join(BASE_DIR, "stations.json"), "r", encoding="utf-8") as f:
    STATIONS = json.load(f)


# 気象庁への問い合わせ結果のキャッシュ。
# 同じ地点・月を何度も取りに行かずに済み、気象庁への負荷も抑えられる。
_INCOMPLETE_MONTH_TTL = 1800  # 観測途中の月は30分で捨てる
_CACHE_MAX_ENTRIES = 3000

_monthly_cache = {}  # (prec_no, area_code, year, month) -> (期限, データ)
_normal_cache = {}   # (prec_no, area_code, month) -> データ


def _month_is_complete(year, month):
    """その月の全日程がすでに過ぎているか(=データがもう増えないか)"""
    if month == 12:
        next_month_start = date(year + 1, 1, 1)
    else:
        next_month_start = date(year, month + 1, 1)
    return next_month_start <= date.today()


def get_monthly_records(prec_no, area_code, year, month):
    """指定した年月の日ごとの観測値(気温・降水量・天気)を返す(キャッシュ付き)"""
    year, month = int(year), int(month)
    key = (prec_no, area_code, year, month)

    cached = _monthly_cache.get(key)
    if cached is not None:
        expires_at, data = cached
        if expires_at is None or expires_at > time.time():
            return data

    data = fetch_monthly_records(prec_no, area_code, year, month)

    if len(_monthly_cache) >= _CACHE_MAX_ENTRIES:
        _monthly_cache.clear()
    # 終わった月のデータはもう変わらないので期限なしで持っておく
    expires_at = None if _month_is_complete(year, month) else time.time() + _INCOMPLETE_MONTH_TTL
    _monthly_cache[key] = (expires_at, data)
    return data


def get_monthly_data(prec_no, area_code, year, month):
    """日別の平均気温だけを {date: 気温} で返す。取得は get_monthly_records と共通"""
    return {
        d: rec["temp"]
        for d, rec in get_monthly_records(prec_no, area_code, year, month).items()
        if rec["temp"] is not None
    }


def get_normal_daily_temps(prec_no, area_code, month):
    """指定した月の日別平年値を返す(キャッシュ付き)。平年値は10年に一度しか更新されない"""
    month = int(month)
    key = (prec_no, area_code, month)

    if key in _normal_cache:
        return _normal_cache[key]

    data = fetch_normal_daily_temps(prec_no, area_code, month)

    if len(_normal_cache) >= _CACHE_MAX_ENTRIES:
        _normal_cache.clear()
    _normal_cache[key] = data
    return data


def _to_float(text):
    """数値に変換する。欠測(「///」「×」)や空欄は None にする"""
    try:
        return float(text)
    except (ValueError, TypeError):
        return None


def _to_precip(text):
    """降水量を数値にする。

    気象庁の記号では「--」は現象なし(=雨が降らなかった)で、欠測ではない。
    そのまま None にすると観測できなかった日と区別が付かなくなるので、0.0 として扱う。
    「0.0」は雨は降ったが0.5mmに満たなかった日を指す。
    """
    if text is not None and text.strip() == "--":
        return 0.0
    return _to_float(text)


def fetch_monthly_records(prec_no, area_code, year, month):
    """指定した年月の日ごとの観測値を返す。

    {date: {"temp": 平均気温, "precip": 降水量合計, "weather": 天気概況}}
    天気概況は官署だけが観測しているので、アメダスでは None になる。
    """
    # 観測地点コードが5桁なら官署(daily_s1.php)、4桁ならアメダス(daily_a1.php)
    if len(area_code) == 5:
        url = f"https://www.data.jma.go.jp/stats/etrn/view/daily_s1.php?prec_no={prec_no}&block_no={area_code}&year={year}&month={month}&day=1&view="
        temp_col, precip_col, weather_col = 6, 3, 19
    else:
        url = f"https://www.data.jma.go.jp/stats/etrn/view/daily_a1.php?prec_no={prec_no}&block_no={area_code}&year={year}&month={month}&day=1&view="
        temp_col, precip_col, weather_col = 4, 1, None

    response = requests.get(url, timeout=20)
    soup = BeautifulSoup(response.text, "html.parser")

    target_table = soup.find("table", class_="data2_s")
    if target_table is None:
        return {}

    rows = target_table.find_all("tr")

    result = {}
    for row in rows[3:]:
        cells = row.find_all(["td", "th"])
        cell_texts = [cell.get_text(strip=True) for cell in cells]
        if len(cell_texts) <= temp_col:
            continue
        try:
            day = int(cell_texts[0])
            d = date(int(year), int(month), day)
        except ValueError:
            continue  # 見出し行など

        weather = None
        if weather_col is not None and len(cell_texts) > weather_col:
            weather = cell_texts[weather_col].strip() or None

        result[d] = {
            "temp": _to_float(cell_texts[temp_col]),
            "precip": _to_precip(cell_texts[precip_col]),
            "weather": weather,
        }
    return result


def fetch_normal_daily_temps(prec_no, area_code, month):
    """指定した観測地点の、指定した月の日別平年値(平均気温)を取得する。{日: 気温}のdictを返す"""
    # 観測地点コードが5桁なら官署(nml_sfc_d.php)、4桁ならアメダス(nml_amd_d.php)
    if len(area_code) == 5:
        url = f"https://www.data.jma.go.jp/stats/etrn/view/nml_sfc_d.php?prec_no={prec_no}&block_no={area_code}&year=&month={month}&day=1&view="
    else:
        url = f"https://www.data.jma.go.jp/stats/etrn/view/nml_amd_d.php?prec_no={prec_no}&block_no={area_code}&year=&month={month}&day=1&view="
    response = requests.get(url, timeout=20)
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


# 都道府県・地方コード -> 気象庁の府県予報区コード
with open(os.path.join(BASE_DIR, "forecast_areas.json"), "r", encoding="utf-8") as f:
    FORECAST_AREAS = json.load(f)

_FORECAST_TTL = 1800  # 予報は3時間ごとに更新されるので30分で取り直す
_forecast_cache = {}  # prec_no -> (期限, {date: {...}})

# 天気コードの上1桁が基本の天気を表す。短縮表記を作れなかったときの控え
_WEATHER_BASE = {"1": "晴", "2": "くもり", "3": "雨", "4": "雪"}


def shorten_weather(text):
    """予報の文章を、表の1列に収まる短さにする。

    「くもり　夜　雨　所により　夜遅く　雷　を伴う」→「くもり夜雨」
    「所により」以降はその地域内の細かい違いの説明なので落とす。
    """
    if not text:
        return None
    normalized = text.replace("　", "")
    for marker in ("所により", "を伴う", "はじめ", "のち一時"):
        index = normalized.find(marker)
        if index > 0:
            normalized = normalized[:index]
    return normalized.strip() or None


def build_weather_labels(section):
    """3日予報の「コードと文章の対」から、コード→短縮表記の対応を作る"""
    labels = {}
    for series in section.get("timeSeries", []):
        for area in series.get("areas", []):
            codes = area.get("weatherCodes") or []
            texts = area.get("weathers") or []
            for code, text in zip(codes, texts):
                label = shorten_weather(text)
                if label and code not in labels:
                    labels[code] = label
    return labels


def get_forecast(prec_no):
    """その地方の週間予報を {date: {"weather": 天気, "rain_chance": 降水確率}} で返す。

    気象庁の予報は府県予報区の単位なので、観測地点そのものではなく
    その地点が属する地域の予報になる。降水量(mm)の予報は提供されていないため、
    代わりに降水確率(%)を持つ。
    """
    area = FORECAST_AREAS.get(prec_no)
    if not area:
        return {}

    cached = _forecast_cache.get(prec_no)
    if cached and cached[0] > time.time():
        return cached[1]

    try:
        data = requests.get(
            f"https://www.jma.go.jp/bosai/forecast/data/forecast/{area}.json", timeout=15
        ).json()
    except Exception:
        return {}

    # 天気コードの意味は、同じ応答に入っている3日予報の文章から読み取る。
    # 気象庁が公開していた対応表(telops.json)は現在取得できないため。
    labels = build_weather_labels(data[0])

    def label_for(code):
        return labels.get(code) or _WEATHER_BASE.get(code[:1])

    result = {}

    # 後ろのセクションほど広い期間(週間予報)を持つので、先に週間、あとから
    # 直近3日で上書きして、近い日ほど詳しい情報が残るようにする
    for section in reversed(data):
        for series in section.get("timeSeries", []):
            areas = series.get("areas") or []
            if not areas:
                continue
            first = areas[0]  # 最初のエリアがその予報区の代表
            for i, stamp in enumerate(series["timeDefines"]):
                d = date.fromisoformat(stamp[:10])
                entry = result.setdefault(d, {"weather": None, "rain_chance": None})

                codes = first.get("weatherCodes")
                if codes and i < len(codes):
                    entry["weather"] = label_for(codes[i]) or entry["weather"]

                pops = first.get("pops")
                if pops and i < len(pops) and pops[i] != "":
                    # 3日予報は6時間ごとに複数あるので、その日の最大を採る
                    pop = int(pops[i])
                    entry["rain_chance"] = pop if entry["rain_chance"] is None else max(entry["rain_chance"], pop)

    _forecast_cache[prec_no] = (time.time() + _FORECAST_TTL, result)
    return result


ANOMALY_YEARS = 5  # アノマリーを求めるのに使う直近の年数
_ANOMALY_WORKERS = 5  # 過去データをまとめて取りにいくときの同時接続数

_anomaly_cache = {}  # (prec_no, area_code, month) -> アノマリー(℃) または None


def _anomaly_target_years(month, years):
    """アノマリー計算に使う、観測が終わった直近years年分の年を返す"""
    this_year = date.today().year
    target_years = []
    year = this_year
    while len(target_years) < years and year > this_year - years - 5:
        if _month_is_complete(year, month):
            target_years.append(year)
        year -= 1
    return target_years


def get_monthly_anomaly(prec_no, area_code, month, years=ANOMALY_YEARS):
    """直近years年の同月実測が、日別平年値からどれだけ離れているかの平均(℃)を返す。

    平年値の基準期間(1991〜2020年)は中心が2005年頃のため、近年の気温より低めに出る。
    この差(アノマリー)を平年値に足すことで、季節変化の形は30年分の滑らかな平年値カーブを
    保ったまま、気温の水準だけを現在の気候に合わせられる。
    データが足りない場合はNoneを返す。
    """
    month = int(month)
    key = (prec_no, area_code, month)
    if key in _anomaly_cache:
        return _anomaly_cache[key]

    normal = get_normal_daily_temps(prec_no, area_code, month)
    if not normal:
        _anomaly_cache[key] = None
        return None

    target_years = _anomaly_target_years(month, years)

    # 5年分を順番に取ると待ち時間が積み上がるので、まとめて取りにいく
    if len(target_years) > 1:
        with ThreadPoolExecutor(max_workers=_ANOMALY_WORKERS) as pool:
            list(pool.map(
                lambda y: get_monthly_data(prec_no, area_code, y, month),
                target_years,
            ))

    diffs = []
    for year in target_years:
        actual = get_monthly_data(prec_no, area_code, year, month)
        paired = [
            temp - normal[d.day]
            for d, temp in actual.items()
            if d.day in normal
        ]
        if paired:
            diffs.append(sum(paired) / len(paired))

    anomaly = round(sum(diffs) / len(diffs), 2) if diffs else None

    if len(_anomaly_cache) >= _CACHE_MAX_ENTRIES:
        _anomaly_cache.clear()
    _anomaly_cache[key] = anomaly
    return anomaly


def get_daily_temperatures(prec_no, area_code, start_date, end_date, use_anomaly=False):
    """期間内の日ごとの実測気温と平年値を返す。
    {date: {"actual": 実測気温(昨日まで、それ以降はNone), "normal": 平年値(全ての日)}}
    use_anomaly=True のとき、平年値には近年のアノマリーを加算した値を入れる。
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

    short_term_offset = get_short_term_offset(prec_no, area_code) if use_anomaly else None

    result = {}
    d = start_date
    while d <= end_date:
        actual = all_temps.get(d)
        # 実測がある日は補正しない。アノマリーは直近の実測から求めた値なので、
        # 実測自身と比べると「平年よりどれだけ高いか」の比較が歪んでしまう。
        result[d] = {
            "actual": actual,
            "normal": lookup_normal(
                prec_no, area_code, d,
                use_anomaly and actual is None,
                short_term_offset,
            ),
        }
        d += timedelta(days=1)

    return result


SHORT_TERM_WINDOW = 3     # 短期アノマリーを求める直近の日数
SHORT_TERM_FADE_DAYS = 7  # 短期の影響がゼロになるまでの日数


def get_short_term_anomaly(prec_no, area_code, window=SHORT_TERM_WINDOW):
    """直近window日(昨日まで)の実測が、平年値からどれだけ離れていたかの平均(℃)。

    過去1年の検証では、翌日の気温を当てる誤差は3日窓が最小だった
    (東京 1.70 / 稚内 1.96 / 大阪 1.49℃。長期アノマリーのみだと 2.09 / 2.52 / 1.98℃)。
    """
    diffs = []
    d = date.today() - timedelta(days=1)
    # 欠測があっても遡れるよう、窓の2倍の日数まで探す
    for _ in range(window * 2):
        if len(diffs) >= window:
            break
        actual = get_monthly_data(prec_no, area_code, d.year, d.month).get(d)
        normal = get_normal_daily_temps(prec_no, area_code, d.month).get(d.day)
        if actual is not None and normal is not None:
            diffs.append(actual - normal)
        d -= timedelta(days=1)

    if len(diffs) < window:
        return None
    return sum(diffs) / len(diffs)


def get_short_term_offset(prec_no, area_code):
    """短期補正に使う「天気ぶん」(℃)を返す。

    直近の平年差には気候トレンドぶん(長期アノマリー)がすでに含まれているので、
    それを引いた残りだけを短期の上乗せとして扱う(引かないと二重計上になる)。
    """
    short_term = get_short_term_anomaly(prec_no, area_code)
    if short_term is None:
        return None

    long_term = get_monthly_anomaly(prec_no, area_code, date.today().month)
    if long_term is None:
        return None
    return round(short_term - long_term, 2)


def lookup_normal(prec_no, area_code, d, use_anomaly=False, short_term_offset=None):
    """その日の平年値を返す。use_anomaly=Trueなら近年のアノマリーを加算する。
    short_term_offsetを渡すと、直近の天候ぶんを日数に応じて薄めながら上乗せする。
    """
    normal = get_normal_daily_temps(prec_no, area_code, d.month).get(d.day)
    if normal is None or not use_anomaly:
        return normal

    anomaly = get_monthly_anomaly(prec_no, area_code, d.month)
    if anomaly is None:
        return normal

    value = normal + anomaly

    if short_term_offset:
        # 直近の天候の影響は先に行くほど薄れ、SHORT_TERM_FADE_DAYS日でゼロになる
        lead = (d - date.today()).days + 1
        weight = min(1.0, max(0.0, 1 - lead / SHORT_TERM_FADE_DAYS))
        value += weight * short_term_offset

    return round(value, 1)


def get_cumulative_series(prec_no, area_code, start_date, end_date, base_temp=0, correction=0, use_anomaly=False):
    """日ごとの実測気温・平年値・積算温度の推移をリストで返す。
    積算には実測値(昨日まで)があればそれを、無ければ平年値を使う。
    correction: 観測地点の気温と実際の設置場所の気温のズレを補正する一律オフセット(℃)
    use_anomaly: 平年値に近年のアノマリーを加算するか
    """
    daily_temps = get_daily_temperatures(prec_no, area_code, start_date, end_date, use_anomaly)

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


def get_cumulative_series_until_target(prec_no, area_code, start_date, target_temp, base_temp=0, correction=0, max_days=1095, use_anomaly=False):
    """開始日から積算温度がtarget_tempに達するまでの日ごとの推移を返す。
    (series, reached)のタプルを返す。max_days(既定3年)以内に達しなければreached=Falseになる。
    """
    today = date.today()
    yesterday = today - timedelta(days=1)
    short_term_offset = get_short_term_offset(prec_no, area_code) if use_anomaly else None

    series = []
    total = 0
    reached = False
    d = start_date
    for _ in range(max_days):
        actual = get_monthly_data(prec_no, area_code, d.year, d.month).get(d) if d <= yesterday else None
        # 実測がある日は補正しない(get_daily_temperaturesと同じ理由)
        normal = lookup_normal(
            prec_no, area_code, d,
            use_anomaly and actual is None,
            short_term_offset,
        )
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


# ページに表示するFAQ。構造化データ(JSON-LD)もここから組み立てるので、
# 表示とマークアップの内容が食い違わない。
FAQ = [
    {
        "q": "積算温度と有効積算温度の違いは何ですか？",
        "a": "積算温度は日平均気温をそのまま足した値です。有効積算温度は、"
             "生育がほとんど進まない温度(基準温度)を差し引いた分だけを足した値です。"
             "このツールでは、基準温度を未入力または0にすると積算温度、"
             "値を入れると有効積算温度になります。",
    },
    {
        "q": "基準温度は何度に設定すればよいですか？",
        "a": "作物や品種、生育段階によって異なります。都道府県の農業試験場や"
             "普及指導センターが公開している栽培指針に記載があることが多いので、"
             "そちらを確認してください。未入力の場合は0℃として、気温をそのまま積算します。",
    },
    {
        "q": "未来の日付も計算できますか？",
        "a": "できます。今日以降は実測値がまだ無いため、観測地点の平年値"
             "(1991〜2020年の日別平均気温)を使って計算します。アノマリー補正を有効にすると、"
             "近年の気温の傾向を反映して、より実態に近い推定になります。",
    },
    {
        "q": "目標の積算温度から到達日を予測できますか？",
        "a": "できます。終了の指定方法で「積算温度で指定」を選び、目標値を入力すると、"
             "開始日からその値に達する日を計算します。最大3年先まで探索します。",
    },
    {
        "q": "天気や降水量はどこまで表示されますか？",
        "a": "過去の日は観測値、今日から7日先までは気象庁の予報を表示し、それ以降は空欄です。"
             "降水量(mm)の予報は提供されていないため、未来の日は代わりに降水確率(%)を"
             "別の列に出しています。天気を観測しているのは気象台などの規模の大きい観測所だけなので、"
             "アメダスでは過去の天気は空欄になります。いずれも積算温度の計算には使っていません。",
    },
    {
        "q": "近くのアメダスが一覧に出てこないのはなぜですか？",
        "a": "アメダスには降水量や風向風速だけを観測し、気温を測っていない地点が多くあります。"
             "選んでも計算できないため、そうした地点はあらかじめ一覧から除いています。"
             "全国およそ1700地点のうち、気温を扱える約930地点を掲載しています。",
    },
]


def build_structured_data():
    """検索エンジンにページの内容を伝える構造化データ(JSON-LD)を組み立てる"""
    data = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebApplication",
                "name": "確かな積算温度計算(実測データ版)",
                "url": SITE_URL,
                "applicationCategory": "UtilityApplication",
                "operatingSystem": "Web",
                "inLanguage": "ja",
                "description": "気象庁の観測地点データから、指定期間の積算温度を計算します。"
                               "基準温度や気温補正の指定、目標の積算温度に達する日の予測にも対応。",
                "offers": {"@type": "Offer", "price": "0", "priceCurrency": "JPY"},
            },
            {
                "@type": "FAQPage",
                "mainEntity": [
                    {
                        "@type": "Question",
                        "name": item["q"],
                        "acceptedAnswer": {"@type": "Answer", "text": item["a"]},
                    }
                    for item in FAQ
                ],
            },
        ],
    }
    return json.dumps(data, ensure_ascii=False)


def get_station_name(prec_no, area_code):
    """「宗谷地方 稚内」のような表示用の地点名を返す"""
    pref = STATIONS.get(prec_no)
    if not pref:
        return None
    for station in pref["stations"]:
        if station["block_no"] == area_code:
            return f'{pref["name"]} {station["name"]}'
    return pref["name"]


def attach_weather(prec_no, area_code, series):
    """表示用に、日ごとの天気・降水量・降水確率を series に足す。

    昨日までは観測値(気温取得時のキャッシュを使うので追加の通信はしない)、
    今日以降は気象庁の予報を使う。予報は7日先までなので、それ以降は空になる。
    降水量(mm)の予報は提供されていないため、未来日は降水確率(%)を入れる。
    """
    if not series:
        return

    yesterday = date.today() - timedelta(days=1)
    forecast = get_forecast(prec_no)
    records = {}

    for row in series:
        d = row["date"]
        if d <= yesterday:
            key = (d.year, d.month)
            if key not in records:
                records[key] = get_monthly_records(prec_no, area_code, d.year, d.month)
            record = records[key].get(d) or {}
            row["weather"] = record.get("weather")
            row["precip"] = record.get("precip")
            row["rain_chance"] = None
            row["is_forecast"] = False
        else:
            predicted = forecast.get(d) or {}
            row["weather"] = predicted.get("weather")
            row["precip"] = None
            row["rain_chance"] = predicted.get("rain_chance")
            row["is_forecast"] = True


def build_anomaly_note(prec_no, area_code, series):
    """平年値を使った日について、適用した補正の内訳を表示用にまとめる"""
    months = sorted({s["date"].month for s in series if not s["is_actual"]})
    monthly = []
    for month in months:
        anomaly = get_monthly_anomaly(prec_no, area_code, month)
        if anomaly is not None:
            monthly.append({"month": month, "anomaly": anomaly})

    if not monthly:
        return None

    return {
        "monthly": monthly,
        "short_term": get_short_term_offset(prec_no, area_code),
        "short_term_window": SHORT_TERM_WINDOW,
        "fade_days": SHORT_TERM_FADE_DAYS,
    }


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
    """積算温度の推移を折れ線グラフのSVGとして描画する。

    (SVG文字列, 各点の座標リスト) を返す。座標はカーソルを重ねたときの
    ポップアップ表示に使うので、描画と同じ計算結果をそのまま渡す。
    """
    if not series:
        return "", []

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
        # 区切り線が右端に近いと右側に置いたラベルがはみ出すので、その場合は線の左に回す
        label = "これ以降は平年値"
        label_font = 13
        label_width = len(label) * label_font  # 全角なので1文字≒フォントサイズ
        if bx + 7 + label_width <= width - padding_right:
            label_x, anchor = bx + 7, "start"
        else:
            label_x, anchor = bx - 7, "end"
        svg_parts.append(
            f'<text x="{label_x:.1f}" y="{padding_top + 14}" font-size="{label_font}" '
            f'text-anchor="{anchor}" fill="#a8b2c0">{label}</text>'
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

    chart_points = [
        {
            "x": round(x, 1),
            "y": round(y, 1),
            "date": s["date"].isoformat(),
            "cumulative": s["cumulative"],
            "temp": s["actual"] if s["actual"] is not None else s["normal"],
            "is_actual": s["is_actual"],
        }
        for (x, y), s in zip(points, series)
    ]
    return "".join(svg_parts), chart_points


@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    daily_series = None
    chart_svg = None
    chart_points = []
    reached_date = None
    summary = None
    station_name = None
    anomaly_note = None
    error = None
    form = request.form if request.method == "POST" else {}

    if request.method == "POST":
        try:
            prec_no = request.form["prec_no"]
            area_code = request.form["area_code"]
            start_date = date.fromisoformat(request.form["start_date"])
            base_temp = float(request.form.get("base_temp") or 0)
            correction = float(request.form.get("correction") or 0)
            end_mode = request.form.get("end_mode", "date")
            use_anomaly = request.form.get("use_anomaly") == "1"

            if not area_code:
                # 画面側でも必須にしているが、素通りすると0℃という紛らわしい結果になるため
                error = "観測地点を選択してください"
            elif end_mode == "target":
                target_temp = float(request.form.get("target_temp") or 0)
                if target_temp <= 0:
                    error = "目標積算温度は0より大きい値を指定してください"
                else:
                    daily_series, reached = get_cumulative_series_until_target(
                        prec_no, area_code, start_date, target_temp, base_temp, correction,
                        use_anomaly=use_anomaly,
                    )
                    if not reached:
                        error = "指定期間(3年)内に目標積算温度へ到達しませんでした。条件を見直してください。"
                    elif daily_series:
                        reached_date = daily_series[-1]["date"]
                    result = daily_series[-1]["cumulative"] if daily_series else 0
                    chart_svg, chart_points = build_chart_svg(daily_series)
            else:
                end_date = date.fromisoformat(request.form["end_date"])
                if start_date > end_date:
                    error = "開始日は終了日より前の日付を指定してください"
                else:
                    daily_series = get_cumulative_series(
                        prec_no, area_code, start_date, end_date, base_temp, correction,
                        use_anomaly=use_anomaly,
                    )
                    result = daily_series[-1]["cumulative"] if daily_series else 0
                    chart_svg, chart_points = build_chart_svg(daily_series)

            if daily_series:
                attach_weather(prec_no, area_code, daily_series)
                summary = build_summary(daily_series)
                station_name = get_station_name(prec_no, area_code)
                if use_anomaly:
                    anomaly_note = build_anomaly_note(prec_no, area_code, daily_series)

        # 途中で失敗しても素のInternal Server Errorは出さず、いつもの画面のまま
        # 理由を伝える。入力内容も残るので、そのまま押し直せる。
        except requests.RequestException:
            app.logger.exception("気象庁からのデータ取得に失敗しました")
            error = (
                "気象庁のデータを取得できませんでした。"
                "気象庁のサイトが混み合っているのかもしれません。"
                "少し時間をおいて、もう一度お試しください。"
            )
            result, daily_series, chart_svg, chart_points = None, None, None, []
            reached_date, summary, station_name, anomaly_note = None, None, None, None
        except Exception:
            app.logger.exception("積算温度の計算に失敗しました")
            error = "計算中に問題が起きました。入力内容を確かめて、もう一度お試しください。"
            result, daily_series, chart_svg, chart_points = None, None, None, []
            reached_date, summary, station_name, anomaly_note = None, None, None, None

    return render_template(
        "index.html",
        result=result,
        daily_series=daily_series,
        chart_svg=chart_svg,
        chart_points=chart_points,
        reached_date=reached_date,
        summary=summary,
        station_name=station_name,
        anomaly_note=anomaly_note,
        anomaly_years=ANOMALY_YEARS,
        site_url=SITE_URL,
        faq=FAQ,
        structured_data=build_structured_data(),
        stations=STATIONS,
        error=error,
        form=form,
    )


@app.route("/manifest.webmanifest")
def manifest():
    """ホーム画面に追加したときのアプリ情報(PWA)"""
    body = json.dumps({
        "name": "確かな積算温度計算(実測データ版)",
        "short_name": "積算温度(実測)",
        "description": "気象庁の観測地点データから、指定期間の積算温度を計算します。",
        "lang": "ja",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#f4f6f9",
        "theme_color": "#2b6ca8",
        "icons": [
            {
                "src": "/static/icons/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": "/static/icons/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
            {
                # Androidが丸などに切り抜く用
                "src": "/static/icons/icon-maskable-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    }, ensure_ascii=False)
    return Response(body, mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    """PWAとしてインストールできるようにするためのService Worker。

    静的ファイルだけをキャッシュし、HTMLと計算(POST)は常にネットワークへ通す。
    HTMLをキャッシュすると古い計算結果が残ってしまうため。
    ルート直下から配信しないとサイト全体を制御できないので、staticではなくルートに置く。
    """
    body = """
const CACHE = "sekisan-v2";

self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// 静的ファイルはネットワークを優先し、取れたらキャッシュを更新する。
// キャッシュ優先にすると、CSSやアイコンを直しても一度訪れた端末に
// 新しいものが永久に届かなくなるため。
// キャッシュは通信できないときの控えとしてだけ使う。
self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;  // 計算はPOSTなので触らない

  const url = new URL(request.url);
  if (url.origin !== location.origin) return;
  if (!url.pathname.startsWith("/static/")) return;  // HTMLは常に最新を取りにいく

  event.respondWith(
    fetch(request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy));
        return response;
      })
      .catch(() => caches.match(request))
  );
});
""".strip()
    return Response(body, mimetype="application/javascript")


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(app.static_folder, "icons"), "favicon.ico", mimetype="image/x-icon"
    )


@app.route("/robots.txt")
def robots_txt():
    """クローラーには全ページを開放する(検索で見つけてもらうため)。
    気象庁への問い合わせはPOST時だけなので、クロールされても外部への負荷は増えない。
    """
    body = f"User-agent: *\nAllow: /\n\nSitemap: {SITE_URL.rstrip('/')}/sitemap.xml\n"
    return Response(body, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    """1ページだけのサイトなので、トップページのみを載せる"""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        "  <url>\n"
        f"    <loc>{SITE_URL}</loc>\n"
        "    <changefreq>daily</changefreq>\n"
        "  </url>\n"
        "</urlset>\n"
    )
    return Response(body, mimetype="application/xml")


@app.errorhandler(404)
def page_not_found(e):
    """住所を間違えたときも、ブラウザ素のそっけない画面ではなくアプリの画面を出す"""
    return render_template(
        "error.html",
        code=404,
        heading="ページが見つかりません",
        message="お探しのページは移動したか、URLが間違っている可能性があります。",
        site_url=SITE_URL,
    ), 404


@app.errorhandler(500)
@app.errorhandler(Exception)
def internal_error(e):
    """計算処理の外で想定外の例外が出たときの受け皿。

    トップページの計算中に起きた失敗は index() 側で拾って画面内に出すので、
    ここに来るのはそれ以外の想定外だけ。素のInternal Server Errorは見せない。
    """
    # HTTPException(404など)は各ハンドラ・既定の処理に任せる
    if isinstance(e, HTTPException):
        return e

    app.logger.exception("想定外のエラーが発生しました")
    return render_template(
        "error.html",
        code=500,
        heading="エラーが発生しました",
        message=(
            "処理の途中で問題が起きました。"
            "少し時間をおいて、もう一度お試しください。"
        ),
        site_url=SITE_URL,
    ), 500


if __name__ == "__main__":
    app.run()