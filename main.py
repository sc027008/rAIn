import os
import sys
import math
import requests
from PIL import Image
from io import BytesIO

# ---------------------------------------------------------
# 1. 環境変数の取得と安全性のチェック
# ---------------------------------------------------------
WEBHOOK_URL = os.environ.get("CHAT_WEBHOOK_URL")
LAT_STR = os.environ.get("TARGET_LAT")
LON_STR = os.environ.get("TARGET_LON")

if not WEBHOOK_URL or not LAT_STR or not LON_STR:
    print("❌ エラー: 必須の環境変数 (CHAT_WEBHOOK_URL, TARGET_LAT, TARGET_LON) が取得できませんでした。")
    sys.exit(1)

try:
    LAT = float(LAT_STR)
    LON = float(LON_STR)
except ValueError:
    print(f"❌ エラー: 緯度・経度の数値変換に失敗しました。(TARGET_LAT: '{LAT_STR}', TARGET_LON: '{LON_STR}')")
    sys.exit(1)


# ---------------------------------------------------------
# 2. 緯度経度から気象庁画像タイル（Webメルカトル）の座標を計算
# ---------------------------------------------------------
def latlon_to_tile(lat, lon, zoom=10):
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    xtile = int((lon + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    
    pixel_x = int(((lon + 180.0) / 360.0 * n - xtile) * 256)
    pixel_y = int(((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n - ytile) * 256)
    return xtile, ytile, pixel_x, pixel_y


# ---------------------------------------------------------
# 3. 気象庁ナウキャストのピクセル色（RGB）から雨量と表示色を判定
# ---------------------------------------------------------
def rgb_to_rainfall(rgb):
    r, g, b = rgb[:3]
    # 返り値: (テキスト表現, 数値(mm/h), カラーコード)
    if (r, g, b) == (180, 0, 104):  return "猛烈な雨", 80.0, "#8e24aa"  # 紫
    if (r, g, b) == (255, 0, 0):    return "非常に強い雨", 50.0, "#b71c1c" # 濃赤
    if (r, g, b) == (255, 106, 0):  return "強い雨", 30.0, "#d93025"     # 赤
    if (r, g, b) == (255, 216, 0):  return "やや強い雨", 20.0, "#e65100" # オレンジ
    if (r, g, b) == (0, 70, 255):   return "雨", 10.0, "#0d47a1"         # 濃青
    if (r, g, b) == (0, 170, 255):  return "しっかりした雨", 5.0, "#1a73e8" # 青
    if (r, g, b) == (100, 200, 255): return "ポツポツ雨", 1.0, "#4285f4" # 薄青
    if (r, g, b) == (200, 230, 255): return "わずかな降水", 0.5, "#78909c" # グレー
    return "降水なし", 0.0, "#5f6368"


# ---------------------------------------------------------
# 4. Google Chat CardsV2（シンプルカード形式）メッセージ生成処理
# ---------------------------------------------------------
def send_google_chat_card(rain_desc, rain_val, color_code):
    # 気象庁「今後の雨」ページへ直接移動するURL
    jma_url = f"https://www.jma.go.jp/bosai/nowc/#lat:{LAT}/lon:{LON}/zoom:11/colorkind:amemesh"
    
    card_payload = {
        "cardsV2": [
            {
                "cardId": "rainAlertCard",
                "card": {
                    "header": {
                        "title": "アメデス",
                        "imageUrl": "https://fonts.gstatic.com/s/i/short-term/release/googlesymbols/umbrella/default/48px.svg", # 開いた傘アイコン
                        "imageType": "CIRCLE"
                    },
                    "sections": [
                        {
                            "widgets": [
                                {
                                    "textParagraph": {
                                        "text": f"<b><font color=\"{color_code}\">{rain_desc}</font></b>"
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
                                        "text": "<font color=\"#a0a0a0\"><small>出典: 気象庁</small></font>"
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
        chat_res = requests.post(WEBHOOK_URL, json=card_payload, timeout=10)
        if chat_res.status_code == 200:
            print("✅ Google Chatへカード形式通知を正常に送信しました。")
        else:
            print(f"⚠️ Google Chatへの送信でエラーが発生しました: HTTP {chat_res.status_code}\n{chat_res.text}")
    except Exception as e:
        print(f"❌ Google Chatへの通知送信時に通信エラーが発生しました: {e}")


# ---------------------------------------------------------
# 5. メイン処理
# ---------------------------------------------------------
def main():
    headers = {"User-Agent": "Mozilla/5.0"}
    
    # ① 最新の基準時刻（basetime）と10分後の予測時刻（validtime）を取得
    try:
        elem_res = requests.get("https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N1.json", headers=headers, timeout=10)
        if elem_res.status_code != 200:
            print("❌ エラー: 気象庁の時刻データの取得に失敗しました。")
            sys.exit(1)
        
        target_times = elem_res.json()
        target = target_times[2] # 10分後の予測データ
        basetime = target["basetime"]
        validtime = target["validtime"]
        
    except Exception as e:
        print(f"❌ 時刻データの解析エラー: {e}")
        sys.exit(1)

    zoom = 10
    xtile, ytile, px, py = latlon_to_tile(LAT, LON, zoom)
    
    # ② 10分後（validtime）の降水ナウキャスト画像タイルURLを作成
    url = f"https://www.jma.go.jp/bosai/jmatile/data/nowc/{basetime}/none/{validtime}/surf/hrpns/{zoom}/{xtile}/{ytile}.png"
    
    try:
        res = requests.get(url, headers=headers, timeout=10)
    except Exception as e:
        print(f"❌ 通信エラー: 気象庁画像サーバーへのアクセスに失敗しました: {e}")
        sys.exit(1)
        
    if res.status_code != 200:
        print("ℹ️ 該当時間帯の雨雲画像データが存在しないか、更新待機中です。")
        return

    # ③ 画像解析（該当ピクセルのRGB取得）
    img = Image.open(BytesIO(res.content)).convert("RGBA")
    pixel_color = img.getpixel((px, py))
    
    rain_desc, rain_val, color_code = rgb_to_rainfall(pixel_color)
    print(f"📊 解析結果 [10分後予報] -> 状態: {rain_desc} ({rain_val} mm/h)")

    # ④ 5.0mm/h以上の雨が予想された場合のみCardsV2で通知
    if rain_val >= 0.0:
        send_google_chat_card(rain_desc, rain_val, color_code)
    else:
        print("ℹ️ 10分後の予測雨量は5.0mm/h未満のため、通知をスキップしました。")

if __name__ == "__main__":
    main()
