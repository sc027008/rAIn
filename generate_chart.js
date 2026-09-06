const { createCanvas, registerFont } = require('canvas');
const { Chart } = require('chart.js/auto');
const ChartDataLabels = require('chartjs-plugin-datalabels');
const GIFEncoder = require('gifencoder');
const fs = require('fs');
const path = require('path');

// --- 1. ローカルフォントの登録 ---
const fontLineBoldPath = path.join(__dirname, 'fonts', 'LINESeedJP-Bold.ttf');
const fontNotoRegularPath = path.join(__dirname, 'fonts', 'NotoSans-Regular.ttf');
const fontNotoBoldPath = path.join(__dirname, 'fonts', 'NotoSans-Bold.ttf');

if (fs.existsSync(fontLineBoldPath)) {
  registerFont(fontLineBoldPath, { family: 'LINE Seed JP', weight: 'bold' });
}
if (fs.existsSync(fontNotoRegularPath)) {
  registerFont(fontNotoRegularPath, { family: 'Noto Sans', weight: 'normal' });
}
if (fs.existsSync(fontNotoBoldPath)) {
  registerFont(fontNotoBoldPath, { family: 'Noto Sans', weight: 'bold' });
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
    const { ctx, chartArea } = chart;
    ctx.save();
    
    // フォントと色の設定
    ctx.font = 'bold 14px "LINE Seed JP"';
    ctx.fillStyle = '#999999';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'bottom';
    
    // 描画位置：グラフエリアの右下から少し離した位置
    const x = chartArea.right;
    const y = height - 4; // X軸ラベルの下あたり
    
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
    // 数字「10」「25」（19px Noto Sans）の実際の飛び出し幅
    const labelOffset = 5;

    // 1. 左タイトル：数字「10」の左端ラインに合わせる
    ctx.fillStyle = '#555555';
    ctx.textAlign = 'left';
    ctx.fillText('▼\u2009棒: 時間雨量 mm/h', chartArea.left - labelOffset, y);

    // 2. 右タイトル：数字「25」の右端ラインに合わせる
    ctx.fillStyle = '#7B1FA2';
    ctx.textAlign = 'right';
    ctx.fillText('折れ線: 積算雨量 mm\u2009▼', chartArea.right + labelOffset, y);

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
const totalFrames = 40; // フレーム数
const dataLength = hourlyRain.length;

for (let i = 1; i <= totalFrames; i++) {
  // 最後のフレーム（完成形）は 15,000ms（15秒）表示
  if (i === totalFrames) {
    encoder.setDelay(15000);
  } else {
    encoder.setDelay(50); // 1フレームの秒数 [ms]
  }

  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, width, height);

const globalProgress = i / totalFrames;

  const currentHourly = [];
  const currentCumulative = [];
  const localProgresses = []; // ★【追加位置 1】配列の宣言を追加

  for (let j = 0; j < dataLength; j++) {
    const startThreshold = (j / dataLength) * 0.7;
    if (globalProgress <= startThreshold) {
      currentHourly.push(0);
      currentCumulative.push(null);
      localProgresses.push(0);
    } else {
      const localProgress = Math.min(1.0, (globalProgress - startThreshold) / 0.3);
      const easedProgress = easeOutCubic(localProgress);
      
      // 棒グラフ：下から伸びるアニメーション（イージング適用）
      currentHourly.push(hourlyRain[j] * easedProgress);
      
      // 折れ線グラフ：棒がある程度伸びたタイミングで実値を表示
      if (easedProgress >= 0.75) {
        currentCumulative.push(cumulativeRain[j]); // 実値を表示
      } else {
        currentCumulative.push(null); // それまでは非表示
      }
      
      localProgresses.push(localProgress);
    }
  }

  // ★【追加位置 4】datalabelDisplay の判定もこちらへ差し替え
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
          data: hourlyRain, // ★ 確定値の配列をそのまま渡す（これで途中の数値変動が消えます）
          borderColor: 'transparent',
          backgroundColor: 'transparent',
          pointRadius: 0,
          yAxisID: 'y1',
          order: 0,
          datalabels: {
            display: datalabelDisplay, // ★ バーが82%伸びたタイミングで確定値を表示
            anchor: 'end',
            align: 'end',
            offset: -2,
            color: '#111111',
            font: { size: 20, family: 'Noto Sans', weight: 'bold' },
            textStrokeColor: '#ffffff',
            textStrokeWidth: 4,
            formatter: (value) => value // 端数処理不要でそのまま表示
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
          spanGaps: false, // null 地点への描画をカットして左から伸びる表現にする
          yAxisID: 'y2',
          order: 1,
          datalabels: { display: false }
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
            text: '時間後',
            color: '#555555',
            font: { size: 21, family: 'LINE Seed JP', weight: 'bold' }
          },
          ticks: {
            color: '#111111',
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
            font: { size: 19, family: 'Noto Sans', weight: 'normal' }
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
            font: { size: 19, family: 'Noto Sans', weight: 'normal' }
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
