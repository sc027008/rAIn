import os
import sys
import math
import json
import uuid
import random
import requests
import urllib.parse
import subprocess
from datetime import datetime, timezone, timedelta
from PIL import Image
from io import BytesIO

# =========================================================
# 0. 定数・環境変数・各種アイコン設定
# =========================================================
STATE_FILE = "state.json"

# 気象庁タイルのズームレベル（全国共通 zoom=10）
ZOOM_LEVEL = 10

# 夜間積算雨量（17時〜翌8時）の通知判定しきい値（mm）
NIGHT_RAIN_THRESHOLD = float(os.environ.get("NIGHT_RAIN_THRESHOLD", "15.0"))

# Google Noto Emoji アイコンURL（カードヘッダー用）
ICON_RAINY = "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/128/emoji_u2614.png"
ICON_RAINBOW = "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/128/emoji_u1f308.png"
ICON_NIGHT_RAIN = "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/128/emoji_u1f303.png"

# 気象庁雨雲タイルの全3種24パターンカラーパレット統合定義（各降水階級の中央値補正版）
# フォーマット: ((R, G, B), 降水量mm/h, 降水状態の日本語表示)
JMA_24_PALETTE = [
    # パレット1 (標準スタイル)
    ((180, 0, 104), 80.0, "猛烈な雨"), ((255, 40, 0), 65.0, "非常に激しい雨"),
    ((255, 153, 0), 40.0, "激しい雨"), ((255, 245, 0), 25.0, "強い雨"),
    ((0, 65, 255), 15.0, "やや強い雨"), ((33, 140, 255), 7.0, "雨"),
    ((160, 210, 255), 3.0, "弱雨"), ((242, 242, 255), 0.5, "わずかな降水"),
    # パレット2 (中間スタイル)
    ((199, 64, 142), 80.0, "猛烈な雨"), ((255, 94, 64), 65.0, "非常に激しい雨"),
    ((255, 179, 64), 40.0, "激しい雨"), ((255, 248, 64), 25.0, "強い雨"),
    ((64, 113, 255), 15.0, "やや強い雨"), ((89, 169, 255), 7.0, "雨"),
    ((184, 222, 255), 3.0, "弱雨"), ((246, 246, 255), 0.5, "わずかな降水"),
    # パレット3 (パステルスタイル)
    ((217, 127, 179), 80.0, "猛烈な雨"), ((255, 147, 127), 65.0, "非常に激しい雨"),
    ((255, 204, 127), 40.0, "激しい雨"), ((255, 250, 127), 25.0, "強い雨"),
    ((127, 160, 255), 15.0, "やや強い雨"), ((144, 197, 255), 7.0, "雨"),
    ((207, 232, 255), 3.0, "弱雨"), ((248, 248, 255), 0.5, "わずかな降水"),
]

