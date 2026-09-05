import os
import sys
import math
import json
import requests
import jpholiday
from datetime import datetime, timezone, timedelta
from PIL import Image
from io import BytesIO

STATE_FILE = "state.json"

# Google Material Symbols アイコンURL (Google公式・商用フリー)
ICON_RAINY = "https://fonts.gstatic.com/s/i/short-term/release/googlesymbols/rainy/default/48px.svg"
ICON_CLOUD = "https://fonts.gstatic.com/s/i/short-term/release/googlesymbols/cloud/default/48px.svg"


# ---------------------------------------------------------
# 1. 稼働時間・休日の判定 (8:00〜19:00 / 平日のみ / お盆・年末年始除外)
# ---------------------------------------------------------
def is_operating_time():
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    
    # 時間帯チェック (8:00〜18:59)
    if not (8 <= now.hour < 19):
        return False
        
    # 曜日チェック (0:月〜4:金)
    if now.weekday() >= 5:
        return False
        
    # 祝日チェック (祝日法に規定された正式な祝日のみ)
    if jpholiday.is_holiday(now.date()):
        return False
        
    # お盆 (8/11〜8/16) チェック
    if now.month == 8 and 11 <= now.day <= 16:
        return False
        
    # 年末年始 (12/29〜1/3) チェック
    if (now.month == 12 and now.day >= 29) or (now.month == 1 and now.day <= 3):
        return False
        
    return True


# ---------------------------------------------------------
# 2. 状態（state.json）の読み込み・保存
# ---------------------------------------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                last_time_str = data.get("last_updated", "")
                if last_time_str:
                    last_time = datetime.fromisoformat(last_time_str)
                    jst = timezone(timedelta(hours=9))
                    # 前回記録から1時間以上経過している場合は間隔が空いたためリセット
                    if (datetime.now(jst) - last_time).total_seconds() > 3600:
                        return 0.0, 0
                return float(data.get("last_rain_val", 0.0)), int(data.get("last_rank", 0))
        except Exception:
            return 0.0, 0
    return 0.0, 0

def save_state(rain_val, rank):
    jst = timezone(timedelta(hours=9))
    data = {
        "last_rain_val": rain_val,
        "last_rank": rank,
        "last_updated": datetime.now(jst).isoformat()
    }
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)


# ---------------------------------------------------------
# 3. 座標計算と画像解析
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
    # 返り値: (テキスト表現, 数値(mm/h), カラーコード, 危険度ランク)
    if (r, g, b) == (180, 0, 104):  return "猛烈な雨", 80.0, "#8e24aa", 6        # 紫
    if (r, g, b) == (255, 0, 0):    return "非常に激しい雨", 50.0, "#b71c1c", 5 # 濃赤
    if (r, g, b) == (255, 106, 0):  return "激しい雨", 30.0, "#d93025", 4       # 赤
    if (r, g, b) == (255, 216, 0):  return "強い雨", 20.0, "#e65100", 3         # オレンジ
    if (r, g, b) == (0, 70, 255):   return "やや強い雨", 10.0, "#0d47a1", 2     # 濃青
    if (r, g, b) == (0, 170, 255):  return "雨", 5.0, "#1a73e8", 1              # 青
    if (r, g, b) == (100, 200, 255): return "弱雨", 1.0, "#4285f4", 0           # 薄青
    if (r, g, b) == (200, 230, 255): return "わずかな降水", 0.5, "#78909c", 0   # グレー
    return "降水なし", 0.0, "#5f6368", 0


