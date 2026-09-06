import os
import sys
import math
import json
import uuid
import random
import requests
import urllib.parse
from datetime import datetime, timezone, timedelta
from PIL import Image
from io import BytesIO

# =========================================================
# 0. 定数・環境変数・各種アイコン設定
# =========================================================
STATE_FILE = "state.json"

# 夜間積算雨量（17時〜翌8時）の通知判定しきい値（mm）
NIGHT_RAIN_THRESHOLD = float(os.environ.get("NIGHT_RAIN_THRESHOLD", "15.0"))

# Google Noto Emoji アイコンURL
ICON_RAINY = "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/128/emoji_u2614.png"
ICON_RAINBOW = "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/128/emoji_u1f308.png"
ICON_NIGHT_RAIN = "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/128/emoji_u1f303.png"

# 気象庁雨雲タイルの全3種24パターンカラーパレット統合定義
JMA_24_PALETTE = [
    # パレット1 (標準)
    ((180, 0, 104), 80.0, "猛烈な雨"), ((255, 40, 0), 50.0, "非常に激しい雨"),
    ((255, 153, 0), 30.0, "激しい雨"), ((255, 245, 0), 20.0, "強い雨"),
    ((0, 65, 255), 10.0, "やや強い雨"), ((33, 140, 255), 5.0, "雨"),
    ((160, 210, 255), 1.0, "弱雨"), ((242, 242, 255), 0.5, "わずかな降水"),
    # パレット2 (中間)
    ((199, 64, 142), 80.0, "猛烈な雨"), ((255, 94, 64), 50.0, "非常に激しい雨"),
    ((255, 179, 64), 30.0, "激しい雨"), ((255, 248, 64), 20.0, "強い雨"),
    ((64, 113, 255), 10.0, "やや強い雨"), ((89, 169, 255), 5.0, "雨"),
    ((184, 222, 255), 1.0, "弱雨"), ((246, 246, 255), 0.5, "わずかな降水"),
    # パレット3 (パステル)
    ((217, 127, 179), 80.0, "猛烈な雨"), ((255, 147, 127), 50.0, "非常に激しい雨"),
    ((255, 204, 127), 30.0, "激しい雨"), ((255, 250, 127), 20.0, "強い雨"),
    ((127, 160, 255), 10.0, "やや強い雨"), ((144, 197, 255), 5.0, "雨"),
    ((207, 232, 255), 1.0, "弱雨"), ((248, 248, 255), 0.5, "わずかな降水"),
]

# =========================================================
# 1. 稼働時間・休日の判定ロジック
# =========================================================
def is_operating_time():
    """
    現在の日本時間が通知稼働時間内（8時〜18時59分、日曜除く、正月三箇日除く）か判定します。
    """
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    
    if not (8 <= now.hour < 19): 
        return False
    if now.weekday() == 6: 
        return False
    if now.month == 1 and 1 <= now.day <= 3: 
        return False
        
    return True

# =========================================================
# 2. 状態（state.json）の読み込み・保存・初期化
# =========================================================
def save_state(rain_val, current_rank, last_notified_rank, last_notified_type, last_evening_alert_date=""):
    jst = timezone(timedelta(hours=9))
    data = {
        "last_rain_val": rain_val,
        "last_rank": current_rank,
        "last_notified_rank": last_notified_rank,
        "last_notified_type": last_notified_type,
        "last_evening_alert_date": last_evening_alert_date,
        "last_updated": datetime.now(jst).isoformat()
    }
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)

def init_state_file():
    if not os.path.exists(STATE_FILE):
        save_state(0.0, 0, 0, "NONE", "")

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                last_time_str = data.get("last_updated", "")
                last_evening_alert_date = data.get("last_evening_alert_date", "")
                
                if last_time_str:
                    last_time = datetime.fromisoformat(last_time_str)
                    jst = timezone(timedelta(hours=9))
                    if (datetime.now(jst) - last_time).total_seconds() > 3600:
                        return 0.0, 0, 0, "NONE", last_evening_alert_date, True
                
                return (
                    float(data.get("last_rain_val", 0.0)),
                    int(data.get("last_rank", 0)),
                    int(data.get("last_notified_rank", 0)),
                    data.get("last_notified_type", "NONE"),
                    last_evening_alert_date,
                    False
                )
        except Exception:
            return 0.0, 0, 0, "NONE", "", True
    return 0.0, 0, 0, "NONE", "", True

