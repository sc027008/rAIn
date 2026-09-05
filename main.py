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
    print("GitHubの『Settings > Secrets and variables > Actions』の設定を確認してください。")
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
    
    # 日本時間の現在時刻を取得
    jst = timezone(timedelta(hours=9))
    now_jst = datetime.now(jst).strftime("%H:%M")
    
    card_payload = {
        "cardsV2": [
            {
                "cardId": "rainAlertCard",
                "card": {
                    "header": {
                        "title": "☔ 雨雲接近アラート（10分後予報）",
                        "subtitle": f"検知時刻: {now_jst} JST",
                        "imageUrl": "https://fonts.gstatic.com/s/i/short-term/release/googlesymbols/umbrella/default/48px.svg",
                        "imageType": "CIRCLE"
                    },
                    "sections": [
                        {
                            "widgets": [
                                {
                                    "decoratedText": {
                                        "topLabel": "10分後の予想雨量",
                                        "text": f"<b><font color=\"#d93025\">{rain_desc}</font></b>",
                                        "bottomLabel": f"推定数値: 約 {rain_val} mm/h 以上"
                                    }
                                },
                                {
                                    "textParagraph": {
                                        "text": "まもなく周辺でまとまった雨が降り始める予想です。傘の準備や屋外の荷物の移動を確認してください。"
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
            print("❌ エラー: 気象庁の時刻データ（targetTimes_N1.json）の取得に失敗しました。")
            sys.exit(1)
        
        target_times = elem_res.json()
        # 10分後（インデックス2: 0=現在, 1=5分後, 2=10分後）の時刻情報を取得
        target = target_times[2]
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
    
    rain_desc, rain_val = rgb_to_rainfall(pixel_color)
    print(f"📊 解析結果 [10分後予報] -> 状態: {rain_desc} ({rain_val} mm/h)")

    # ④ 5.0mm/h以上の雨が予想された場合のみCardsV2で通知
    if rain_val >= 5.0:
        send_google_chat_card(rain_desc, rain_val)
    else:
        print("ℹ️ 10分後の予測雨量は5.0mm/h未満のため、通知をスキップしました。")

if __name__ == "__main__":
    main()
