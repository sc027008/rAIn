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

STATE_FILE = "state.json"

# 夜間積算雨量（17時〜翌8時）の通知しきい値（mm）
NIGHT_RAIN_THRESHOLD = float(os.environ.get("NIGHT_RAIN_THRESHOLD", "15.0"))

# Google Noto Emoji
ICON_RAINY = "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/128/emoji_u2614.png"
ICON_RAINBOW = "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/128/emoji_u1f308.png"
ICON_NIGHT_RAIN = "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/128/emoji_u1f303.png"

# ---------------------------------------------------------
# 1. 稼働時間・休日の判定
# ---------------------------------------------------------
def is_operating_time():
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    if not (8 <= now.hour < 19): return False
    if now.weekday() == 6: return False
    if now.month == 1 and 1 <= now.day <= 3: return False
    return True

# ---------------------------------------------------------
# 2. 状態（state.json）の読み込み・保存・初期化
# ---------------------------------------------------------
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

# ---------------------------------------------------------
# 3. 座標計算・画像解析・予測積算雨量算出＆グラフURL生成
# ---------------------------------------------------------
def latlon_to_tile(lat, lon, zoom=10):
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    xtile = int((lon + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    pixel_x = int(((lon + 180.0) / 360.0 * n - xtile) * 256)
    pixel_y = int(((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n - ytile) * 256)
    return xtile, ytile, pixel_x, pixel_y

def rgb_to_rainfall(rgb):
    r, g, b = rgb[:3]
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

def generate_chart_url(hourly_rain_list):
    labels = [str(i + 1) for i in range(len(hourly_rain_list))]
    bar_colors = [get_color_for_value(val) for val in hourly_rain_list]
    
    cumulative_rain = []
    total = 0.0
    for r in hourly_rain_list:
        total += r
        cumulative_rain.append(round(total, 1))

    max_bar = max(hourly_rain_list) if hourly_rain_list else 0.0
    max_cum = cumulative_rain[-1] if cumulative_rain else 0.0

    steps = 5
    step_y1 = get_nice_step(max(max_bar * 1.35, 10.0), steps)
    y1_max = step_y1 * steps

    step_y2 = get_nice_step(max(max_cum * 1.15, 10.0), steps)
    y2_max = step_y2 * steps

    # ★ キャンバス上端（y=0）から絶対座標で描画するため見切れ・消失を完全防止
    draw_y_titles_js = """function(chart) {
        var ctx = chart.ctx;
        ctx.save();
        ctx.font = "bold 15px 'BIZ UDPGothic', sans-serif";
        ctx.fillStyle = "#111111";
        ctx.textAlign = "center";
        ctx.textBaseline = "top"; // 座標指定を文字の上端に設定
        
        var top = 5; // 画像の一番上から5pxの位置から書き始める
        var left = chart.chartArea.left; // 左軸とグラフの境界X座標
        var right = chart.chartArea.right; // 右軸とグラフの境界X座標
        
        // 左軸タイトル
        ctx.fillText("棒グラフ", left, top);
        ctx.fillText("時間雨量", left, top + 18);
        ctx.fillText("[mm/h]", left, top + 36);
        
        // 右軸タイトル
        ctx.fillText("折れ線グラフ", right, top);
        ctx.fillText("積算雨量", right, top + 18);
        ctx.fillText("[mm]", right, top + 36);
        
        ctx.restore();
    }"""

    chart_config = {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": [
                {
                    # ★ メインの折れ線（深緑）
                    "type": "line",
                    "label": "積算雨量(mm)",
                    "data": cumulative_rain,
                    "borderColor": "#2e7d32",
                    "borderWidth": 3.5,
                    "pointRadius": 0,
                    "pointHoverRadius": 0,
                    "fill": False,
                    "yAxisID": "y2",
                    "order": 0,  # 一番手前
                    "datalabels": {"display": False}
                },
                {
                    # ★ 白縁取り用の太い折れ線
                    "type": "line",
                    "label": "積算雨量_白縁取り",
                    "data": cumulative_rain,
                    "borderColor": "white",
                    "borderWidth": 7.5,
                    "pointRadius": 0,
                    "pointHoverRadius": 0,
                    "fill": False,
                    "yAxisID": "y2",
                    "order": 1,  # メイン線と棒グラフの間
                    "datalabels": {"display": False}
                },
                {
                    # 棒グラフ
                    "type": "bar",
                    "label": "時間雨量(mm/h)",
                    "data": hourly_rain_list,
                    "backgroundColor": bar_colors,
                    "yAxisID": "y1",
                    "order": 2,  # 一番奥
                    "datalabels": {
                        "display": "auto",
                        "anchor": "end",
                        "align": "end",
                        "offset": -2,
                        "color": "#111111",
                        "font": {"size": 14, "family": "Noto Sans", "weight": "bold"},
                        "formatter": "function(value) { return value >= 1.0 ? value : ''; }"
                    }
                }
            ]
        },
        "options": {
            "defaultFontFamily": "Noto Sans",
            "title": {"display": False},
            "legend": {"display": False},
            "layout": {
                "padding": {
                    "top": 60,   # ★3行の文字(51px)がギリギリ入る最小限の余白に圧縮
                    "left": 15,
                    "right": 15,
                    "bottom": 5
                }
            },
            "plugins": {
                "datalabels": {"display": True}
            },
            "scales": {
                "xAxes": [{
                    "gridLines": {
                        "display": False
                    },
                    "scaleLabel": {
                        "display": True,
                        "labelString": "時間後",
                        "fontSize": 18,               # ★スマホ向けに拡大
                        "fontColor": "#111111",
                        "fontFamily": "BIZ UDPGothic",
                        "fontStyle": "bold"
                    },
                    "ticks": {
                        "fontSize": 15,               # ★スマホ向けに拡大
                        "maxRotation": 0,
                        "minRotation": 0,
                        "fontColor": "#111111",
                        "fontFamily": "Noto Sans"
                    }
                }],
                "yAxes": [
                    {
                        "id": "y1",
                        "type": "linear",
                        "position": "left",
                        "ticks": {
                            "min": 0,
                            "max": y1_max,
                            "stepSize": step_y1,
                            "fontSize": 14,           # ★スマホ向けに拡大
                            "fontColor": "#111111",
                            "fontFamily": "Noto Sans"
                        }
                    },
                    {
                        "id": "y2",
                        "type": "linear",
                        "position": "right",
                        "ticks": {
                            "min": 0,
                            "max": y2_max,
                            "stepSize": step_y2,
                            "fontSize": 14,           # ★スマホ向けに拡大
                            "fontColor": "#111111",
                            "fontFamily": "Noto Sans"
                        },
                        "gridLines": {
                            "drawOnChartArea": True
                        }
                    }
                ]
            }
        },
        "plugins": [
            {
                "id": "yAxisTopTitles",
                "afterDraw": draw_y_titles_js
            }
        ]
    }
    encoded = urllib.parse.quote(json.dumps(chart_config))
    return f"https://quickchart.io/chart?c={encoded}&w=540&h=280&bkg=white&devicePixelRatio=3&f=BIZ+UDPGothic"

def get_future_cumulative_rain_data(lat, lon, zoom=10):
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        url_target = "https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N2.json"
        res = requests.get(url_target, headers=headers, timeout=10)
        if res.status_code != 200:
            return 0.0, 0.0, [], ""
        
        target_times = res.json()
        xtile, ytile, px, py = latlon_to_tile(lat, lon, zoom)
        
        hourly_rain_list = []
        for target in target_times[:15]:
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
        
        cum_3h = round(sum(hourly_rain_list[:3]), 1)
        cum_15h = round(sum(hourly_rain_list[:15]), 1)
        chart_url = generate_chart_url(hourly_rain_list)
        
        return cum_3h, cum_15h, hourly_rain_list, chart_url
    except Exception as e:
        print(f"⚠️ 予測積算データ取得エラー: {e}")
        return 0.0, 0.0, [], ""

# ---------------------------------------------------------
# 4. Google Chat カード送信処理（CardsV2）
# ---------------------------------------------------------
def send_google_chat_card(webhook_url, lat, lon, title_text, formatted_text, icon_url, chart_url=None):
    jma_url = f"https://www.jma.go.jp/bosai/kaikotan/#lat:{lat}/lon:{lon}/zoom:11"
    unique_card_id = f"rainAlert_{uuid.uuid4().hex[:8]}"
    
    widgets = [
        {
            "textParagraph": {
                "text": formatted_text
            }
        }
    ]
    
    if chart_url:
        widgets.append({
            "image": {
                "imageUrl": chart_url,
                "altText": "雨量予測グラフ"
            }
        })
        
    widgets.append({
        "buttonList": {
            "buttons": [
                {
                    "text": "<b>雨雲レーダーを開く</b>｜気象庁",
                    "color": {
                        # ★ 薄いブルーグレーの背景色を指定し、枠線を消去
                        "red": 0.94,
                        "green": 0.96,
                        "blue": 0.98,
                        "alpha": 1.0
                    },
                    "onClick": {
                        "openLink": {
                            "url": jma_url
                        }
                    }
                }
            ]
        }
    })
    
    card_payload = {
        "cardsV2": [
            {
                "cardId": unique_card_id,
                "card": {
                    "header": {
                        "title": title_text,
                        "imageUrl": icon_url,
                        "imageType": "SQUARE"
                    },
                    "sections": [
                        {
                            "widgets": widgets
                        }
                    ]
                }
            }
        ]
    }
    
    try:
        res = requests.post(webhook_url, json=card_payload, timeout=10)
        print(f"📡 送信ステータス: {res.status_code} ({title_text})")
    except Exception as e:
        print(f"❌ 送信エラー: {e}")

# ---------------------------------------------------------
# 5. メイン処理（本番用）
# ---------------------------------------------------------
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
        print("ℹ️ 稼働時間外のため処理をスキップします。")
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

    print(f"📊 現在: {rain_desc}(ランク{current_rank}) | 前回通知: {last_notified_type}(ランク{last_notified_rank}) | 朝一:{is_fresh_start}")

    sent_amedes_in_this_run = False

    if is_fresh_start and current_rank >= 1:
        print("ℹ️ 稼働開始時点で既に雨が降っているため、朝一の通知をスキップします。")
        save_state(rain_val, current_rank, current_rank, "RAINY", last_evening_alert_date)

    elif current_rank >= 1 and (last_notified_type != "RAINY" or current_rank > last_notified_rank):
        cum_3h, cum_15h, _, chart_url = get_future_cumulative_rain_data(lat, lon, zoom)
        
        val_str = str(rain_val) if rain_val < 1.0 else str(int(rain_val))
        cum_3h_str = str(cum_3h) if cum_3h < 1.0 else str(int(cum_3h))
        cum_15h_str = str(cum_15h) if cum_15h < 1.0 else str(int(cum_15h))
        
        formatted_text = (
            f"<font color=\"{color_code}\"><b>{rain_desc}</b> {val_str} mm/h</font><br>"
            f"<font color=\"#757575\">•今後3時間積算 {cum_3h_str} mm<br>•今後15時間積算 {cum_15h_str} mm</font>"
        )
        
        send_google_chat_card(webhook_url, lat, lon, "アメデス", formatted_text, ICON_RAINY, chart_url)
        save_state(rain_val, current_rank, current_rank, "RAINY", last_evening_alert_date)
        sent_amedes_in_this_run = True

    elif current_rank == 0 and last_notified_type == "RAINY":
        # ★ 雨上がりの予感でもグラフを取得して添付する
        _, _, _, chart_url = get_future_cumulative_rain_data(lat, lon, zoom)
        formatted_text = f"<font color=\"{color_code}\"><b>{rain_desc}</b></font>"
        send_google_chat_card(webhook_url, lat, lon, "雨上がりの予感", formatted_text, ICON_RAINBOW, chart_url)
        save_state(0.0, 0, 0, "WEAK", last_evening_alert_date)

    else:
        save_state(rain_val, current_rank, last_notified_rank, last_notified_type, last_evening_alert_date)

    if now.hour == 17 and (0 <= now.minute <= 10) and not sent_amedes_in_this_run and last_evening_alert_date != today_str:
        _, cum_15h, _, chart_url = get_future_cumulative_rain_data(lat, lon, zoom)
        
        if cum_15h >= NIGHT_RAIN_THRESHOLD:
            cum_15h_str = str(cum_15h) if cum_15h < 1.0 else str(int(cum_15h))
            formatted_text = f"17～翌8時の積算雨量 <b>{cum_15h_str} mm</b>"
            send_google_chat_card(webhook_url, lat, lon, "今夜アメデス", formatted_text, ICON_NIGHT_RAIN, chart_url)
            save_state(rain_val, current_rank, last_notified_rank, last_notified_type, today_str)

# ---------------------------------------------------------
# 6. 全通知テスト実行用関数
# ---------------------------------------------------------
def test_all_notifications():
    init_state_file()
    webhook_url = os.environ.get("CHAT_WEBHOOK_URL")
    lat = float(os.environ.get("TARGET_LAT", "35.681236"))
    lon = float(os.environ.get("TARGET_LON", "139.767125"))

    if not webhook_url:
        print("❌ エラー: CHAT_WEBHOOK_URL が設定されていません。")
        sys.exit(1)

    print("🧪 全3パターンの通知表示テストメッセージを送信中...")

    sample_rain = [2.0, 15.0, 35.0, 50.0, 25.0, 10.0, 5.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    sample_chart_url = generate_chart_url(sample_rain)

    text_amedes = (
        f"<font color=\"#f5a623\"><b>強い雨</b> 20 mm/h</font><br>"
        f"<font color=\"#757575\">•今後3時間積算 52 mm<br>•今後15時間積算 143 mm</font>"
    )
    send_google_chat_card(webhook_url, lat, lon, "アメデス", text_amedes, ICON_RAINY, sample_chart_url)

    text_weak = f"<font color=\"#78909c\"><b>降水なし</b></font>"
    send_google_chat_card(webhook_url, lat, lon, "雨上がりの予感", text_weak, ICON_RAINBOW, sample_chart_url) # ★ グラフ添付

    text_evening = f"17～翌8時の積算雨量 <b>143 mm</b>"
    send_google_chat_card(webhook_url, lat, lon, "今夜アメデス", text_evening, ICON_NIGHT_RAIN, sample_chart_url)

    print("✅ テスト送信が完了しました。Google Chatのメッセージをご確認ください。")

if __name__ == "__main__":
    # 【本番運用モード】（普段はこちらを有効化）
    # main()

    # 【テスト送信モード】（テスト時は上の main() の頭に # を付け、下の行の # を消してください）
    test_all_notifications()