# =========================================================
# 3. 座標計算・画像解析・予測データ算出＆グラフURL生成
# =========================================================
def latlon_to_tile(lat, lon, zoom=10):
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    xtile = int((lon + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    pixel_x = int(((lon + 180.0) / 360.0 * n - xtile) * 256)
    pixel_y = int(((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n - ytile) * 256)
    return xtile, ytile, pixel_x, pixel_y

def rgb_to_rainfall(pixel):
    """
    気象庁雨雲タイルのピクセル色から雨量(mm/h)と降雨ランクを判定します。
    透明ピクセルおよび白背景を除外し、全24パターンのパレットから最も近い色を探索します。
    """
    if not pixel or len(pixel) < 3 or (len(pixel) >= 4 and pixel[3] == 0):
        return "降水なし", 0.0, "#78909c", 0

    r, g, b = pixel[:3]
    if (r, g, b) == (255, 255, 255):
        return "降水なし", 0.0, "#78909c", 0

    min_dist = float("inf")
    best_desc, best_val = "降水なし", 0.0

    for (pr, pg, pb), val, desc in JMA_24_PALETTE:
        dist = math.sqrt((r - pr)**2 + (g - pg)**2 + (b - pb)**2)
        if dist < min_dist:
            min_dist = dist
            best_desc, best_val = desc, val

    if min_dist > 45.0:
        return "降水なし", 0.0, "#78909c", 0

    if best_val >= 80.0: return best_desc, best_val, "#ab47bc", 6
    if best_val >= 50.0: return best_desc, best_val, "#e53935", 5
    if best_val >= 30.0: return best_desc, best_val, "#f57c00", 4
    if best_val >= 20.0: return best_desc, best_val, "#f5a623", 3
    if best_val >= 10.0: return best_desc, best_val, "#1e88e5", 2
    if best_val >= 5.0:  return best_desc, best_val, "#29b6f6", 1
    if best_val >= 1.0:  return best_desc, best_val, "#4dd0e1", 0
    if best_val >= 0.5:  return best_desc, best_val, "#90a4ae", 0

    return "降水なし", 0.0, "#78909c", 0

def get_color_for_value(val):
    if val >= 80.0: return "#ab47bc"
    if val >= 50.0: return "#e53935"
    if val >= 30.0: return "#f57c00"
    if val >= 20.0: return "#f5a623"
    if val >= 10.0: return "#1e88e5"
    if val >= 5.0:  return "#29b6f6"
    if val >= 1.0:  return "#4dd0e1"
    if val > 0.0:   return "#90a4ae"
    return "#e0e0e0"

def get_nice_step(raw_max, steps=5):
    raw_step = raw_max / steps
    nice_steps = [1, 2, 5, 10, 15, 20, 25, 30, 40, 50, 60, 75, 100, 125, 150, 200, 250, 300, 400, 500, 1000]
    for n in nice_steps:
        if n >= raw_step:
            return n
    return math.ceil(raw_step)

def generate_chart_url(hourly_rain_list, current_rain_val=0.0):
    all_rain = [current_rain_val] + hourly_rain_list
    labels = [str(i) for i in range(len(all_rain))]
    bar_colors = [get_color_for_value(val) for val in all_rain]
    datalabel_display = [val >= 0.5 for val in all_rain]
    
    cumulative_rain = []
    total = 0.0
    for r in all_rain:
        total += r
        cumulative_rain.append(round(total, 1))

    max_bar = max(all_rain) if all_rain else 0.0
    max_cum = cumulative_rain[-1] if cumulative_rain else 0.0

    steps = 5
    step_y1 = get_nice_step(max(max_bar * 1.35, 10.0), steps)
    y1_max = step_y1 * steps

    step_y2 = get_nice_step(max(max_cum * 1.15, 10.0), steps)
    y2_max = step_y2 * steps

    title_text = "↓棒グラフ: 時間雨量 [mm/h]" + " " * 6 + "折れ線グラフ: 積算雨量 [mm]↓"

    chart_config = {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": [
                {
                    "type": "line",
                    "label": "時間雨量ラベル用ダミー",
                    "data": all_rain,
                    "borderColor": "transparent",
                    "backgroundColor": "transparent",
                    "pointRadius": 0,
                    "yAxisID": "y1",
                    "order": 0,
                    "datalabels": {
                        "display": datalabel_display,
                        "anchor": "end",
                        "align": "end",
                        "offset": -2,
                        "color": "#111111",
                        "font": {"size": 20, "family": "LINE Seed JP", "weight": "bold"},
                        "textStrokeColor": "#ffffff",
                        "textStrokeWidth": 4
                    }
                },
                {
                    "type": "line",
                    "label": "積算雨量(mm)",
                    "data": cumulative_rain,
                    "borderColor": "#7B1FA2",
                    "borderWidth": 4,
                    "pointRadius": 0,
                    "fill": True,
                    "backgroundColor": "rgba(123, 31, 162, 0.08)",
                    "yAxisID": "y2",
                    "order": 1,
                    "datalabels": {"display": False}
                },
                {
                    "type": "line",
                    "label": "積算雨量_白縁取り",
                    "data": cumulative_rain,
                    "borderColor": "rgba(255, 255, 255, 0.7)",
                    "borderWidth": 10,
                    "pointRadius": 0,
                    "fill": False,
                    "yAxisID": "y2",
                    "order": 2,
                    "datalabels": {"display": False}
                },
                {
                    "type": "bar",
                    "label": "時間雨量(mm/h)",
                    "data": all_rain,
                    "backgroundColor": bar_colors,
                    "borderRadius": 6,
                    "yAxisID": "y1",
                    "order": 3,
                    "datalabels": {"display": False}
                }
            ]
        },
        "options": {
            "plugins": {
                "title": {
                    "display": True,
                    "text": title_text,
                    "color": "#111111",
                    "font": {"size": 19, "family": "Noto Sans CJK JP", "weight": "bold"},
                    "padding": 12
                },
                "legend": {"display": False},
                "datalabels": {"display": True}
            },
            "layout": {"padding": {"top": 5, "left": 10, "right": 10, "bottom": 5}},
            "scales": {
                "x": {
                    "grid": {"display": False},
                    "title": {"display": True, "text": "時間後", "color": "#111111", "font": {"size": 19, "family": "Noto Sans CJK JP", "weight": "bold"}},
                    "ticks": {"color": "#111111", "font": {"size": 18, "family": "Noto Sans CJK JP"}, "maxRotation": 0}
                },
                "y1": {
                    "type": "linear",
                    "position": "left",
                    "min": 0,
                    "max": y1_max,
                    "ticks": {"stepSize": step_y1, "color": "#111111", "font": {"size": 19, "family": "LINE Seed JP"}},
                    "grid": {"color": "#bdbdbd"},
                    "border": {"dash": [2, 3]}
                },
                "y2": {
                    "type": "linear",
                    "position": "right",
                    "min": 0,
                    "max": y2_max,
                    "ticks": {"stepSize": step_y2, "color": "#111111", "font": {"size": 19, "family": "LINE Seed JP"}},
                    "grid": {"drawOnChartArea": True, "color": "#bdbdbd"},
                    "border": {"dash": [2, 3]}
                }
            }
        }
    }

    try:
        payload = {"version": "4", "chart": chart_config, "width": 600, "height": 300, "backgroundColor": "white", "devicePixelRatio": 3}
        res = requests.post("https://quickchart.io/chart/create", json=payload, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data.get("success") and "url" in data:
                return data["url"]
    except Exception:
        pass

    compact_json = json.dumps(chart_config, separators=(',', ':'))
    encoded = urllib.parse.quote(compact_json)
    return f"https://quickchart.io/chart?v=4&c={encoded}&w=600&h=300&bkg=white&devicePixelRatio=3&f=Noto+Sans+CJK+JP"

# =========================================================
# 4. データ取得・カード構築・送信処理
# =========================================================
def parse_jma_time(time_str):
    return datetime.strptime(time_str, "%Y%m%d%H%M%S")

def get_future_cumulative_rain_data(lat, lon, current_rain_val=0.0, zoom=10):
    """
    1コマごとに HTTP ステータスコード（200, 404, 500等）を個別保持し、
    正常取得(200)とエラー（通信障害・不整合）の原因を明確に分離します。
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    details = []

    try:
        url_target = "https://www.jma.go.jp/bosai/jmatile/data/rasrf/targetTimes.json"
        res = requests.get(url_target, headers=headers, timeout=10)
        
        if res.status_code != 200:
            for idx in range(1, 16):
                details.append({
                    "idx": idx,
                    "validtime": "----",
                    "status_code": res.status_code,
                    "rain_val": None,
                    "status_desc": f"メタデータ取得失敗 (HTTP {res.status_code})"
                })
            return 0.0, 0.0, [0.0]*15, "", details

        raw_data = res.json()
        xtile, ytile, px, py = latlon_to_tile(lat, lon, zoom)
        
        if not raw_data:
            for idx in range(1, 16):
                details.append({
                    "idx": idx,
                    "validtime": "----",
                    "status_code": "EMPTY",
                    "rain_val": None,
                    "status_desc": "メタデータが空データ"
                })
            return 0.0, 0.0, [0.0]*15, "", details

        valid_frames = [
            item for item in raw_data 
            if "rasrf" in item.get("elements", []) and item.get("basetime") != item.get("validtime")
        ]
        valid_frames.sort(key=lambda x: x["validtime"])

        target_frames = valid_frames[:15]
        hourly_rain_list = []

        for idx, item in enumerate(target_frames, start=1):
            basetime = item["basetime"]
            validtime = item["validtime"]
            member = item.get("member", "none")
            tile_url = f"https://www.jma.go.jp/bosai/jmatile/data/rasrf/{basetime}/{member}/{validtime}/surf/rasrf/{zoom}/{xtile}/{ytile}.png"
            
            try:
                t_res = requests.get(tile_url, headers=headers, timeout=5)
                if t_res.status_code == 200:
                    img = Image.open(BytesIO(t_res.content)).convert("RGBA")
                    pixel_color = img.getpixel((px, py))
                    _, rain_val, _, _ = rgb_to_rainfall(pixel_color)
                    hourly_rain_list.append(rain_val)
                    details.append({
                        "idx": idx,
                        "validtime": validtime,
                        "status_code": 200,
                        "rain_val": rain_val,
                        "status_desc": f"HTTP 200 (正常取得: {rain_val}mm/h)"
                    })
                else:
                    hourly_rain_list.append(0.0)
                    details.append({
                        "idx": idx,
                        "validtime": validtime,
                        "status_code": t_res.status_code,
                        "rain_val": None,
                        "status_desc": f"HTTP {t_res.status_code} (画像エラー)"
                    })
            except Exception as req_err:
                hourly_rain_list.append(0.0)
                details.append({
                    "idx": idx,
                    "validtime": validtime,
                    "status_code": "EXCEPT",
                    "rain_val": None,
                    "status_desc": f"通信例外 ({type(req_err).__name__})"
                })

        while len(hourly_rain_list) < 15:
            idx = len(hourly_rain_list) + 1
            hourly_rain_list.append(0.0)
            details.append({
                "idx": idx,
                "validtime": "----",
                "status_code": "NODATA",
                "rain_val": None,
                "status_desc": "該当時間帯のデータ定義なし"
            })
            
        all_rain = [current_rain_val] + hourly_rain_list
        cum_3h = round(sum(all_rain[:4]), 1)
        cum_15h = round(sum(all_rain), 1)
        chart_url = generate_chart_url(hourly_rain_list, current_rain_val)
        
        return cum_3h, cum_15h, hourly_rain_list, chart_url, details
    except Exception as e:
        for idx in range(1, 16):
            details.append({
                "idx": idx,
                "validtime": "----",
                "status_code": "FATAL",
                "rain_val": None,
                "status_desc": f"致命的エラー ({e})"
            })
        return 0.0, 0.0, [0.0]*15, "", details

def send_google_chat_card(webhook_url, lat, lon, title_text, formatted_text, icon_url, chart_url=None):
    jma_url = f"https://www.jma.go.jp/bosai/kaikotan/#lat:{lat}/lon:{lon}/zoom:11"
    unique_card_id = f"rainAlert_{uuid.uuid4().hex[:8]}"
    
    widgets = [{"textParagraph": {"text": formatted_text}}]
    
    if chart_url:
        widgets.append({
            "image": {
                "imageUrl": chart_url,
                "altText": "雨量予測グラフ",
                "onClick": {"openLink": {"url": chart_url}}
            }
        })
        
    widgets.append({
        "buttonList": {
            "buttons": [{
                "text": "<b>雨雲レーダーを開く</b>｜気象庁",
                "color": {"red": 0.82, "green": 0.90, "blue": 0.98, "alpha": 1.0},
                "onClick": {"openLink": {"url": jma_url}}
            }]
        }
    })
    
    card_payload = {
        "cardsV2": [{
            "cardId": unique_card_id,
            "card": {
                "header": {"title": title_text, "imageUrl": icon_url, "imageType": "SQUARE"},
                "sections": [{"widgets": widgets}]
            }
        }]
    }
    
    try:
        requests.post(webhook_url, json=card_payload, timeout=10)
    except Exception:
        pass

# =========================================================
# 5. メインロジック（本番定期実行用）
# =========================================================
def main():
    webhook_url = os.environ.get("CHAT_WEBHOOK_URL")
    lat_str = os.environ.get("TARGET_LAT")
    lon_str = os.environ.get("TARGET_LON")

    if not webhook_url or not lat_str or not lon_str:
        sys.exit(1)

    lat = float(lat_str)
    lon = float(lon_str)

    init_state_file()

    if not is_operating_time():
        if load_state()[0] > 0:
            save_state(0.0, 0, 0, "NONE", load_state()[4])
        sys.exit(0)

    last_rain_val, last_rank, last_notified_rank, last_notified_type, last_evening_alert_date, is_fresh_start = load_state()
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        elem_res = requests.get("https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N1.json", headers=headers, timeout=10)
        target_times = elem_res.json()
        
        # basetime == validtime の実況フレーム（現在値）を正確に抽出
        target = next((t for t in target_times if t.get("basetime") == t.get("validtime")), target_times[0] if target_times else None)
        if not target:
            sys.exit(1)
            
        basetime = target["basetime"]
        validtime = target["validtime"]
    except Exception:
        sys.exit(1)

    zoom = 10
    xtile, ytile, px, py = latlon_to_tile(lat, lon, zoom)
    url = f"https://www.jma.go.jp/bosai/jmatile/data/nowc/{basetime}/none/{validtime}/surf/hrpns/{zoom}/{xtile}/{ytile}.png"

    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            rain_desc, rain_val, color_code, current_rank = "降水なし", 0.0, "#78909c", 0
        else:
            img = Image.open(BytesIO(res.content)).convert("RGBA")
            pixel_color = img.getpixel((px, py))
            rain_desc, rain_val, color_code, current_rank = rgb_to_rainfall(pixel_color)
    except Exception:
        sys.exit(1)

    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    today_str = now.strftime("%Y-%m-%d")

    sent_amedes_in_this_run = False

    if is_fresh_start and current_rank >= 1:
        save_state(rain_val, current_rank, current_rank, "RAINY", last_evening_alert_date)

    elif current_rank >= 1 and (last_notified_type != "RAINY" or current_rank > last_notified_rank):
        _, cum_15h, _, chart_url, _ = get_future_cumulative_rain_data(lat, lon, rain_val, zoom)
        val_str = str(rain_val) if rain_val < 1.0 else str(int(rain_val))
        cum_15h_int = int(cum_15h)
        
        formatted_text = (
            f"<font color=\"{color_code}\"><b>{rain_desc}</b> {val_str} mm/h</font><br>"
            f"<font color=\"#757575\">今後15時間積算 {cum_15h_int} mm</font>"
        )
        
        send_google_chat_card(webhook_url, lat, lon, "アメデス", formatted_text, ICON_RAINY, chart_url)
        save_state(rain_val, current_rank, current_rank, "RAINY", last_evening_alert_date)
        sent_amedes_in_this_run = True

    elif current_rank == 0 and last_notified_type == "RAINY":
        _, _, _, chart_url, _ = get_future_cumulative_rain_data(lat, lon, rain_val, zoom)
        formatted_text = f"<font color=\"{color_code}\"><b>{rain_desc}</b></font>"
        send_google_chat_card(webhook_url, lat, lon, "雨上がりの予感", formatted_text, ICON_RAINBOW, chart_url)
        save_state(0.0, 0, 0, "WEAK", last_evening_alert_date)

    else:
        save_state(rain_val, current_rain_val, last_notified_rank, last_notified_type, last_evening_alert_date)

    if now.hour == 17 and (0 <= now.minute <= 10) and not sent_amedes_in_this_run and last_evening_alert_date != today_str:
        _, cum_15h, _, chart_url, _ = get_future_cumulative_rain_data(lat, lon, rain_val, zoom)
        if cum_15h >= NIGHT_RAIN_THRESHOLD:
            cum_15h_int = int(cum_15h)
            formatted_text = f"17～翌8時の積算雨量 <b>{cum_15h_int} mm</b>"
            send_google_chat_card(webhook_url, lat, lon, "今宵アメデス", formatted_text, ICON_NIGHT_RAIN, today_str)
            save_state(rain_val, current_rank, last_notified_rank, last_notified_type, today_str)

    print("Execution completed successfully.")

# =========================================================
# 6. 原因切り分け用 強制データ取得＆Chat送信テスト関数
# =========================================================
def find_active_rain_location():
    """
    日本全国の主要候補地および雨雲タイルをランダムな順序で探索し、
    現在雨が降っている（rain_val > 0.0）地点の (地名, lat, lon) を返します。
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    
    candidate_spots = [
        ("札幌", 43.0618, 141.3545), ("函館", 41.7687, 140.7288), ("青森", 40.8244, 140.7400),
        ("秋田", 39.7186, 140.1024), ("仙台", 38.2682, 140.8694), ("新潟", 37.9161, 139.0364),
        ("金沢", 36.5613, 136.6562), ("東京", 35.6762, 139.6503), ("八丈島", 33.1112, 139.7902),
        ("静岡", 34.9756, 138.3828), ("名古屋", 35.1815, 136.9066), ("大阪", 34.6937, 135.5023),
        ("和歌山", 34.2260, 135.1675), ("鳥取", 35.5011, 134.2351), ("広島", 34.3853, 132.4553),
        ("高知", 33.5597, 133.5311), ("松山", 33.8416, 132.7657), ("福岡", 33.5902, 130.4017),
        ("長崎", 32.7503, 129.8777), ("鹿児島", 31.5966, 130.5571), ("奄美", 28.3772, 129.4950),
        ("那覇", 26.2124, 127.6809), ("石垣島", 24.3448, 124.1572)
    ]
    random.shuffle(candidate_spots)

    try:
        url = "https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N1.json"
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code != 200:
            return None, None, None

        target_times = res.json()
        target = next((t for t in target_times if t.get("basetime") == t.get("validtime")), None)
        if not target:
            return None, None, None

        basetime, validtime = target["basetime"], target["validtime"]

        for name, lat, lon in candidate_spots:
            xtile, ytile, px, py = latlon_to_tile(lat, lon, 10)
            tile_url = f"https://www.jma.go.jp/bosai/jmatile/data/nowc/{basetime}/none/{validtime}/surf/hrpns/10/{xtile}/{ytile}.png"
            t_res = requests.get(tile_url, headers=headers, timeout=3)
            
            if t_res.status_code == 200:
                img = Image.open(BytesIO(t_res.content)).convert("RGBA")
                pixel = img.getpixel((px, py))
                _, rain_val, _, _ = rgb_to_rainfall(pixel)
                if rain_val > 0.0:
                    return f"{name}周辺", lat, lon

        scan_tiles = [(901, 404), (905, 402), (895, 410), (890, 415), (910, 395)]
        random.shuffle(scan_tiles)

        for xtile, ytile in scan_tiles:
            tile_url = f"https://www.jma.go.jp/bosai/jmatile/data/nowc/{basetime}/none/{validtime}/surf/hrpns/10/{xtile}/{ytile}.png"
            t_res = requests.get(tile_url, headers=headers, timeout=3)
            if t_res.status_code == 200:
                img = Image.open(BytesIO(t_res.content)).convert("RGBA")
                width, height = img.size
                for py_idx in range(0, height, 16):
                    for px_idx in range(0, width, 16):
                        pixel = img.getpixel((px_idx, py_idx))
                        _, rain_val, _, _ = rgb_to_rainfall(pixel)
                        if rain_val > 0.0:
                            n = 2.0 ** 10
                            x = xtile + px_idx / 256.0
                            y = ytile + py_idx / 256.0
                            calc_lon = round((x / n) * 360.0 - 180.0, 4)
                            calc_lat = round(math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n)))), 4)
                            return f"雨域検出地点({calc_lat}, {calc_lon})", calc_lat, calc_lon

    except Exception as e:
        print(f"降雨エリア探索エラー: {e}")

    return None, None, None

