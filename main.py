import os
import sys
import math
import json
import uuid
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
    アルファ値（透明ピクセル）を最優先で非降水判定します。
    """
    if len(pixel) >= 4 and pixel[3] == 0:
        return "降水なし", 0.0, "#78909c", 0

    r, g, b = pixel[:3]
    if (r, g, b) == (180, 0, 104):  return "猛烈な雨", 80.0, "#ab47bc", 6
    if (r, g, b) == (255, 0, 0):    return "非常に激しい雨", 50.0, "#e53935", 5
    if (r, g, b) == (255, 106, 0):  return "激しい雨", 30.0, "#f57c00", 4
    if (r, g, b) == (255, 216, 0):  return "強い雨", 20.0, "#f5a623", 3
    if (r, g, b) == (0, 70, 255):   return "やや強い雨", 10.0, "#1e88e5", 2
    if (r, g, b) == (0, 170, 255):  return "雨", 5.0, "#29b6f6", 1
    if (r, g, b) == (100, 200, 255): return "弱雨", 1.0, "#4dd0e1", 0
    if (r, g, b) == (200, 230, 255): return "わずかな降水", 0.5, "#90a4ae", 0
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

    title_text = "↓棒グラフ: 時間雨量 [mm/h]" + " " * 5 + "折れ線グラフ: 積算雨量 [mm]↓"

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
    気象庁N2タイルの混在データから正確に1時間間隔（15時間分）を抽出して雨量予測を取得します。
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        url_target = "https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N2.json"
        res = requests.get(url_target, headers=headers, timeout=10)
        if res.status_code != 200:
            return 0.0, 0.0, [0.0]*15, ""
        
        target_times = res.json()
        xtile, ytile, px, py = latlon_to_tile(lat, lon, zoom)
        
        if not target_times:
            return 0.0, 0.0, [0.0]*15, ""

        base_dt = parse_jma_time(target_times[0]["basetime"])
        hourly_targets = []
        target_hours = [base_dt + timedelta(hours=i) for i in range(1, 16)]
        
        for th in target_hours:
            best_match = None
            min_diff = float("inf")
            for t in target_times:
                v_dt = parse_jma_time(t["validtime"])
                diff = abs((v_dt - th).total_seconds())
                if diff < min_diff:
                    min_diff = diff
                    best_match = t
            if best_match and min_diff < 1800:
                hourly_targets.append(best_match)

        hourly_rain_list = []
        for target in hourly_targets:
            basetime = target["basetime"]
            validtime = target["validtime"]
            tile_url = f"https://www.jma.go.jp/bosai/jmatile/data/nowc/{basetime}/none/{validtime}/surf/hrpns/{zoom}/{xtile}/{ytile}.png"
            
            t_res = requests.get(tile_url, headers=headers, timeout=5)
            if t_res.status_code == 200:
                img = Image.open(BytesIO(t_res.content)).convert("RGBA")
                pixel_color = img.getpixel((px, py))
                _, rain_val, _, _ = rgb_to_rainfall(pixel_color)
                hourly_rain_list.append(rain_val)
            else:
                hourly_rain_list.append(0.0)
        
        while len(hourly_rain_list) < 15:
            hourly_rain_list.append(0.0)
            
        all_rain = [current_rain_val] + hourly_rain_list
        cum_3h = round(sum(all_rain[:4]), 1)
        cum_15h = round(sum(all_rain), 1)
        chart_url = generate_chart_url(hourly_rain_list, current_rain_val)
        
        return cum_3h, cum_15h, hourly_rain_list, chart_url
    except Exception:
        return 0.0, 0.0, [0.0]*15, ""

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
        target = target_times[2]
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
        _, cum_15h, _, chart_url = get_future_cumulative_rain_data(lat, lon, rain_val, zoom)
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
        _, _, _, chart_url = get_future_cumulative_rain_data(lat, lon, rain_val, zoom)
        formatted_text = f"<font color=\"{color_code}\"><b>{rain_desc}</b></font>"
        send_google_chat_card(webhook_url, lat, lon, "雨上がりの予感", formatted_text, ICON_RAINBOW, chart_url)
        save_state(0.0, 0, 0, "WEAK", last_evening_alert_date)

    else:
        save_state(rain_val, current_rain_val, last_notified_rank, last_notified_type, last_evening_alert_date)

    if now.hour == 17 and (0 <= now.minute <= 10) and not sent_amedes_in_this_run and last_evening_alert_date != today_str:
        _, cum_15h, _, chart_url = get_future_cumulative_rain_data(lat, lon, rain_val, zoom)
        if cum_15h >= NIGHT_RAIN_THRESHOLD:
            cum_15h_int = int(cum_15h)
            formatted_text = f"17～翌8時の積算雨量 <b>{cum_15h_int} mm</b>"
            send_google_chat_card(webhook_url, lat, lon, "今宵アメデス", formatted_text, ICON_NIGHT_RAIN, today_str)
            save_state(rain_val, current_rank, last_notified_rank, last_notified_type, today_str)

    print("Execution completed successfully.")

# =========================================================
# 6. 安全な実データAPI取得テスト関数（デバッグ結果をChatへ送信）
# =========================================================
import math
import os
import requests
from io import BytesIO
from PIL import Image
from datetime import datetime, timezone, timedelta

# 気象庁RGBカラーパレット（全24パターン統合定義）
JMA_COMPLETE_PALETTE = [
    ((242, 242, 255), 0.5, "わずかな降水"), ((235, 245, 255), 0.5, "わずかな降水"), ((200, 225, 255), 0.5, "わずかな降水"),
    ((160, 210, 255), 1.0, "弱雨"), ((128, 192, 255), 1.0, "弱雨"), ((175, 210, 240), 1.0, "弱雨"),
    ((33,  140, 255), 5.0, "雨"), ((65,  140, 255), 5.0, "雨"), ((110, 160, 240), 5.0, "雨"),
    ((0,   65,  255), 10.0, "やや強い雨"), ((0,   0,   255), 10.0, "やや強い雨"), ((90,  120, 240), 10.0, "やや強い雨"),
    ((250, 245, 0),   20.0, "強い雨"), ((255, 255, 0),   20.0, "強い雨"), ((245, 240, 110), 20.0, "強い雨"),
    ((255, 153, 0),   30.0, "激しい雨"), ((255, 165, 0),   30.0, "激しい雨"), ((250, 185, 100), 30.0, "激しい雨"),
    ((255, 40,  0),   50.0, "非常に激しい雨"), ((255, 0,   0),   50.0, "非常に激しい雨"), ((240, 130, 110), 50.0, "非常に激しい雨"),
    ((180, 0,   104), 80.0, "猛烈な雨"), ((210, 0,   170), 80.0, "猛烈な雨"), ((200, 120, 160), 80.0, "猛烈な雨"),
]

def local_parse_time(time_str):
    return datetime.strptime(time_str, "%Y%m%d%H%M%S")

def local_rgb_to_rainfall(pixel):
    if not pixel or len(pixel) < 3 or (len(pixel) >= 4 and pixel[3] == 0) or pixel[:3] == (255, 255, 255):
        return "降水なし", 0.0
    r, g, b = pixel[:3]
    min_dist = float("inf")
    best = ("降水なし", 0.0)
    for (pr, pg, pb), val, desc in JMA_COMPLETE_PALETTE:
        dist = math.sqrt((r - pr)**2 + (g - pg)**2 + (b - pb)**2)
        if dist < min_dist:
            min_dist = dist
            best = (desc, val)
    return best

def test_real_api_fetch():
    """
    rasrf/targetTimes.json の中身を推測・フィルター・検索処理を一切行わずに
    生のメタデータ（basetime, validtime, member, elements）としてそのまま全件出力します。
    """
    import os
    import requests

    webhook_url = os.environ.get("CHAT_WEBHOOK_URL")
    lat_str = os.environ.get("TARGET_LAT")
    lon_str = os.environ.get("TARGET_LON")
    if not webhook_url or not lat_str or not lon_str:
        print("Execution finished (Missing env vars).")
        return

    headers = {"User-Agent": "Mozilla/5.0"}
    logs = ["<b>🔍 rasrf/targetTimes.json 生データ全件ダンプ結果</b><br>"]

    try:
        url = "https://www.jma.go.jp/bosai/jmatile/data/rasrf/targetTimes.json"
        res = requests.get(url, headers=headers, timeout=5)
        
        if res.status_code == 200:
            data = res.json()
            logs.append(f"<b>総件数</b>: {len(data)} 件<br>")
            
            # 生のデータをそのまま全件フォーマット（一切の処理を行わない）
            for i, item in enumerate(data, start=1):
                b = item.get("basetime", "なし")
                v = item.get("validtime", "なし")
                m = item.get("member", "なし(未定義)")
                e = ",".join(item.get("elements", []))
                
                logs.append(f"[{i:03d}] Valid: <b>{v}</b> | Base: <code>{b}</code> | Member: <code>{m}</code> | Elem: <code>{e}</code>")
        else:
            logs.append(f"❌ JSON取得失敗: HTTP {res.status_code}")

    except Exception as e:
        logs.append(f"❌ 実行エラー: {e}")

    # メッセージ長制限のため上位50件を出力
    debug_text = "<br>".join(logs[:52])
    if len(logs) > 52:
        debug_text += f"<br>...他 {len(logs)-52} 件省略"

    lat, lon = float(lat_str), float(lon_str)
    send_google_chat_card(webhook_url, lat, lon, "🔬 生メタデータ調査ログ", debug_text, ICON_RAINY)
    print("Execution completed successfully.")

# =========================================================
# 7. スクリプト実行エントリーポイント
# =========================================================
if __name__ == "__main__":
    
    # ---------------------------------------------------------
    # 実行モード選択
    # ---------------------------------------------------------
    
    # 【本番運用モード】（時間・曜日ガードあり）
    # main()
    
    # 【テスト検証モード】（デバッグ結果をGoogle Chatへ直接送信）
    test_real_api_fetch()
