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

# Google Noto Emoji アイコンURL（Google Chat カードのヘッダー用）
ICON_RAINY = "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/128/emoji_u2614.png"
ICON_RAINBOW = "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/128/emoji_u1f308.png"
ICON_NIGHT_RAIN = "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/128/emoji_u1f303.png"

# =========================================================
# 1. 稼働時間・休日の判定ロジック
# =========================================================
def is_operating_time():
    """
    現在の日本時間が通知稼働時間内（8時〜18時59分、日曜除く、正月三箇日除く）か判定します。
    ※ 稼働時間外は無駄な通知や不要なAPIリクエストを防止するために処理をスキップします。
    """
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    
    # 時間帯判定（8時〜18時台のみ稼働）
    if not (8 <= now.hour < 19): 
        return False
    # 曜日判定（日曜日＝6 は除外）
    if now.weekday() == 6: 
        return False
    # 正月三箇日（1月1日〜3日）は除外
    if now.month == 1 and 1 <= now.day <= 3: 
        return False
        
    return True

# =========================================================
# 2. 状態（state.json）の読み込み・保存・初期化
# =========================================================
def save_state(rain_val, current_rank, last_notified_rank, last_notified_type, last_evening_alert_date=""):
    """
    直近の雨量データおよび通知状態を JSON ファイルへ保存します。
    連続通知の防止や降雨ランクの上昇判定に使用します。
    """
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
    """
    状態保存ファイル（state.json）が存在しない場合に初期化生成します。
    """
    if not os.path.exists(STATE_FILE):
        save_state(0.0, 0, 0, "NONE", "")