def test_forced_notification():
    """
    雨が降っている地域を自動特定してデータ取得を行い、
    該当地域の気象庁Web GUIリンクを添えてGoogle Chatへ送信します。
    """
    webhook_url = os.environ.get("CHAT_WEBHOOK_URL")
    if not webhook_url:
        print("エラー: CHAT_WEBHOOK_URL が設定されていません。")
        return

    headers = {"User-Agent": "Mozilla/5.0"}
    print("=== 全国雨域自動スキャン開始 ===")

    location_name, lat, lon = find_active_rain_location()

    if not lat or not lon:
        print("現在、日本全国の主要監視エリアに降雨が検出されませんでした（全域晴れ/薄くもり）。")
        lat = float(os.environ.get("TARGET_LAT", "35.1815"))
        lon = float(os.environ.get("TARGET_LON", "136.9066"))
        location_name = "指定座標(降雨なし)"

    print(f"検証対象地点決定: {location_name} (緯度:{lat}, 経度:{lon})")

    rain_desc, rain_val, color_code = "降水なし", 0.0, "#78909c"
    try:
        elem_res = requests.get("https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N1.json", headers=headers, timeout=10)
        if elem_res.status_code == 200:
            target_times = elem_res.json()
            # basetime == validtime の実況フレーム（現在値）を正確に抽出
            target = next((t for t in target_times if t.get("basetime") == t.get("validtime")), None)
            if target:
                basetime, validtime = target["basetime"], target["validtime"]
                xtile, ytile, px, py = latlon_to_tile(lat, lon, 10)
                url = f"https://www.jma.go.jp/bosai/jmatile/data/nowc/{basetime}/none/{validtime}/surf/hrpns/10/{xtile}/{ytile}.png"
                res = requests.get(url, headers=headers, timeout=10)
                if res.status_code == 200:
                    img = Image.open(BytesIO(res.content)).convert("RGBA")
                    pixel_color = img.getpixel((px, py))
                    rain_desc, rain_val, color_code, _ = rgb_to_rainfall(pixel_color)
    except Exception as e:
        print(f"実況取得例外: {e}")

    cum_3h, cum_15h, hourly_rain_list, chart_url, details = get_future_cumulative_rain_data(lat, lon, rain_val, 10)
    jma_gui_url = f"https://www.jma.go.jp/bosai/kaikotan/#lat:{lat}/lon:{lon}/zoom:11"

    val_str = str(rain_val) if rain_val < 1.0 else str(int(rain_val))
    logs = [
        f"<b>【実降雨エリアデバッグ検証】</b>",
        f"<b>検証対象地域</b>: 📍 <b>{location_name}</b>",
        f"<b>現在地の状況</b>: <font color=\"{color_code}\"><b>{rain_desc}</b> {val_str} mm/h</font>",
        f"<b>気象庁Web GUI確認リンク</b>: <a href=\"{jma_gui_url}\">雨雲の動き(公式GUI)で画面照合</a><br>",
        f"<b>【15時間予測値およびHTTPレスポンス詳細】</b>"
    ]

    for d in details:
        idx = d["idx"]
        vt = d["validtime"]
        code = d["status_code"]
        rv = d["rain_val"]
        
        if code == 200:
            if rv is not None and rv > 0.0:
                logs.append(f"・+{idx:02d}h ({vt}): <font color=\"#2e7d32\">HTTP 200</font> | <b><font color=\"#ff0000\">{rv} mm/h</font></b>")
            else:
                logs.append(f"・+{idx:02d}h ({vt}): <font color=\"#2e7d32\">HTTP 200</font> | {rv} mm/h")
        else:
            logs.append(f"・+{idx:02d}h ({vt}): <font color=\"#e53935\"><b>HTTP {code}</b></font>")

    logs.append(f"<br><b>15時間積算雨量</b>: {int(cum_15h)} mm")
    formatted_text = "<br>".join(logs)

    send_google_chat_card(
        webhook_url=webhook_url,
        lat=lat,
        lon=lon,
        title_text=f"🌧️ 実降雨検証 ({location_name})",
        formatted_text=formatted_text,
        icon_url=ICON_RAINY,
        chart_url=chart_url
    )

    print(f"送信完了: {location_name} (lat:{lat}, lon:{lon}) の実降雨データをGoogle Chatへ送信しました。")

if __name__ == "__main__":
    test_forced_notification()
