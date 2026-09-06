const { createCanvas, registerFont } = require('canvas');
const { Chart } = require('chart.js/auto');
const ChartDataLabels = require('chartjs-plugin-datalabels');
const GIFEncoder = require('gifencoder');
const fs = require('fs');
const path = require('path');

// --- 1. ローカルフォントの登録 ---
const fontLineBoldPath = path.join(__dirname, 'fonts', 'LINESeedJP-Bold.ttf');
const fontOpenSansCondRegularPath = path.join(__dirname, 'fonts', 'OpenSans_Condensed-Regular.ttf');
const fontOpenSansCondBoldPath = path.join(__dirname, 'fonts', 'OpenSans_Condensed-Bold.ttf');

if (fs.existsSync(fontLineBoldPath)) {
  registerFont(fontLineBoldPath, { family: 'LINE Seed JP', weight: 'bold' });
}
if (fs.existsSync(fontOpenSansCondRegularPath)) {
  registerFont(fontOpenSansCondRegularPath, { family: 'Open Sans Condensed', weight: 'normal' });
}
if (fs.existsSync(fontOpenSansCondBoldPath)) {
  registerFont(fontOpenSansCondBoldPath, { family: 'Open Sans Condensed Bold', weight: 'bold' });
}

// --- 2. 背景を絶対に白にするカスタムプラグイン ---
const whiteBackgroundPlugin = {
  id: 'customCanvasBackgroundColor',
  beforeDraw: (chart) => {
    const { ctx, width, height } = chart;
    ctx.save();
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, width, height);
    ctx.restore();
  }
};

// --- 出典表示用カスタムプラグイン ---
const sourceTextPlugin = {
  id: 'sourceText',
  afterDraw: (chart) => {
    const { ctx, chartArea, height } = chart;
    ctx.save();

    ctx.font = 'bold 14px "LINE Seed JP"';
    ctx.fillStyle = '#999999';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'bottom';

    const x = chartArea.right;
    const y = height - 5;

    ctx.fillText('出典: 気象庁', x, y);
    ctx.restore();
  }
};

// 上部左右タイトルの2色描画プラグイン
const customTitlePlugin = {
  id: 'customTitle',
  afterDraw: (chart) => {
    const { ctx, chartArea } = chart;
    ctx.save();

    ctx.font = 'bold 21px "LINE Seed JP"';
    ctx.textBaseline = 'top';

    const y = 8;
    const labelOffset = 32;

    // 1. 左タイトル：数字「10」の左端ラインに合わせる
    ctx.fillStyle = '#555555';
    ctx.textAlign = 'left';
    ctx.fillText('▼\u2009棒\u200A: 時\u200A間\u200A雨\u200A量 mm/h', chartArea.left - labelOffset, y);

    // 2. 右タイトル：数字「25」の右端ラインに合わせる
    ctx.fillStyle = '#7B1FA2';
    ctx.textAlign = 'right';
    ctx.fillText('折れ線\u200A: 積\u200A算\u200A雨\u200A量 mm\u2009▼', chartArea.right + labelOffset, y);

    ctx.restore();
  }
};

// --- 3. 引数の取得 ---
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

const gifOutputPath = outputPath.replace(/\.png$/, '.gif');
const dir = path.dirname(gifOutputPath);
if (!fs.existsSync(dir)) {
  fs.mkdirSync(dir, { recursive: true });
}

// --- 4. GIFEncoder と Canvas の初期化 ---
const width = 600;
const height = 300;

const encoder = new GIFEncoder(width, height);
encoder.createReadStream().pipe(fs.createWriteStream(gifOutputPath));

encoder.start();
encoder.setRepeat(0); // 無限ループ
encoder.setQuality(10);

const canvas = createCanvas(width, height);
const ctx = canvas.getContext('2d');

function easeOutCubic(t) {
  return 1 - Math.pow(1 - t, 3);
}

// --- 5. 時系列アニメーション用フレーム生成処理 ---
const totalFrames = 40;
const dataLength = hourlyRain.length;

