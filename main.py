import os
import sys
import requests
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------
# 1. 環境変数の取得とチェック
# ---------------------------------------------------------
WEBHOOK_URL = os.environ.get("CHAT_WEBHOOK_URL")

if not WEBHOOK_URL:
    print("❌ エラー: CHAT_WEBHOOK_URL が取得できませんでした。")
    sys.exit(1)

# ---------------------------------------------------------
# 2. テスト用 Google Chat CardsV2 メッセージ送信処理
# ---------------------------------------------------------
def main():
    jst = timezone(timedelta(hours=9))
    now_jst = datetime.now(jst).strftime("%H:%M")
    
    # 動作確認用にテストカードデータを作成
    card_payload = {
        "cardsV2": [
            {
                "cardId": "rainAlertTestCard",
                "card": {
                    "header": {
                        "title": "☔ 雨雲接近アラート（動作テスト）",
                        "subtitle": f"送信テスト時刻: {now_jst} JST",
                        "imageUrl": "https://fonts.gstatic.com/s/i/short-term/release/googlesymbols/umbrella/default/48px.svg",
                        "imageType": "CIRCLE"
                    },
                    "sections": [
                        {
                            "widgets": [
                                {
                                    "decoratedText": {
                                        "topLabel": "10分後の予想雨量（ダミーデータ）",
                                        "text": "<b><font color=\"#d93025\">強い雨（30〜50mm/h）</font></b>",
                                        "bottomLabel": "推定数値: 約 30.0 mm/h"
                                    }
                                },
                                {
                                    "textParagraph": {
                                        "text": "✅ システムの通知テストです。Google Chatへの連携が正常に機能しています。"
                                    }
                                },
                                {
                                    "buttonList": {
                                        "buttons": [
                                            {
                                                "text": "雨雲レーダー（気象庁）を開く",
                                                "onClick": {
                                                    "openLink": {
                                                        "url": "https://www.jma.go.jp/bosai/nowc/"
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
    
    print("📤 Google Chatへテストメッセージを送信しています...")
    try:
        chat_res = requests.post(WEBHOOK_URL, json=card_payload, timeout=10)
        print(f"📡 レスポンスコード: HTTP {chat_res.status_code}")
        
        if chat_res.status_code == 200:
            print("✅ Google Chatへのテスト通知送信に成功しました！スペースを確認してください。")
        else:
            print(f"❌ 送信エラーが発生しました: HTTP {chat_res.status_code}")
            print(f"詳細: {chat_res.text}")
    except Exception as e:
        print(f"❌ 通信エラーが発生しました: {e}")

if __name__ == "__main__":
    main()
