const { createCanvas, registerFont } = require('canvas');
const { Chart } = require('chart.js/auto');
const ChartDataLabels = require('chartjs-plugin-datalabels');
const fs = require('fs');
const path = require('path');

// --- 1. フォントの登録 (run.yml で自動ダウンロードされたファイルを指定) ---
const fontLineBoldPath = '/usr/share/fonts/truetype/line-seed/LINESeedJP-Bold.ttf';
const fontNotoRegularPath = '/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf';
const fontNotoBoldPath = '/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf';

if (fs.existsSync(fontLineBoldPath)) {
  registerFont(fontLineBoldPath, { family: 'LINE Seed JP', weight: 'bold' });
}
if (fs.existsSync(fontNotoRegularPath)) {
  registerFont(fontNotoRegularPath, { family: 'Noto Sans', weight: 'normal' });
}
if (fs.existsSync(fontNotoBoldPath)) {
  registerFont(fontNotoBoldPath, { family: 'Noto Sans', weight: 'bold' });
}

// --- 2. 引数の取得 (Python側からJSON文字列を受取) ---
const inputJson = process.argv[2];
if (!inputJson) {
  console.error("エラー: グラフデータ(JSON)が渡されていません。");
  process.exit(1);
}

const params = JSON.parse(inputJson);
const {
  labels,
  hourlyRain,
  cumulativeRain,
  barColors,
  y1Max,
  stepY1,
  y2Max,
  stepY2,
  titleText,
  outputPath
} = params;

// 出力先ディレクトリの自動作成
const dir = path.dirname(outputPath);
if (!fs.existsSync(dir)) {
  fs.mkdirSync(dir, { recursive: true });
}

// --- 3. Canvas の作成 ---
const width = 600;
const height = 300;
const canvas = createCanvas(width, height);
const ctx = canvas.getContext('2d');

// --- 4. Chart.js 設定構築 ---
const datalabelDisplay = hourlyRain.map(val => val >= 0.5);

const chartConfig = {
  type: 'bar',
  data: {
    labels: labels,
    datasets: [
      {
        type: 'line',
        label: '時間雨量ラベル用ダミー',
        data: hourlyRain,
        borderColor: 'transparent',
        backgroundColor: 'transparent',
        pointRadius: 0,
        yAxisID: 'y1',
        order: 0,
        datalabels: {
          display: datalabelDisplay,
          anchor: 'end',
          align: 'end',
          offset: -2,
          color: '#111111',
          // 棒グラフの数値数値ラベル: Noto Sans (Bold)
          font: { size: 20, family: 'Noto Sans', weight: 'bold' },
          textStrokeColor: '#ffffff',
          textStrokeWidth: 4
        }
      },
      {
        type: 'line',
        label: '積算雨量(mm)',
        data: cumulativeRain,
        borderColor: '#7B1FA2',
        borderWidth: 4,
        pointRadius: 0,
        fill: true,
        backgroundColor: 'rgba(123, 31, 162, 0.08)',
        yAxisID: 'y2',
        order: 1,
        datalabels: { display: false }
      },
      {
        type: 'line',
        label: '積算雨量_白縁取り',
        data: cumulativeRain,
        borderColor: 'rgba(255, 255, 255, 0.7)',
        borderWidth: 10,
        pointRadius: 0,
        fill: false,
        yAxisID: 'y2',
        order: 2,
        datalabels: { display: false }
      },
      {
        type: 'bar',
        label: '時間雨量(mm/h)',
        data: hourlyRain,
        backgroundColor: barColors,
        borderRadius: 6,
        yAxisID: 'y1',
        order: 3,
        datalabels: { display: false }
      }
    ]
  },
  plugins: [ChartDataLabels],
  options: {
    animation: false,
    responsive: false,
    plugins: {
      title: {
        display: true,
        text: titleText,
        color: '#111111',
        // 日本語タイトル: LINE Seed JP (Bold)
        font: { size: 19, family: 'LINE Seed JP', weight: 'bold' },
        padding: 12
      },
      legend: { display: false },
      datalabels: { display: true }
    },
    layout: { padding: { top: 5, left: 10, right: 10, bottom: 5 } },
    scales: {
      x: {
        grid: { display: false },
        title: {
          display: true,
          text: '時間後',
          color: '#111111',
          // 日本語軸タイトル: LINE Seed JP (Bold)
          font: { size: 19, family: 'LINE Seed JP', weight: 'bold' }
        },
        ticks: {
          color: '#111111',
          // 数字目盛り: Noto Sans (Regular)
          font: { size: 18, family: 'Noto Sans', weight: 'normal' },
          maxRotation: 0
        }
      },
      y1: {
        type: 'linear',
        position: 'left',
        min: 0,
        max: y1Max,
        ticks: {
          stepSize: stepY1,
          color: '#111111',
          // 数字目盛り: Noto Sans (Regular)
          font: { size: 19, family: 'Noto Sans', weight: 'normal' }
        },
        grid: { color: '#bdbdbd' },
        border: { dash: [2, 3] }
      },
      y2: {
        type: 'linear',
        position: 'right',
        min: 0,
        max: y2Max,
        ticks: {
          stepSize: stepY2,
          color: '#111111',
          // 数字目盛り: Noto Sans (Regular)
          font: { size: 19, family: 'Noto Sans', weight: 'normal' }
        },
        grid: { drawOnChartArea: true, color: '#bdbdbd' },
        border: { dash: [2, 3] }
      }
    }
  }
};

// --- 5. 描画 & PNGファイル書き出し ---
new Chart(ctx, chartConfig);
const buffer = canvas.toBuffer('image/png');
fs.writeFileSync(outputPath, buffer);
console.log(`グラフ生成完了: ${outputPath}`);