for (let i = 1; i <= totalFrames; i++) {
  if (i === totalFrames) {
    encoder.setDelay(15000);
  } else {
    encoder.setDelay(50);
  }

  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, width, height);

  const globalProgress = i / totalFrames;

  const currentHourly = [];
  const currentCumulative = [];
  const localProgresses = [];

  for (let j = 0; j < dataLength; j++) {
    const startThreshold = (j / dataLength) * 0.7;
    if (globalProgress <= startThreshold) {
      currentHourly.push(0);
      currentCumulative.push(null);
      localProgresses.push(0);
    } else {
      const localProgress = Math.min(1.0, (globalProgress - startThreshold) / 0.3);
      const easedProgress = easeOutCubic(localProgress);

      currentHourly.push(hourlyRain[j] * easedProgress);

      if (easedProgress >= 0.75) {
        currentCumulative.push(cumulativeRain[j]);
      } else {
        currentCumulative.push(null);
      }

      localProgresses.push(localProgress);
    }
  }

  const datalabelDisplay = (context) => {
    const targetVal = hourlyRain[context.dataIndex];
    const progress = localProgresses[context.dataIndex];
    return targetVal > 0 && progress >= 0.5;
  };

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
            font: { size: 22, family: 'Open Sans Condensed Bold', weight: 'bold' },
            textStrokeColor: '#ffffff',
            textStrokeWidth: 6,
            formatter: (value) => value
          }
        },
        {
          type: 'line',
          label: '積算雨量(mm)',
          data: currentCumulative,
          borderColor: '#7B1FA2',
          borderWidth: 4,
          pointRadius: 0,
          fill: true,
          backgroundColor: 'rgba(123, 31, 162, 0.08)',
          spanGaps: false,
          yAxisID: 'y2',
          order: 1,
          datalabels: {
            display: (context) => {
              const isLastIndex = context.dataIndex === context.dataset.data.length - 1;
              const val = context.dataset.data[context.dataIndex];
              return isLastIndex && val !== null && val !== undefined;
            },
            align: 315,   // 右上
            anchor: 'end',    // データポイントの外側端
            offset: 4,        // 右方向への距離
            color: '#ffffff',
            backgroundColor: '#7B1FA2',
            borderRadius: 4,
            padding: { top: 3, bottom: 3, left: 6, right: 6 },
            font: {
              size: 22,
              family: 'Open Sans Condensed Bold',
              weight: 'bold'
            },
            formatter: (value) => Math.round(value)
          }
        },
        {
          type: 'line',
          label: '積算雨量_白縁取り',
          data: currentCumulative,
          borderColor: 'rgba(255, 255, 255, 0.7)',
          borderWidth: 10,
          pointRadius: 0,
          fill: false,
          spanGaps: false,
          yAxisID: 'y2',
          order: 2,
          datalabels: { display: false }
        },
        {
          type: 'bar',
          label: '時間雨量(mm/h)',
          data: currentHourly,
          backgroundColor: barColors,
          borderRadius: 6,
          yAxisID: 'y1',
          order: 3,
          datalabels: { display: false }
        }
      ]
    },
    plugins: [ChartDataLabels, whiteBackgroundPlugin, sourceTextPlugin, customTitlePlugin],
    options: {
      animation: false,
      responsive: false,
      plugins: {
        title: { display: false },
        legend: { display: false },
        datalabels: { display: true }
      },
      layout: {
        padding: {
          top: 50,
          left: 10,
          right: 10,
          bottom: 5
        }
      },
      scales: {
        x: {
          grid: { display: false },
          title: {
            display: true,
            text: '時\u200A間\u200A後',
            color: '#555555',
            font: { size: 21, family: 'LINE Seed JP', weight: 'bold' }
          },
          ticks: {
            color: '#111111',
            font: { size: 23, family: 'Open Sans Condensed', weight: 'normal' },
            maxRotation: 0,
            padding: -2
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
            font: { size: 23, family: 'Open Sans Condensed', weight: 'normal' }
          },
          grid: { color: '#bdbdbd' },
          border: { display: false, dash: [3, 4] }
        },
        y2: {
          type: 'linear',
          position: 'right',
          min: 0,
          max: y2Max,
          ticks: {
            stepSize: stepY2,
            color: '#7B1FA2',
            font: { size: 23, family: 'Open Sans Condensed', weight: 'normal' }
          },
          grid: { drawOnChartArea: true, color: '#bdbdbd' },
          border: { display: false, dash: [3, 4] }
        }
      }
    }
  };

  const chart = new Chart(ctx, chartConfig);
  encoder.addFrame(ctx);
  chart.destroy();
}

encoder.finish();
console.log(`アニメーションGIFを生成しました: ${gifOutputPath}`);