# ---------------------------------------------------------
# 4. Google Chat カード送信処理（CardsV2）
# ---------------------------------------------------------
def send_google_chat_card(webhook_url, lat, lon, title_text, msg_text, rain_val, color_code, icon_url):
    jma_url = f"https://www.jma.go.jp/bosai/nowc/#lat:{lat}/lon:{lon}/zoom:11/colorkind:amemesh"
    
    # 降水がある場合のみ数値 (◯mm/h) を表示テキストに併記
    if rain_val > 0.0:
        display_text = f"{msg_text} ({int(rain_val)}mm/h)"
    else:
        display_text = msg_text
    
    card_payload = {
        "cardsV2": [
            {
                "cardId": "rainAlertCard",
                "card": {
                    "header": {
                        "title": title_text,
                        "imageUrl": icon_url,
                        "imageType": "CIRCLE"
                    },
                    "sections": [
                        {
                            "widgets": [
                                {
                                    "textParagraph": {
                                        "text": f"<b><font color=\"{color_code}\">{display_text}</font></b>"
                                    }
                                },
                                {
                                    "buttonList": {
                                        "buttons": [
                                            {
                                                "text": "雨雲レーダーを開く",
                                                "onClick": {
                                                    "openLink": {
                                                        "url": jma_url
                                                    }
                                                }
                                            }
                                        ]
                                    }
                                },
                                {
                                    "textParagraph": {
                                        # 案1: 最も薄いグレー(#c0c0c0)で最小化した文字表記
                                        "text": "<font color=\"#c0c0c0\"><small>出典: 気象庁</small></font>"
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
        requests.post(webhook_url, json=card_payload, timeout=10)
    except Exception as e:
        print(f"❌ 送信エラー: {e}")


# ---------------------------------------------------------
# 5. メイン処理
# ---------------------------------------------------------
def main():
    webhook_url = os.environ.get("CHAT_WEBHOOK_URL")
    lat_str = os.environ.get("TARGET_LAT")
    lon_str = os.environ.get("TARGET_LON")

    if not webhook_url or not lat_str or not lon_str:
        sys.exit(1)

    lat = float(lat_str)
    lon = float(lon_str)

    # 稼働時間外の判定
    if not is_operating_time():
        print("ℹ️ 稼働時間外のため処理をスキップします。")
        if load_state()[0] > 0:
            save_state(0.0, 0)
        sys.exit(0)

    last_rain_val, last_rank = load_state()
    headers = {"User-Agent": "Mozilla/5.0"}

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
            rain_desc, rain_val, color_code, current_rank = "降水なし", 0.0, "#5f6368", 0
        else:
            img = Image.open(BytesIO(res.content)).convert("RGBA")
            pixel_color = img.getpixel((px, py))
            rain_desc, rain_val, color_code, current_rank = rgb_to_rainfall(pixel_color)
    except Exception:
        sys.exit(1)

    print(f"📊 前回ランク: {last_rank} (数値:{last_rain_val}) -> 今回ランク: {current_rank} (数値:{rain_val}, {rain_desc})")

    # 通知条件判定
    # ① 5.0mm/h以上で、前回のランクより「雨の強さの区分」が上がった時（降り始め・雨が強くなった時）
    if current_rank >= 1 and current_rank > last_rank:
        send_google_chat_card(
            webhook_url, lat, lon,
            title_text="アメデス",
            msg_text=rain_desc,
            rain_val=rain_val,
            color_code=color_code,
            icon_url=ICON_RAINY
        )
        save_state(rain_val, current_rank)
        
    # ② 前回5.0mm/h以上降っていた状態から、5.0mm/h未満になった時（雨が弱まった・本降り終了）
    elif last_rank >= 1 and current_rank == 0:
        send_google_chat_card(
            webhook_url, lat, lon,
            title_text="雨が弱くなりました",
            msg_text=rain_desc,
            rain_val=rain_val,
            color_code=color_code,
            icon_url=ICON_CLOUD
        )
        save_state(0.0, 0)
        
    # ③ それ以外（ランクに変化がない場合等）は数値とランクの状態更新のみ
    else:
        save_state(rain_val, current_rank)


if __name__ == "__main__":
    # main()
    # 🧪 テスト1: 雨が強くなった場合のカード（アメデス / Rainyアイコン / 数値併記）
    send_google_chat_card(
        os.environ.get("CHAT_WEBHOOK_URL"),
        float(os.environ.get("TARGET_LAT", "35.681236")),
        float(os.environ.get("TARGET_LON", "139.767125")),
        title_text="アメデス",
        msg_text="激しい雨",
        rain_val=30.0,
        color_code="#d93025",
        icon_url=ICON_RAINY
    )
    
    # 🧪 テスト2: 雨が弱くなった場合のカード（雨が弱くなりました / Cloudアイコン）
    send_google_chat_card(
        os.environ.get("CHAT_WEBHOOK_URL"),
        float(os.environ.get("TARGET_LAT", "35.681236")),
        float(os.environ.get("TARGET_LON", "139.767125")),
        title_text="雨が弱くなりました",
        msg_text="降水なし",
        rain_val=0.0,
        color_code="#5f6368",
        icon_url=ICON_CLOUD
    )
