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

# ダークモード/ライトモード両対応の白縁取りアイコン（商用フリー・パブリックドメイン/オープンライセンス）
ICON_RAINY = "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f327.png"  # 雨雲アイコン
ICON_CLOUD = "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/2601.png"   # 雲アイコン


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
                    last_time = datetime.now().fromisoformat(last_time_str)
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
    # ダークモード/ライトモード視認性調整済みのカラーコード
    # 返り値: (テキスト表現, 数値(mm/h), カラーコード, 危険度ランク)
    if (r, g, b) == (180, 0, 104):  return "猛烈な雨", 80.0, "#ab47bc", 6        # マゼンタ系紫（視認性向上）
    if (r, g, b) == (255, 0, 0):    return "非常に激しい雨", 50.0, "#e53935", 5 # 鮮やかな赤
    if (r, g, b) == (255, 106, 0):  return "激しい雨", 30.0, "#f57c00", 4       # ビビッドオレンジ
    if (r, g, b) == (255, 216, 0):  return "強い雨", 20.0, "#fbc02d", 3         # 明るいイエローオレンジ
    if (r, g, b) == (0, 70, 255):   return "やや強い雨", 10.0, "#1e88e5", 2     # 明るめのブルー
    if (r, g, b) == (0, 170, 255):  return "雨", 5.0, "#29b6f6", 1              # ライトブルー
    if (r, g, b) == (100, 200, 255): return "弱雨", 1.0, "#4dd0e1", 0           # シアン
    if (r, g, b) == (200, 230, 255): return "わずかな降水", 0.5, "#90a4ae", 0   # グレー
    return "降水なし", 0.0, "#78909c", 0


# ---------------------------------------------------------
# 4. Google Chat カード送信処理（CardsV2）
# ---------------------------------------------------------
def send_google_chat_card(webhook_url, lat, lon, title_text, msg_text, rain_val, color_code, icon_url):
    jma_url = f"https://www.jma.go.jp/bosai/nowc/#lat:{lat}/lon:{lon}/zoom:11/colorkind:amemesh"
    
    # 降水がある場合のみ数値 (◯ mm/h) を半角スペース付きで併記
    if rain_val > 0.0:
        display_text = f"{msg_text} ({int(rain_val)} mm/h)"
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
                                        # ダークモード対応かつ最小フォント指定
                                        "text": "<font color=\"#9e9e9e\"><sub>出典: 気象庁</sub></font>"
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
# 5. 全カラー＆パターン出力テスト関数
# ---------------------------------------------------------
def test_all_colors():
    webhook_url = os.environ.get("CHAT_WEBHOOK_URL")
    lat = float(os.environ.get("TARGET_LAT", "35.681236"))
    lon = float(os.environ.get("TARGET_LON", "139.767125"))

    test_patterns = [
        ("アメデス", "猛烈な雨", 80.0, "#ab47bc", ICON_RAINY),
        ("アメデス", "非常に激しい雨", 50.0, "#e53935", ICON_RAINY),
        ("アメデス", "激しい雨", 30.0, "#f57c00", ICON_RAINY),
        ("アメデス", "強い雨", 20.0, "#fbc02d", ICON_RAINY),
        ("アメデス", "やや強い雨", 10.0, "#1e88e5", ICON_RAINY),
        ("アメデス", "雨", 5.0, "#29b6f6", ICON_RAINY),
        ("雨が弱くなりました", "降水なし", 0.0, "#78909c", ICON_CLOUD),
    ]

    print("🧪 全7パターンの表示テストメッセージを送信中...")
    for title, msg, val, color, icon in test_patterns:
        send_google_chat_card(webhook_url, lat, lon, title, msg, val, color, icon)
    print("✅ テスト送信完了。Google Chatをご確認ください。")


# ---------------------------------------------------------
# 6. メイン処理
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
            rain_desc, rain_val, color_code, current_rank = "降水なし", 0.0, "#78909c", 0
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
    # main()  # 一時的にコメントアウト
    test_all_colors()  # 🧪 全色味・全パターンのテスト実行