def load_state():
    """
    保存された前回状態を読み込みます。
    前回の記録から1時間以上経過している場合は、状態が古いため自動的にフレッシュスタート（初期化）として扱います。
    """
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                last_time_str = data.get("last_updated", "")
                last_evening_alert_date = data.get("last_evening_alert_date", "")
                
                if last_time_str:
                    last_time = datetime.fromisoformat(last_time_str)
                    jst = timezone(timedelta(hours=9))
                    # 1時間以上経過している場合は新鮮なスタートとして扱う
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
    """
    緯度経度から気象庁タイル画像のピクセル座標（X, Y）を算出します。
    """
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    xtile = int((lon + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    pixel_x = int(((lon + 180.0) / 360.0 * n - xtile) * 256)
    pixel_y = int(((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n - ytile) * 256)
    return xtile, ytile, pixel_x, pixel_y

def rgb_to_rainfall(rgb):
    """
    気象庁雨雲タイルのRGBピクセル色から雨量(mm/h)と降雨ランク（0〜6）を判定します。
    """
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
    """
    雨量数値(mm/h)に応じた気象庁規定のバー表示カラーコードを返します。
    """
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
    """
    Y軸の目盛りがきれいな区切り数値（10, 15, 20, 25等）になるステップ値を計算します。
    """
    raw_step = raw_max / steps
    nice_steps = [1, 2, 5, 10, 15, 20, 25, 30, 40, 50, 60, 75, 100, 125, 150, 200, 250, 300, 400, 500, 1000]
    for n in nice_steps:
        if n >= raw_step:
            return n
    return math.ceil(raw_step)

def generate_chart_url(hourly_rain_list, current_rain_val=0.0):
    """
    0時間後（リアルタイムナウキャスト値）＋15時間予測データを合わせた複合グラフ（QuickChart API v4）のURLを生成します。
    """
    # 先頭に0時間後の雨量を結合し、全16データ（0〜15時間後）の配列を作成
    all_rain = [current_rain_val] + hourly_rain_list
    labels = [str(i) for i in range(len(all_rain))]
    bar_colors = [get_color_for_value(val) for val in all_rain]
    
    # 0.5mm未満（雨なし）のデータは datalabels 表示を False にしてグラフ上の「0」表記を非表示化
    datalabel_display = [val >= 0.5 for val in all_rain]
    
    # 累積雨量の配列を作成
    cumulative_rain = []
    total = 0.0
    for r in all_rain:
        total += r
        cumulative_rain.append(round(total, 1))

    # Y軸の最大値とステップ間隔を動的に調整
    max_bar = max(all_rain) if all_rain else 0.0
    max_cum = cumulative_rain[-1] if cumulative_rain else 0.0

    steps = 5
    step_y1 = get_nice_step(max(max_bar * 1.35, 10.0), steps)
    y1_max = step_y1 * steps

    step_y2 = get_nice_step(max(max_cum * 1.15, 10.0), steps)
    y2_max = step_y2 * steps

    # タイトルのスペース個数（左右軸の真上に見出しテキストを配置するための位置微調整）
    title_text = "↓棒グラフ: 時間雨量 [mm/h]" + " " * 5 + "折れ線グラフ: 積算雨量 [mm]↓"

    # Chart.js v4 規格の設定オブジェクト
    chart_config = {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": [
                # ----------------------------------------------------
                # レイヤー0 (最前面): ラベル専用（透明な線グラフ）
                # ----------------------------------------------------
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
                        # 重なっても読めるよう白フチを追加
                        "textStrokeColor": "#ffffff",
                        "textStrokeWidth": 4
                    }
                },
                # ----------------------------------------------------
                # レイヤー1: 積算雨量 (折れ線・メイン)
                # ----------------------------------------------------
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
                # ----------------------------------------------------
                # レイヤー2: 積算雨量 (折れ線・透過白フチ用)
                # ----------------------------------------------------
                {
                    "type": "line",
                    "label": "積算雨量_白縁取り",
                    "data": cumulative_rain,
                    "borderColor": "rgba(255, 255, 255, 0.7)", # 透過白フチ
                    "borderWidth": 10,
                    "pointRadius": 0,
                    "fill": False,
                    "yAxisID": "y2",
                    "order": 2,
                    "datalabels": {"display": False}
                },
                # ----------------------------------------------------
                # レイヤー3 (最背面): 時間雨量 (棒グラフ本体)
                # ----------------------------------------------------
                {
                    "type": "bar",
                    "label": "時間雨量(mm/h)",
                    "data": all_rain,
                    "backgroundColor": bar_colors,
                    "borderRadius": 6,
                    "yAxisID": "y1",
                    "order": 3,
                    "datalabels": {"display": False} # ラベルはダミーで描画するため非表示
                }
            ]
        },
        "options": {
            "plugins": {
                "title": {
                    "display": True,
                    "text": title_text,
                    "color": "#111111",
                    "font": {"size": 19, "family": "LINE Seed JP", "weight": "bold"},
                    "padding": 12
                },
                "legend": {"display": False},
                "datalabels": {"display": True}
            },
            "layout": {
                "padding": {
                    "top": 5,
                    "left": 10,
                    "right": 10,
                    "bottom": 5
                }
            },
            "scales": {
                "x": {
                    "grid": {"display": False},
                    "title": {
                        "display": True,
                        "text": "時間後",
                        "color": "#111111",
                        "font": {"size": 19, "family": "LINE Seed JP", "weight": "bold"}
                    },
                    "ticks": {
                        "color": "#111111",
                        "font": {"size": 18, "family": "LINE Seed JP"},
                        "maxRotation": 0
                    }
                },
                "y1": {
                    "type": "linear",
                    "position": "left",
                    "min": 0,
                    "max": y1_max,
                    "ticks": {
                        "stepSize": step_y1,
                        "color": "#111111",
                        "font": {"size": 19, "family": "LINE Seed JP"}
                    },
                    "grid": {
                        "color": "#bdbdbd",
                        "borderDash": [6, 6]
                    }
                },
                "y2": {
                    "type": "linear",
                    "position": "right",
                    "min": 0,
                    "max": y2_max,
                    "ticks": {
                        "stepSize": step_y2,
                        "color": "#111111",
                        "font": {"size": 19, "family": "LINE Seed JP"}
                    },
                    "grid": {
                        "drawOnChartArea": True,
                        "color": "#bdbdbd",
                        "borderDash": [6, 6]
                    }
                }
            }
        }
    }

    try:
        # payload に "version": "4" を明示指定して Short URL を発行（Google Chat URL長制限の回避）
        payload = {
            "version": "4",
            "chart": chart_config,
            "width": 600,
            "height": 300,
            "backgroundColor": "white",
            "devicePixelRatio": 3
        }
        res = requests.post("https://quickchart.io/chart/create", json=payload, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data.get("success") and "url" in data:
                return data["url"]
    except Exception as e:
        print(f"⚠️ Short URL発行失敗(GETへフォールバック): {e}")

    # POST通信失敗時のフォールバック処理（URLエンコードGET送信）
    compact_json = json.dumps(chart_config, separators=(',', ':'))
    encoded = urllib.parse.quote(compact_json)
    return f"https://quickchart.io/chart?v=4&c={encoded}&w=600&h=300&bkg=white&devicePixelRatio=3&f=LINE+Seed+JP"

# =========================================================
# 4. データ取得・カード構築・送信処理
# =========================================================
def get_future_cumulative_rain_data(lat, lon, current_rain_val=0.0, zoom=10):
    """
    気象庁APIから今後15時間分の雨量予測データを取得し、積算雨量とグラフURLを生成して返します。
    """
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
        
        all_rain = [current_rain_val] + hourly_rain_list
        cum_3h = round(sum(all_rain[:4]), 1)
        cum_15h = round(sum(all_rain), 1)
        chart_url = generate_chart_url(hourly_rain_list, current_rain_val)
        
        return cum_3h, cum_15h, hourly_rain_list, chart_url
    except Exception as e:
        print(f"⚠️ 予測積算データ取得エラー: {e}")
        return 0.0, 0.0, [], ""

def send_google_chat_card(webhook_url, lat, lon, title_text, formatted_text, icon_url, chart_url=None):
    """
    Google Chat Webhook へ CardsV2 形式のリッチカード通知を送信します。
    """
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
                "altText": "雨量予測グラフ",
                "onClick": {
                    "openLink": {
                        "url": chart_url
                    }
                }
            }
        })
        
    widgets.append({
        "buttonList": {
            "buttons": [
                {
                    "text": "<b>雨雲レーダーを開く</b>｜気象庁",
                    "color": {
                        "red": 0.82,
                        "green": 0.90,
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

# =========================================================
# 5. メインロジック（定期実行判定）
# =========================================================
def main():
    """
    定期実行（GitHub Actions / Cron / クラウドスケジュール等）で使用するメインエントリーポイント。
    気象庁APIをリアルタイム解析し、条件を満たした場合のみGoogle Chatへ通知します。
    """
    webhook_url = os.environ.get("CHAT_WEBHOOK_URL")
    lat_str = os.environ.get("TARGET_LAT")
    lon_str = os.environ.get("TARGET_LON")

    if not webhook_url or not lat_str or not lon_str:
        sys.exit(1)

    lat = float(lat_str)
    lon = float(lon_str)

    init_state_file()

    # 稼働時間外チェック
    if not is_operating_time():
        print("ℹ️ 稼働時間外のため処理をスキップします。")
        if load_state()[0] > 0:
            save_state(0.0, 0, 0, "NONE", load_state()[4])
        sys.exit(0)

    last_rain_val, last_rank, last_notified_rank, last_notified_type, last_evening_alert_date, is_fresh_start = load_state()
    headers = {"User-Agent": "Mozilla/5.0"}

    # 最新のナウキャストタイル時刻を取得
    try:
        elem_res = requests.get("https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N1.json", headers=headers, timeout=10)
        target_times = elem_res.json()
        target = target_times[2]
        basetime = target["basetime"]
        validtime = target["validtime"]
    except Exception:
        sys.exit(1)

    # 現在位置のリアルタイム雨量を取得
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

    # 1) 朝一判定：システム起動時点で既に雨が降っている場合は過剰通知を避けるため初回の降雨通知をスキップ
    if is_fresh_start and current_rank >= 1:
        print("ℹ️ 稼働開始時点で既に雨が降っているため、朝一の通知をスキップします。")
        save_state(rain_val, current_rank, current_rank, "RAINY", last_evening_alert_date)

    # 2) 降雨発生・強まり通知（アメデス）
    elif current_rank >= 1 and (last_notified_type != "RAINY" or current_rank > last_notified_rank):
        cum_3h, cum_15h, _, chart_url = get_future_cumulative_rain_data(lat, lon, rain_val, zoom)
        
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

    # 3) 雨上がり通知（雨上がりの予感）
    elif current_rank == 0 and last_notified_type == "RAINY":
        _, _, _, chart_url = get_future_cumulative_rain_data(lat, lon, rain_val, zoom)
        formatted_text = f"<font color=\"{color_code}\"><b>{rain_desc}</b></font>"
        send_google_chat_card(webhook_url, lat, lon, "雨上がりの予感", formatted_text, ICON_RAINBOW, chart_url)
        save_state(0.0, 0, 0, "WEAK", last_evening_alert_date)

    else:
        save_state(rain_val, current_rain_val, last_notified_rank, last_notified_type, last_evening_alert_date)

    # 4) 夕方定時通知（今夜アメデス）：17時台に積算雨量が閾値を超えている場合のみ実行
    if now.hour == 17 and (0 <= now.minute <= 10) and not sent_amedes_in_this_run and last_evening_alert_date != today_str:
        _, cum_15h, _, chart_url = get_future_cumulative_rain_data(lat, lon, rain_val, zoom)
        
        if cum_15h >= NIGHT_RAIN_THRESHOLD:
            cum_15h_str = str(cum_15h) if cum_15h < 1.0 else str(int(cum_15h))
            formatted_text = f"17～翌8時の積算雨量 <b>{cum_15h_str} mm</b>"
            send_google_chat_card(webhook_url, lat, lon, "今夜アメデス", formatted_text, ICON_NIGHT_RAIN, chart_url)
            save_state(rain_val, current_rank, last_notified_rank, last_notified_type, today_str)

# =========================================================
# 6. テスト実行・表示検証用関数
# =========================================================
def test_all_notifications():
    """
    ローカル開発環境での動作検証・UI表示確認用関数。
    サンプルデータを用いて、全3パターン（「アメデス」「雨上がりの予感」「今夜アメデス」）の
    カードメッセージとグラフ画像を Google Chat へ即時送信します。
    """
    init_state_file()
    webhook_url = os.environ.get("CHAT_WEBHOOK_URL")
    lat = float(os.environ.get("TARGET_LAT", "35.681236"))
    lon = float(os.environ.get("TARGET_LON", "139.767125"))

    if not webhook_url:
        print("❌ エラー: CHAT_WEBHOOK_URL が設定されていません。環境変数を設定してください。")
        sys.exit(1)

    print("🧪 全3パターンの通知表示テストメッセージを送信中...")

    # 重なりを検証するためのダミーデータ
    # 棒グラフが中盤で高くなり、積算の折れ線グラフと交差・重なりやすいパターン
    current_rain_val = 15.0
    sample_rain = [20.0, 15.0, 10.0, 30.0, 25.0, 10.0, 5.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    sample_chart_url = generate_chart_url(sample_rain, current_rain_val)

    # 1. アメデス（降雨通知）テスト
    text_amedes = (
        f"<font color=\"#f5a623\"><b>強い雨</b> 20 mm/h</font><br>"
        f"<font color=\"#757575\">•今後3時間積算 {sum(sample_rain[:3]) + current_rain_val} mm<br>•今後15時間積算 {sum(sample_rain) + current_rain_val} mm</font>"
    )
    send_google_chat_card(webhook_url, lat, lon, "アメデス", text_amedes, ICON_RAINY, sample_chart_url)

    # 2. 雨上がりの予感（止み間通知）テスト
    text_weak = f"<font color=\"#78909c\"><b>降水なし</b></font>"
    send_google_chat_card(webhook_url, lat, lon, "雨上がりの予感", text_weak, ICON_RAINBOW, sample_chart_url)

    # 3. 今夜アメデス（17時定時通知）テスト
    text_evening = f"17～翌8時の積算雨量 <b>145 mm</b>"
    send_google_chat_card(webhook_url, lat, lon, "今夜アメデス", text_evening, ICON_NIGHT_RAIN, sample_chart_url)

    print("✅ テスト送信が完了しました。Google Chatのメッセージをご確認ください。")

# =========================================================
# 7. スクリプト実行エントリーポイント
# =========================================================
if __name__ == "__main__":
    # ---------------------------------------------------------
    # 【運用モードの切り替え】
    #
    # ■ 本番環境で運用する場合（GitHub Actions, Cron, サーバー定期実行）
    #   -> 以下の `main()` のコメントアウト解除 `#` を外し、`test_all_notifications()` をコメントアウトします。
    #
    # ■ ローカル環境でデザインやカードの表示テストを行う場合
    #   -> 以下の `test_all_notifications()` を有効にした状態でスクリプトを実行します。
    # ---------------------------------------------------------
    
    # main()                     # <- 本番定期実行時はこちらを有効化
    test_all_notifications()     # <- デザイン検証テスト時はこちらを有効化
