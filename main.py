import os
import sys
import math
import requests
from datetime import datetime, timezone, timedelta
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
# 3. 気象庁ナウキャストのピクセル色（RGB）から雨量を判定
# ---------------------------------------------------------
def rgb_to_rainfall(rgb):
    # 透明ピクセル（Alpha = 0）の場合は降水なし
    if len(rgb) == 4 and rgb[3] == 0:
        return "降水なし（透明）", 0.0

    r, g, b = rgb[:3]
    if (r, g, b) == (180, 0, 104):  return "猛烈な雨（80mm/h以上）", 80.0
    if (r, g, b) == (255, 0, 0):    return "非常に強い雨（50〜80mm/h）", 50.0
    if (r, g, b) == (255, 106, 0):  return "強い雨（30〜50mm/h）", 30.0
    if (r, g, b) == (255, 216, 0):  return "やや強い雨（20〜30mm/h）", 20.0
    if (r, g, b) == (0, 70, 255):   return "雨（10〜20mm/h）", 10.0
    if (r, g, b) == (0, 170, 255):  return "しっかりした雨（5〜10mm/h）", 5.0
    if (r, g, b) == (100, 200, 255): return "ポツポツ雨（1〜5mm/h）", 1.0
    if (r, g, b) == (200, 230, 255): return "わずかな降水（0.5〜1mm/h）", 0.5
    return "降水なし", 0.0


# ---------------------------------------------------------
# 4. Google Chat CardsV2（カード形式）メッセージ生成処理
# ---------------------------------------------------------
def send_google_chat_card(rain_desc, rain_val):
    jma_url = f"https://www.jma.go.jp/bosai/nowc/#lat:{LAT}/lon:{LON}/zoom:11/colorkind:rain"
    
    jst = timezone(timedelta(hours=9))
    now_jst = datetime.now(jst).strftime("%H:%M")
    
    card_payload = {
        "cardsV2": [
            {
                "cardId": "rainAlertCard",
                "card": {
                    "header": {
                        "title": "☔ 雨雲接近アラート（テスト通知）",
                        "subtitle": f"検知時刻: {now_jst} JST",
                        "imageUrl": "https://fonts.gstatic.com/s/i/short-term/release/googlesymbols/umbrella/default/48px.svg",
                        "imageType": "CIRCLE"
                    },
                    "sections": [
                        {
                            "widgets": [
                                {
                                    "decoratedText": {
                                        "topLabel": "10分後の予想雨量（テスト）",
                                        "text": f"<b><font color=\"#d93025\">{rain_desc}</font></b>",
                                        "bottomLabel": f"推定数値: 約 {rain_val} mm/h"
                                    }
                                },
                                {
                                    "textParagraph": {
                                        "text": "※これは自動通知システムの動作テストメッセージです。"
                                    }
                                },
                                {
                                    "buttonList": {
                                        "buttons": [
                                            {
                                                "text": "雨雲レーダー（気象庁）を開く",
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
                                        "text": "<font color=\"#808080\"><small>出典：気象庁高解像度降水ナウキャスト</small></font>"
                                    }
                                }
                            ]
                        }
                    ]
                }
            }
        ]
    }
    
    print("📤 Google Chatへテストメッセージを送信中...")
    try:
        chat_res = requests.post(WEBHOOK_URL, json=card_payload, timeout=10)
        print(f"📡 Chat API レスポンスコード: {chat_res.status_code}")
        if chat_res.status_code == 200:
            print("✅ Google Chatへカード形式通知を正常に送信しました！")
        else:
            print(f"❌ 送信失敗: HTTP {chat_res.status_code}\n詳細: {chat_res.text}")
    except Exception as e:
        print(f"❌ 通信エラー: {e}")


# ---------------------------------------------------------
# 5. メイン処理
# ---------------------------------------------------------
def main():
    headers = {"User-Agent": "Mozilla/5.0"}
    
    # 🧪 テストモード: 天候にかかわらず強制的に通知をテスト送信する
    print("🔍 画像データのチェックを開始します...")
    
    try:
        elem_res = requests.get("https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N1.json", headers=headers, timeout=10)
        target_times = elem_res.json()
        target = target_times[2]
        basetime = target["basetime"]
        validtime = target["validtime"]
        
        zoom = 10
        xtile, ytile, px, py = latlon_to_tile(LAT, LON, zoom)
        url = f"https://www.jma.go.jp/bosai/jmatile/data/nowc/{basetime}/none/{validtime}/surf/hrpns/{zoom}/{xtile}/{ytile}.png"
        
        print(f"🌐 取得対象URL: {url}")
        res = requests.get(url, headers=headers, timeout=10)
        print(f"📡 画像取得レスポンス: HTTP {res.status_code}")
        
        if res.status_code == 200:
            img = Image.open(BytesIO(res.content)).convert("RGBA")
            pixel_color = img.getpixel((px, py))
            rain_desc, rain_val = rgb_to_rainfall(pixel_color)
            print(f"📊 実際の解析結果: {rain_desc} ({rain_val} mm/h), ピクセルRGBA: {pixel_color}")
        else:
            print("ℹ️ タイル画像が存在しないため『降水なし（データ無）』と判定しました。")
            rain_desc, rain_val = "晴れ / 降水なし（データ透過）", 0.0

    except Exception as e:
        print(f"⚠️ 解析中に例外が発生しました: {e}")
        rain_desc, rain_val = "【テスト判定】晴れ", 0.0

    # 強制的にChatへ通知を送信
    send_google_chat_card(f"テスト実行: {rain_desc}", rain_val)

if __name__ == "__main__":
    main()
