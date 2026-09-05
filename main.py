import os
import sys
import math
import json
import uuid
import requests
from datetime import datetime, timezone, timedelta
from PIL import Image
from io import BytesIO

STATE_FILE = "state.json"

# 夜間積算雨量（17時〜翌8時）の通知しきい値（mm）
NIGHT_RAIN_THRESHOLD = float(os.environ.get("NIGHT_RAIN_THRESHOLD", "15.0"))

# Google Noto Emoji (SIL Open Font License / Apache 2.0: 完全商用フリー・クレジット不要・ダークモード対応)
ICON_RAINY = "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/128/emoji_u1f327.png"      # 雨雲
ICON_RAINBOW = "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/128/emoji_u1f308.png"    # 虹
ICON_NIGHT_RAIN = "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/128/emoji_u1f303.png" # 夜の街/夜空（雨の夜イメージ）


# ---------------------------------------------------------
# 1. 稼働時間・休日の判定 (8:00〜19:00 / 日曜・正月三が日のみ除外)
# ---------------------------------------------------------
def is_operating_time():
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    
    # 8:00〜19:00 以外の時間帯は除外
    if not (8 <= now.hour < 19):
        return False
        
    # 日曜日 (weekday: 0=月, 1=火, ... 6=日)
    if now.weekday() == 6:
        return False
        
    # 1月1日〜1月3日 (正月三が日)
    if now.month == 1 and 1 <= now.day <= 3:
        return False
        
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
# 3. 座標計算・画像解析・予測積算雨量算出
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

def get_future_cumulative_rain(lat, lon, zoom=10):
    """気象庁降水短時間予報から今後3時間および15時間の積算雨量(mm)を算出"""
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        url_target = "https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N2.json"
        res = requests.get(url_target, headers=headers, timeout=10)
        if res.status_code != 200:
            return 0.0, 0.0
        
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
        
        cum_3h = sum(hourly_rain_list[:3])
        cum_15h = sum(hourly_rain_list[:15])
        return round(cum_3h, 1), round(cum_15h, 1)
    except Exception as e:
        print(f"⚠️ 予測積算データ取得エラー: {e}")
        return 0.0, 0.0