# =========================================================
# 1. 稼働時間・休日の判定ロジック
# =========================================================
def is_operating_time():
    """
    現在の日本時間が通知稼働時間内か判定します。
    【稼働条件】
    - 時間: 8:00 〜 18:59 JST
    - 曜日: 月〜土曜日（日曜日[weekday=6]は除外）
    - 祝日特例: 正月三箇日（1月1日〜3日）は除外
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
    """実行状態（前回通知した雨量ランク・通知タイプ・日付など）をstate.jsonへ永続化保存します。"""
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
    """state.jsonが存在しない場合に初期ファイルを作成します。"""
    if not os.path.exists(STATE_FILE):
        save_state(0.0, 0, 0, "NONE", "")

def load_state():
    """
    state.json から前回の状態を取得します。
    【戻り値】
    (last_rain_val, last_rank, last_notified_rank, last_notified_type, last_evening_alert_date, is_fresh_start)
    ※最終更新から1時間以上経過している場合は強制リセットフラグ(is_fresh_start=True)を返します。
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
                    # 1時間以上実行が空いた場合はリセット処理
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
def latlon_to_tile(lat, lon, zoom=ZOOM_LEVEL):
    """
    緯度経度(WGS84)を気象庁Webメルカトルタイルの座標(X, Y)およびタイル内ピクセル位置(px, py)へ変換します。
    zoom: タイルのズームレベル（デフォルト ZOOM_LEVEL = 10）
    """
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    xtile = int((lon + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    pixel_x = int(((lon + 180.0) / 360.0 * n - xtile) * 256)
    pixel_y = int(((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n - ytile) * 256)
    return xtile, ytile, pixel_x, pixel_y

def rgb_to_rainfall(pixel):
    """
    ピクセルのRGBA値から気象庁24パレットと照合し、降雨強度(mm/h)・状態表記・カラーコード・雨量ランク(0-6)を返します。
    ※アルファ値=0（透明）および (255,255,255)（白背景）は優先的に「降水なし」と判定します。
    """
    if not pixel or len(pixel) < 3 or (len(pixel) >= 4 and pixel[3] == 0):
        return "降水なし", 0.0, "#78909c", 0

    r, g, b = pixel[:3]
    if (r, g, b) == (255, 255, 255):
        return "降水なし", 0.0, "#78909c", 0

    min_dist = float("inf")
    best_desc, best_val = "降水なし", 0.0

    # 24パターンパレットとの3次元色空間距離（ユークリッド距離）を計算
    for (pr, pg, pb), val, desc in JMA_24_PALETTE:
        dist = math.sqrt((r - pr)**2 + (g - pg)**2 + (b - pb)**2)
        if dist < min_dist:
            min_dist = dist
            best_desc, best_val = desc, val

    # 許容色誤差（距離45超）を超える場合は背景色等とみなして非降水扱い
    if min_dist > 45.0:
        return "降水なし", 0.0, "#78909c", 0

    # 降水量に応じた表示色と通知ランク(0〜6)のマッピング
    if best_val >= 80.0: return best_desc, best_val, "#ab47bc", 6
    if best_val >= 65.0: return best_desc, best_val, "#e53935", 5
    if best_val >= 40.0: return best_desc, best_val, "#f57c00", 4
    if best_val >= 25.0: return best_desc, best_val, "#f5a623", 3
    if best_val >= 15.0: return best_desc, best_val, "#1e88e5", 2
    if best_val >= 7.0:  return best_desc, best_val, "#29b6f6", 1
    if best_val >= 3.0:  return best_desc, best_val, "#4dd0e1", 0
    if best_val >= 0.5:  return best_desc, best_val, "#90a4ae", 0

    return "降水なし", 0.0, "#78909c", 0

def get_color_for_value(val):
    """グラフの棒グラフ表示用カラーコードを取得します。"""
    if val >= 80.0: return "#ab47bc"
    if val >= 65.0: return "#e53935"
    if val >= 40.0: return "#f57c00"
    if val >= 25.0: return "#f5a623"
    if val >= 15.0: return "#1e88e5"
    if val >= 7.0:  return "#29b6f6"
    if val >= 3.0:  return "#4dd0e1"
    if val > 0.0:   return "#90a4ae"
    return "#e0e0e0"

def get_nice_step(raw_max, steps=5):
    """グラフY軸の目盛り間隔（stepSize）をキリの良い数値に調整します。"""
    raw_step = raw_max / steps
    nice_steps = [1, 2, 5, 10, 20, 25, 30, 40, 50, 75, 100, 125, 150, 200, 250, 300, 400, 500, 1000]
    for n in nice_steps:
        if n >= raw_step:
            return n
    return math.ceil(raw_step)

def cleanup_old_charts(charts_dir="charts", retention_hours=168):
    """
    charts/ ディレクトリ内をスキャンし、指定保持時間(デフォルト168時間 = 7日間)を過ぎた
    古いグラフPNG画像を自動削除します。
    """
    if not os.path.exists(charts_dir):
        return

    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)

    for filename in os.listdir(charts_dir):
        if filename.startswith("chart_") and (filename.endswith(".png") or filename.endswith(".gif")):
            file_path = os.path.join(charts_dir, filename)
            try:
                # ファイルの最終更新日時を取得
                mtime = datetime.fromtimestamp(os.path.getmtime(file_path), tz=jst)
                # 168時間（7日間）以上経過している場合は削除
                if (now - mtime).total_seconds() > (retention_hours * 3600):
                    os.remove(file_path)
                    print(f"古いグラフ画像を削除しました: {filename}")
            except Exception as e:
                print(f"画像削除エラー ({filename}): {e}")

def push_chart_to_github(output_path, filename):
    """
    生成された画像ファイルを Git にコミット＆プッシュし、
    CDN (raw.githubusercontent.com) に反映されるまで待機して URL を返します。
    """
    repo = os.environ.get("GITHUB_REPOSITORY")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    if not repo or not os.path.exists(output_path):
        return None

    try:
        # 1. Git コミット & プッシュを実行
        subprocess.run(["git", "config", "--local", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "--local", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", "-A", "state.json", "charts/"], check=True)
        
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        if status.stdout.strip():
            subprocess.run(["git", "commit", "-m", f"Chore: Upload {filename} [skip ci]"], check=True)
            
            # --- 追記: push 直前にリモートの最新変更を引き込んで競合を回避する ---
            subprocess.run(["git", "pull", "origin", "main", "--rebase"], check=True)
            
            subprocess.run(["git", "push"], check=True)
            print(f"Git push 完了: {filename}")

        # 2. CDN 反映をポーリング確認 (最大 60 秒待機)
        raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/charts/{filename}"
        print("CDNへの画像反映を確認中...")
        for i in range(60):
            try:
                res = requests.head(raw_url, timeout=3)
                if res.status_code == 200:
                    print(f"CDN反映確認完了 (HTTP 200 / {i+1}秒経過)")
                    return raw_url
            except Exception:
                pass
            time.sleep(1)

        print("警告: CDNへの反映が60秒以内に完了しなかったため、画像URLを破棄して通知のみ送信します。")
        return None  # 404画像の送信によるカード空欄化を防ぐため None を返す

    except Exception as e:
        print(f"Git push または CDN確認エラー: {e}")
        return None

def generate_chart_url(hourly_rain_list, current_rain_val=0.0):
    """
    generate_chart.js を実行して GIF を生成し、Git push 後に CDN 反映済み URL を返します。
    """
    cleanup_old_charts(retention_hours=168)

    all_rain = [current_rain_val] + hourly_rain_list
    labels = [str(i) for i in range(len(all_rain))]
    bar_colors = [get_color_for_value(val) for val in all_rain]
    
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

    step_y2 = get_nice_step(max(max_cum * 1.01, 10.0), steps)
    y2_max = step_y2 * steps

    jst = timezone(timedelta(hours=9))
    timestamp_str = datetime.now(jst).strftime("%Y%m%d_%H%M%S_%f")[:18]
    filename = f"chart_{timestamp_str}.gif"
    output_path = os.path.join("charts", filename)

    params = {
        "labels": labels,
        "hourlyRain": all_rain,
        "cumulativeRain": cumulative_rain,
        "barColors": bar_colors,
        "y1Max": y1_max,
        "stepY1": step_y1,
        "y2Max": y2_max,
        "stepY2": step_y2,
        "outputPath": output_path
    }

    try:
        cmd = ["node", "generate_chart.js", json.dumps(params)]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        print("【Node.js 実行標準出力】:", result.stdout.strip())

        # ローカル生成後に Git push と CDN 200 確認を実行
        uploaded_url = push_chart_to_github(output_path, filename)
        if uploaded_url:
            return uploaded_url

    except subprocess.CalledProcessError as e:
        print(f"【Node.js 実行エラー詳細】 ReturnCode: {e.returncode}")
        print(f"【Node.js stderr】: {e.stderr}")
    except Exception as e:
        print(f"【予期せぬ例外】: {type(e).__name__} - {e}")

    # フォールバック (QuickChart)
    chart_config = {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": [
                {"type": "line", "data": all_rain, "borderColor": "transparent", "backgroundColor": "transparent", "pointRadius": 0, "yAxisID": "y1", "order": 0, "datalabels": {"display": [val >= 0.5 for val in all_rain], "anchor": "end", "align": "end", "offset": -2, "color": "#111111", "font": {"size": 20, "family": "LINE Seed JP", "weight": "bold"}, "textStrokeColor": "#ffffff", "textStrokeWidth": 4}},
                {"type": "line", "data": cumulative_rain, "borderColor": "#7B1FA2", "borderWidth": 4, "pointRadius": 0, "fill": True, "backgroundColor": "rgba(123, 31, 162, 0.08)", "yAxisID": "y2", "order": 1, "datalabels": {"display": False}},
                {"type": "line", "data": cumulative_rain, "borderColor": "rgba(255, 255, 255, 0.7)", "borderWidth": 10, "pointRadius": 0, "fill": False, "yAxisID": "y2", "order": 2, "datalabels": {"display": False}},
                {"type": "bar", "data": all_rain, "backgroundColor": bar_colors, "borderRadius": 6, "yAxisID": "y1", "order": 3, "datalabels": {"display": False}}
            ]
        },
        "options": {
            "plugins": {"title": {"display": True, "text": title_text, "color": "#111111", "font": {"size": 19, "family": "Noto Sans CJK JP", "weight": "bold"}, "padding": 12}, "legend": {"display": False}, "datalabels": {"display": True}},
            "layout": {"padding": {"top": 5, "left": 10, "right": 10, "bottom": 5}},
            "scales": {
                "x": {"grid": {"display": False}, "title": {"display": True, "text": "時間後", "color": "#111111", "font": {"size": 19, "family": "Noto Sans CJK JP", "weight": "bold"}}, "ticks": {"color": "#111111", "font": {"size": 18, "family": "Noto Sans CJK JP"}, "maxRotation": 0}},
                "y1": {"type": "linear", "position": "left", "min": 0, "max": y1_max, "ticks": {"stepSize": step_y1, "color": "#111111", "font": {"size": 19, "family": "LINE Seed JP"}}, "grid": {"color": "#bdbdbd"}, "border": {"dash": [2, 3]}},
                "y2": {"type": "linear", "position": "right", "min": 0, "max": y2_max, "ticks": {"stepSize": step_y2, "color": "#111111", "font": {"size": 19, "family": "LINE Seed JP"}}, "grid": {"drawOnChartArea": True, "color": "#bdbdbd"}, "border": {"dash": [2, 3]}}
            }
        }
    }
    compact_json = json.dumps(chart_config, separators=(',', ':'))
    encoded = urllib.parse.quote(compact_json)
    return f"https://quickchart.io/chart?v=4&c={encoded}&w=600&h=300&bkg=white&devicePixelRatio=3&f=LINE+Seed+JP"

# =========================================================
# 4. データ取得・カード構築・送信処理
# =========================================================
def parse_jma_time(time_str):
    """気象庁の日時文字列(YYYYMMDDHHMMSS)を datetime オブジェクトに変換します。"""
    return datetime.strptime(time_str, "%Y%m%d%H%M%S")

def fetch_10min_future_rain(lat, lon, zoom=ZOOM_LEVEL):
    """
    nowc (ナウキャスト) APIから「10分後」の雨量予測データを取得します。
    【10分後ロジックの補足】
    targetTimes_N1.json 内の validtime から、basetime（観測時刻）+ 10分後となるコマを抽出し、
    雨雲が直近接近してくるかどうかの早め対策判断に使用します。
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        url_target = "https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N1.json"
        res = requests.get(url_target, headers=headers, timeout=10)
        if res.status_code != 200:
            return "降水なし", 0.0, "#78909c", 0, None, None

        target_times = res.json()
        if not target_times:
            return "降水なし", 0.0, "#78909c", 0, None, None

        # 観測基準時刻（basetime）の取得
        base_dt = parse_jma_time(target_times[0]["basetime"])
        target_10min_dt = base_dt + timedelta(minutes=10)

        # validtime が basetime + 10分 に最も近い予報コマを特定
        best_match = None
        min_diff = float("inf")
        for t in target_times:
            v_dt = parse_jma_time(t["validtime"])
            diff = abs((v_dt - target_10min_dt).total_seconds())
            if diff < min_diff:
                min_diff = diff
                best_match = t

        if best_match and min_diff <= 300: # 5分誤差以内のコマを正とみなす
            basetime = best_match["basetime"]
            validtime = best_match["validtime"]
            xtile, ytile, px, py = latlon_to_tile(lat, lon, zoom)
            
            tile_url = f"https://www.jma.go.jp/bosai/jmatile/data/nowc/{basetime}/none/{validtime}/surf/hrpns/{zoom}/{xtile}/{ytile}.png"
            t_res = requests.get(tile_url, headers=headers, timeout=10)
            if t_res.status_code == 200:
                img = Image.open(BytesIO(t_res.content)).convert("RGBA")
                pixel_color = img.getpixel((px, py))
                rain_desc, rain_val, color_code, rank = rgb_to_rainfall(pixel_color)
                return rain_desc, rain_val, color_code, rank, basetime, validtime

    except Exception as e:
        print(f"10分後ナウキャスト取得エラー: {e}")

    return "降水なし", 0.0, "#78909c", 0, None, None

def get_future_cumulative_rain_data(lat, lon, current_rain_val=0.0, zoom=ZOOM_LEVEL):
    """
    気象庁 rasrf (長期的予測 API) の targetTimes.json メタデータを解析し、
    指定座標の今後15時間分の毎時予測雨量、3時間/15時間積算雨量、グラフURL、取得詳細ログを返します。
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    details = []

    try:
        url_target = "https://www.jma.go.jp/bosai/jmatile/data/rasrf/targetTimes.json"
        res = requests.get(url_target, headers=headers, timeout=10)
        
        if res.status_code != 200:
            for idx in range(1, 16):
                details.append({
                    "idx": idx,
                    "validtime": "----",
                    "status_code": res.status_code,
                    "rain_val": None,
                    "status_desc": f"メタデータ取得失敗 (HTTP {res.status_code})"
                })
            return 0.0, 0.0, [0.0]*15, "", details

        raw_data = res.json()
        xtile, ytile, px, py = latlon_to_tile(lat, lon, zoom)
        
        if not raw_data:
            for idx in range(1, 16):
                details.append({
                    "idx": idx,
                    "validtime": "----",
                    "status_code": "EMPTY",
                    "rain_val": None,
                    "status_desc": "メタデータが空データ"
                })
            return 0.0, 0.0, [0.0]*15, "", details

        # 過去解析データ(basetime == validtime)を除外し、未来の予測コマ(rasrf)のみ抽出
        valid_frames = [
            item for item in raw_data 
            if "rasrf" in item.get("elements", []) and item.get("basetime") != item.get("validtime")
        ]
        valid_frames.sort(key=lambda x: x["validtime"])

        target_frames = valid_frames[:15]
        hourly_rain_list = []

        # 各1時間予報タイルのピクセル色を取得・解析
        for idx, item in enumerate(target_frames, start=1):
            basetime = item["basetime"]
            validtime = item["validtime"]
            member = item.get("member", "none")
            tile_url = f"https://www.jma.go.jp/bosai/jmatile/data/rasrf/{basetime}/{member}/{validtime}/surf/rasrf/{zoom}/{xtile}/{ytile}.png"
            
            try:
                t_res = requests.get(tile_url, headers=headers, timeout=5)
                if t_res.status_code == 200:
                    img = Image.open(BytesIO(t_res.content)).convert("RGBA")
                    pixel_color = img.getpixel((px, py))
                    _, rain_val, _, _ = rgb_to_rainfall(pixel_color)
                    hourly_rain_list.append(rain_val)
                    details.append({
                        "idx": idx,
                        "validtime": validtime,
                        "status_code": 200,
                        "rain_val": rain_val,
                        "status_desc": f"HTTP 200 (正常取得: {rain_val}mm/h)"
                    })
                else:
                    hourly_rain_list.append(0.0)
                    details.append({
                        "idx": idx,
                        "validtime": validtime,
                        "status_code": t_res.status_code,
                        "rain_val": None,
                        "status_desc": f"HTTP {t_res.status_code} (画像エラー)"
                    })
            except Exception as req_err:
                hourly_rain_list.append(0.0)
                details.append({
                    "idx": idx,
                    "validtime": validtime,
                    "status_code": "EXCEPT",
                    "rain_val": None,
                    "status_desc": f"通信例外 ({type(req_err).__name__})"
                })

        # 不足分を0.0でパディング
        while len(hourly_rain_list) < 15:
            idx = len(hourly_rain_list) + 1
            hourly_rain_list.append(0.0)
            details.append({
                "idx": idx,
                "validtime": "----",
                "status_code": "NODATA",
                "rain_val": None,
                "status_desc": "該当時間帯のデータ定義なし"
            })
            
        all_rain = [current_rain_val] + hourly_rain_list
        cum_3h = round(sum(all_rain[:4]), 1)
        cum_15h = round(sum(all_rain), 1)
        chart_url = generate_chart_url(hourly_rain_list, current_rain_val)
        
        return cum_3h, cum_15h, hourly_rain_list, chart_url, details
    except Exception as e:
        for idx in range(1, 16):
            details.append({
                "idx": idx,
                "validtime": "----",
                "status_code": "FATAL",
                "rain_val": None,
                "status_desc": f"致命的エラー ({e})"
            })
        return 0.0, 0.0, [0.0]*15, "", details

def send_google_chat_card(webhook_url, lat, lon, title_text, formatted_text, icon_url, chart_url=None):
    """Google Chat Webhook API を利用して、カード形式（CardsV2）の通知メッセージを送信します。"""
    jma_url = f"https://www.jma.go.jp/bosai/kaikotan/#lat:{lat}/lon:{lon}/zoom:11"
    activated_sludge_url = os.environ.get("ACTIVATED_SLUDGE_URL", "")
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
            "buttons": [
                {
                    "text": "<b>雨雲レーダー</b>を開く🌧️",
                    "color": {"red": 0.82, "green": 0.90, "blue": 0.98, "alpha": 1.0},
                    "onClick": {"openLink": {"url": jma_url}}
                },
                {
                    "text": "活性汚泥　見えるか❔",
                    "color": {"red": 0.90, "green": 0.95, "blue": 0.88, "alpha": 1.0},
                    "onClick": {"openLink": {"url": activated_sludge_url}}
                }
            ]
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
    """
    本番運用時のエントリーポイント関数。
    1. 環境変数チェックおよび動作可能時間判定（営業時間・祝日ガード）
    2. 現在地の10分後予測雨量(nowc)を取得（早め対策ロジック）
    3. 状態管理(state.json)のランク変化に基づき Google Chat 通知を判定・送信
    4. 17時台の夜間雨量アサート条件を満たした場合の特別通知
    """
    webhook_url = os.environ.get("CHAT_WEBHOOK_URL")
    lat_str = os.environ.get("TARGET_LAT")
    lon_str = os.environ.get("TARGET_LON")

    if not webhook_url or not lat_str or not lon_str:
        sys.exit(1)

    lat = float(lat_str)
    lon = float(lon_str)

    init_state_file()

    # 稼働時間外判定（時間外の場合は前回雨量をリセットして正常終了）
    if not is_operating_time():
        if load_state()[0] > 0:
            save_state(0.0, 0, 0, "NONE", load_state()[4])
        sys.exit(0)

    last_rain_val, last_rank, last_notified_rank, last_notified_type, last_evening_alert_date, is_fresh_start = load_state()

    # 10分後の雨量予測データ（nowc）を取得（0hの判定データ）
    rain_desc, rain_val, color_code, current_rank, _, _ = fetch_10min_future_rain(lat, lon, ZOOM_LEVEL)

    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    today_str = now.strftime("%Y-%m-%d")

    sent_amedes_in_this_run = False

    # 条件1: スクリプト起動初回で雨が降っている場合
    if is_fresh_start and current_rank >= 1:
        save_state(rain_val, current_rank, current_rank, "RAINY", last_evening_alert_date)

    # 条件2: 降り始めまたは雨量ランクが上昇した場合の「アメデス」通知
    elif current_rank >= 1 and (last_notified_type != "RAINY" or current_rank > last_notified_rank):
        _, cum_15h, _, chart_url, _ = get_future_cumulative_rain_data(lat, lon, rain_val, ZOOM_LEVEL)
        val_str = str(rain_val) if rain_val < 1.0 else str(int(rain_val))
        cum_15h_int = int(cum_15h)
        
        formatted_text = (
            f"<font color=\"{color_code}\"><b>{rain_desc}</b> {val_str} mm/h</font><br>"
            # f"<font color=\"#757575\">今後15時間積算 {cum_15h_int} mm</font>"
        )
        
        send_google_chat_card(webhook_url, lat, lon, "アメデス", formatted_text, ICON_RAINY, chart_url)
        save_state(rain_val, current_rank, current_rank, "RAINY", last_evening_alert_date)
        sent_amedes_in_this_run = True

    # 条件3: 雨が止んだ場合の「雨上がりの予感」通知
    elif current_rank == 0 and last_notified_type == "RAINY":
        _, _, _, chart_url, _ = get_future_cumulative_rain_data(lat, lon, rain_val, ZOOM_LEVEL)
        formatted_text = f"<font color=\"{color_code}\"><b>{rain_desc}</b></font>"
        send_google_chat_card(webhook_url, lat, lon, "雨上がりの予感", formatted_text, ICON_RAINBOW, chart_url)
        save_state(0.0, 0, 0, "WEAK", last_evening_alert_date)

    else:
        save_state(rain_val, rain_val, last_notified_rank, last_notified_type, last_evening_alert_date)

    # 条件4: 夕方（17時0分〜10分）の「今宵アメデス」積算雨量警告通知
    if now.hour == 17 and (0 <= now.minute <= 10) and not sent_amedes_in_this_run and last_evening_alert_date != today_str:
        _, cum_15h, _, chart_url, _ = get_future_cumulative_rain_data(lat, lon, rain_val, ZOOM_LEVEL)
        if cum_15h >= NIGHT_RAIN_THRESHOLD:
            cum_15h_int = int(cum_15h)
            formatted_text = f"17～翌8時の積算雨量 <b>{cum_15h_int} mm</b>"
            send_google_chat_card(webhook_url, lat, lon, "今宵アメデス", formatted_text, ICON_NIGHT_RAIN, today_str)
            save_state(rain_val, current_rank, last_notified_rank, last_notified_type, today_str)

    print("Execution completed successfully.")

# =========================================================
# 6. 原因切り分け用 強制データ取得＆Chat送信テスト関数
# =========================================================
def find_active_rain_location():
    """
    日本全国の主要候補地および雨雲タイルをランダムな順序で探索し、
    現在〜10分後に雨が降っている（rain_val > 0.0）地点の (地名, lat, lon) を返します。
    """
    candidate_spots = [
        ("札幌", 43.0618, 141.3545), ("函館", 41.7687, 140.7288), ("青森", 40.8244, 140.7400),
        ("秋田", 39.7186, 140.1024), ("仙台", 38.2682, 140.8694), ("新潟", 37.9161, 139.0364),
        ("金沢", 36.5613, 136.6562), ("東京", 35.6762, 139.6503), ("八丈島", 33.1112, 139.7902),
        ("静岡", 34.9756, 138.3828), ("名古屋", 35.1815, 136.9066), ("大阪", 34.6937, 135.5023),
        ("和歌山", 34.2260, 135.1675), ("鳥取", 35.5011, 134.2351), ("広島", 34.3853, 132.4553),
        ("高知", 33.5597, 133.5311), ("松山", 33.8416, 132.7657), ("福岡", 33.5902, 130.4017),
        ("長崎", 32.7503, 129.8777), ("鹿児島", 31.5966, 130.5571), ("奄美", 28.3772, 129.4950),
        ("那覇", 26.2124, 127.6809), ("石垣島", 24.3448, 124.1572)
    ]
    random.shuffle(candidate_spots)

    try:
        for name, lat, lon in candidate_spots:
            _, rain_val, _, _, _, _ = fetch_10min_future_rain(lat, lon, ZOOM_LEVEL)
            if rain_val > 0.0:
                return f"{name}周辺", lat, lon

    except Exception as e:
        print(f"降雨エリア探索エラー: {e}")

    return None, None, None

def test_forced_notification():
    """
    【デバッグ・検証用関数】
    10分後予測データの検証ができるよう、観測時刻(Base)と予測時刻(Valid=+10分)を明示して
    気象庁Web GUI照合用リンク付きのカードメッセージを Google Chat へ送信します。
    """
    webhook_url = os.environ.get("CHAT_WEBHOOK_URL")
    if not webhook_url:
        print("エラー: CHAT_WEBHOOK_URL が設定されていません。")
        return

    print("=== 全国雨域自動スキャン開始 ===")

    location_name, lat, lon = find_active_rain_location()

    if not lat or not lon:
        print("現在、日本全国の主要監視エリアに降雨が検出されませんでした（全域晴れ/薄くもり）。")
        lat = float(os.environ.get("TARGET_LAT", "35.1815"))
        lon = float(os.environ.get("TARGET_LON", "136.9066"))
        location_name = "指定座標(降雨なし)"

    print(f"検証対象地点決定: {location_name} (緯度:{lat}, 経度:{lon})")

    # 10分後予測データの取得および検証メタデータの取得
    rain_desc, rain_val, color_code, _, basetime, validtime = fetch_10min_future_rain(lat, lon, ZOOM_LEVEL)

    cum_3h, cum_15h, hourly_rain_list, chart_url, details = get_future_cumulative_rain_data(lat, lon, rain_val, ZOOM_LEVEL)
    jma_gui_url = f"https://www.jma.go.jp/bosai/kaikotan/#lat:{lat}/lon:{lon}/zoom:11"

    val_str = str(rain_val) if rain_val < 1.0 else str(int(rain_val))
    
    # 10分後の検証時刻表示
    v_time_str = f"{validtime[8:10]}:{validtime[10:12]}" if validtime else "10分後"
    b_time_str = f"{basetime[8:10]}:{basetime[10:12]}" if basetime else "観測時"

    logs = [
        f"<b>【10分後予測 検証デバッグ配信】</b>",
        f"<b>検証対象地域</b>: 📍 <b>{location_name}</b>",
        f"<b>10分後予報 ({v_time_str} / Base:{b_time_str})</b>: <font color=\"{color_code}\"><b>{rain_desc}</b> {val_str} mm/h</font>",
        f"<b>気象庁Web GUI確認リンク</b>: <a href=\"{jma_gui_url}\">雨雲の動き(公式GUI)で画面照合</a><br>",
        f"<b>【15時間予測値およびHTTPレスポンス詳細】</b>"
    ]

    for d in details:
        idx = d["idx"]
        vt = d["validtime"]
        code = d["status_code"]
        rv = d["rain_val"]
        
        if code == 200:
            if rv is not None and rv > 0.0:
                logs.append(f"・+{idx:02d}h ({vt}): <font color=\"#2e7d32\">HTTP 200</font> | <b><font color=\"#ff0000\">{rv} mm/h</font></b>")
            else:
                logs.append(f"・+{idx:02d}h ({vt}): <font color=\"#2e7d32\">HTTP 200</font> | {rv} mm/h")
        else:
            logs.append(f"・+{idx:02d}h ({vt}): <font color=\"#e53935\"><b>HTTP {code}</b></font>")

    logs.append(f"<br><b>15時間積算雨量</b>: {int(cum_15h)} mm")
    formatted_text = "<br>".join(logs)

    send_google_chat_card(
        webhook_url=webhook_url,
        lat=lat,
        lon=lon,
        title_text=f"🌧️ 10分後予測検証 ({location_name})",
        formatted_text=formatted_text,
        icon_url=ICON_RAINY,
        chart_url=chart_url
    )

    print(f"送信完了: {location_name} (lat:{lat}, lon:{lon}) の10分後予測データをGoogle Chatへ送信しました。")

# =========================================================
# 7. スクリプト実行エントリーポイント
# =========================================================
if __name__ == "__main__":
    
    # ---------------------------------------------------------
    # 実行モード選択
    # ---------------------------------------------------------
    
    # 【本番運用モード】（時間・曜日ガードあり）
    # main()
    
    # 【テスト検証モード】（時間・曜日・降水量条件を全バイパスしてチャット通知を強制送信）
    test_forced_notification()