# ---------------------------------------------------------
# 4. Google Chat カード送信処理（CardsV2）
# ---------------------------------------------------------
def send_google_chat_card(webhook_url, lat, lon, title_text, formatted_text, icon_url):
    jma_url = f"https://www.jma.go.jp/bosai/kaikotan/#lat:{lat}/lon:{lon}/zoom:11"
    unique_card_id = f"rainAlert_{uuid.uuid4().hex[:8]}"
    
    card_payload = {
        "cardsV2": [
            {
                "cardId": unique_card_id,
                "card": {
                    "header": {
                        "title": title_text,
                        "imageUrl": icon_url,
                        "imageType": "SQUARE" # 四角形指定によりトリミングを解除
                    },
                    "sections": [
                        {
                            "widgets": [
                                {
                                    "textParagraph": {
                                        "text": formatted_text
                                    }
                                },
                                {
                                    "buttonList": {
                                        "buttons": [
                                            {
                                                "text": "雨雲レーダーを開く｜気象庁",
                                                "onClick": {
                                                    "openLink": {
                                                        "url": jma_url
                                                    }
                                                }
                                            }
                                        ]
                                    }
                                }
                            ]
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

    # 10分後ナウキャストデータ取得
    try:
        elem_res = requests.get("https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N1.json", headers=headers, timeout=10)
        target_times = elem_res.json()
        target = target_times[2] # 10分後予測
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

    # --- 1. 通常通知判定ロジック ---
    if is_fresh_start and current_rank >= 1:
        print("ℹ️ 稼働開始時点で既に雨が降っているため、朝一の通知をスキップします。")
        save_state(rain_val, current_rank, current_rank, "RAINY", last_evening_alert_date)

    elif current_rank >= 1 and (last_notified_type != "RAINY" or current_rank > last_notified_rank):
        cum_3h, cum_15h = get_future_cumulative_rain(lat, lon, zoom)
        
        val_str = str(rain_val) if rain_val < 1.0 else str(int(rain_val))
        cum_3h_str = str(cum_3h) if cum_3h < 1.0 else str(int(cum_3h))
        cum_15h_str = str(cum_15h) if cum_15h < 1.0 else str(int(cum_15h))
        
        formatted_text = (
            f"<font color=\"{color_code}\"><b>{rain_desc}</b> {val_str} mm/h</font><br>"
            f"<font color=\"#757575\">•今後3時間積算 {cum_3h_str} mm<br>•今後15時間積算 {cum_15h_str} mm</font>"
        )
        
        send_google_chat_card(webhook_url, lat, lon, "アメデス", formatted_text, ICON_RAINY)
        save_state(rain_val, current_rank, current_rank, "RAINY", last_evening_alert_date)
        sent_amedes_in_this_run = True

    elif current_rank == 0 and last_notified_type == "RAINY":
        formatted_text = f"<font color=\"{color_code}\"><b>{rain_desc}</b></font>"
        send_google_chat_card(webhook_url, lat, lon, "雨が弱くなります", formatted_text, ICON_RAINBOW)
        save_state(0.0, 0, 0, "WEAK", last_evening_alert_date)

    else:
        save_state(rain_val, current_rank, last_notified_rank, last_notified_type, last_evening_alert_date)


    # --- 2. 終業時（17時前後）の「今夜アメデス」アラート判定 ---
    if now.hour == 17 and (0 <= now.minute <= 10) and not sent_amedes_in_this_run and last_evening_alert_date != today_str:
        _, cum_15h = get_future_cumulative_rain(lat, lon, zoom)
        
        if cum_15h >= NIGHT_RAIN_THRESHOLD:
            cum_15h_str = str(cum_15h) if cum_15h < 1.0 else str(int(cum_15h))
            formatted_text = f"17～翌8時の積算雨量 <b>{cum_15h_str} mm</b>"
            send_google_chat_card(webhook_url, lat, lon, "今夜アメデス", formatted_text, ICON_NIGHT_RAIN)
            save_state(rain_val, current_rank, last_notified_rank, last_notified_type, today_str)


# ---------------------------------------------------------
# 6. 全通知テスト実行用関数
# ---------------------------------------------------------
def test_all_notifications():
    """本システムの全3パターンの通知カードを表示・動作確認するテスト関数"""
    init_state_file()
    webhook_url = os.environ.get("CHAT_WEBHOOK_URL")
    lat = float(os.environ.get("TARGET_LAT", "35.681236"))
    lon = float(os.environ.get("TARGET_LON", "139.767125"))

    if not webhook_url:
        print("❌ エラー: CHAT_WEBHOOK_URL が設定されていません。")
        sys.exit(1)

    print("🧪 全3パターンの通知表示テストメッセージを送信中...")

    # テスト1: アメデス（箇条書き・改行形式）
    text_amedes = (
        f"<font color=\"#f5a623\"><b>強い雨</b> 20 mm/h</font><br>"
        f"<font color=\"#757575\">•今後3時間積算 35 mm<br>•今後15時間積算 68 mm</font>"
    )
    send_google_chat_card(webhook_url, lat, lon, "アメデス", text_amedes, ICON_RAINY)

    # テスト2: 雨が弱くなります
    text_weak = f"<font color=\"#78909c\"><b>降水なし</b></font>"
    send_google_chat_card(webhook_url, lat, lon, "雨が弱くなります", text_weak, ICON_RAINBOW)

    # テスト3: 今夜アメデス（色なし・数値のみ太字）
    text_evening = f"17～翌8時の積算雨量 <b>32 mm</b>"
    send_google_chat_card(webhook_url, lat, lon, "今夜アメデス", text_evening, ICON_NIGHT_RAIN)

    print("✅ テスト送信が完了しました。Google Chatのメッセージをご確認ください。")


# ---------------------------------------------------------
# 7. スクリプト実行エントリーポイント
# ---------------------------------------------------------
if __name__ == "__main__":
    # 【本番運用モード】（普段はこちらを有効化）
    # main()

    # 【テスト送信モード】（テスト時は上の main() の頭に # を付け、下の行の # を消してください）
    test_all_notifications()
