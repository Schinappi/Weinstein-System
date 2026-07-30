const { useEffect, useMemo, useRef, useState } = React;
const html = htm.bind(React.createElement);
const {
  App: AntApp,
  Alert,
  Button,
  Card,
  Col,
  ConfigProvider,
  DatePicker,
  Empty,
  Flex,
  Form,
  Input,
  Layout,
  List,
  Menu,
  Modal,
  Row,
  Segmented,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} = antd;

const { Header, Sider, Content } = Layout;
const { TextArea } = Input;
const { Title, Paragraph, Text } = Typography;

const TODAY = dayjs().format("YYYY-MM-DD");
const APP_VERSION = "v2026.07.26.1";
const PAGE_OVERVIEW = "overview";
const PAGE_BACKTEST = "backtest";
const PAGE_MONITOR = "monitor";
const PAGE_DEMAND_SUPPORT = "demand-support";
const PAGE_DEMAND_BACKTEST = "demand-backtest";
const PAGE_BOX_BACKTEST = "box-backtest";
const SCAN_RULE_LABEL = "通过生命周期/减速/成熟度门槛后，按结构分排序显示前100个";
const BOX_SCAN_RULE_LABEL = "按日线Demand支撑质量 + 历史反弹 + 当前距离排序显示前100个";

function formatNumber(value, digits = 1, fallback = "--") {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : fallback;
}

function formatInt(value, fallback = "--") {
  return Number.isFinite(Number(value)) ? String(Math.round(Number(value))) : fallback;
}

function formatPercent(value, digits = 1, fallback = "--") {
  return Number.isFinite(Number(value)) ? `${Number(value).toFixed(digits)}%` : fallback;
}

function formatElapsedSeconds(value, fallback = "--") {
  return Number.isFinite(Number(value)) ? `${Math.max(0, Math.round(Number(value)))}s` : fallback;
}

function formatBoolean(value) {
  return value ? "是" : "否";
}

function renderStockCell(row) {
  const isNewHit = Boolean(row?.is_new_hit);
  return html`
    <div className=${`symbol-cell${isNewHit ? " symbol-cell-new-hit" : ""}`}>
      <div className="symbol-headline">
        <div className="symbol-code">${row.symbol}</div>
        ${isNewHit ? html`<span className="new-hit-pill">新增</span>` : null}
      </div>
      <div className="symbol-name">${row.name || "--"}</div>
    </div>
  `;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function renderMarkdown(markdown) {
  const source = String(markdown || "").replace(/\r\n/g, "\n");
  const lines = source.split("\n");
  const blocks = [];
  let paragraph = [];
  let listItems = [];
  let listType = "";

  const formatInlineMarkdown = (text) =>
    escapeHtml(text)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>");

  const flushParagraph = () => {
    if (!paragraph.length) return;
    blocks.push(`<p>${formatInlineMarkdown(paragraph.join("\n")).replace(/\n/g, "<br>")}</p>`);
    paragraph = [];
  };

  const flushList = () => {
    if (!listItems.length || !listType) return;
    const items = listItems.map((item) => `<li>${formatInlineMarkdown(item)}</li>`).join("");
    blocks.push(`<${listType}>${items}</${listType}>`);
    listItems = [];
    listType = "";
  };

  lines.forEach((rawLine) => {
    const line = rawLine.trim();
    if (!line) {
      flushParagraph();
      flushList();
      return;
    }

    const headingMatch = line.match(/^(#{1,3})\s+(.*)$/);
    if (headingMatch) {
      flushParagraph();
      flushList();
      const level = headingMatch[1].length;
      blocks.push(`<h${level}>${formatInlineMarkdown(headingMatch[2])}</h${level}>`);
      return;
    }

    const unorderedMatch = line.match(/^[-*]\s+(.*)$/);
    if (unorderedMatch) {
      flushParagraph();
      if (listType && listType !== "ul") flushList();
      listType = "ul";
      listItems.push(unorderedMatch[1]);
      return;
    }

    const orderedMatch = line.match(/^\d+\.\s+(.*)$/);
    if (orderedMatch) {
      flushParagraph();
      if (listType && listType !== "ol") flushList();
      listType = "ol";
      listItems.push(orderedMatch[1]);
      return;
    }

    const blockquoteMatch = line.match(/^>\s?(.*)$/);
    if (blockquoteMatch) {
      flushParagraph();
      flushList();
      blocks.push(`<blockquote>${formatInlineMarkdown(blockquoteMatch[1])}</blockquote>`);
      return;
    }

    flushList();
    paragraph.push(line);
  });

  flushParagraph();
  flushList();
  return blocks.join("") || "<p>--</p>";
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.error) {
    throw new Error(payload.error || `请求失败: ${response.status}`);
  }
  return payload;
}

function usePollingBacktestJob() {
  const timeoutRef = useRef(null);

  useEffect(() => () => {
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current);
    }
  }, []);

  const stop = () => {
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
  };

  const poll = ({ jobId, endpoint = "/api/backtest", onProgress, onDone, onError, intervalMs = 5000, maxAttempts = 360 }) => {
    let attempts = 0;
    stop();

    const tick = async () => {
      try {
        const payload = await fetchJson(`${endpoint}?job_id=${encodeURIComponent(jobId)}`, { method: "POST" });
        if (payload.status === "running" || payload.status === "started") {
          attempts += 1;
          onProgress?.(attempts, payload);
          if (attempts >= maxAttempts) {
            onError?.(new Error("扫描超时，请稍后刷新"));
            stop();
            return;
          }
          timeoutRef.current = setTimeout(tick, intervalMs);
          return;
        }
        stop();
        onDone?.(payload);
      } catch (error) {
        stop();
        onError?.(error);
      }
    };

    timeoutRef.current = setTimeout(tick, 2500);
  };

  return { poll, stop };
}

function drawChart(canvas, chart, tooltipEl, stateRef) {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, width, height);

  const candles = Array.isArray(chart?.candles) ? chart.candles : [];
  const isDaily = chart?.chart_type === "daily";
  if (!candles.length) {
    ctx.fillStyle = "#64748b";
    ctx.font = "18px Noto Sans SC";
    ctx.fillText("暂无K线数据", width / 2 - 50, height / 2);
    stateRef.current = null;
    if (tooltipEl) tooltipEl.classList.add("hidden");
    return;
  }

  const priceAreaTop = 36;
  const priceAreaHeight = 348;
  const volumeAreaTop = 424;
  const volumeAreaHeight = 92;
  const left = 72;
  const right = width - 92;
  const bottom = priceAreaTop + priceAreaHeight;
  const volumeBottom = volumeAreaTop + volumeAreaHeight;
  const candleWidth = Math.max(3, ((right - left) / Math.max(candles.length, 1)) * 0.6);

  const priceSeries = candles
    .flatMap((item) => isDaily
      ? [item.high, item.low, item.ema144, item.ema169]
      : [item.high, item.low, item.ema30w])
    .filter((value) => Number.isFinite(value));
  [chart.breakout_line, chart.resistance_line, chart.base_breakout_line, chart.base_stop_line].forEach((value) => {
    if (Number.isFinite(value)) priceSeries.push(value);
  });
  (chart.box_upper || []).forEach((value) => {
    if (Number.isFinite(value)) priceSeries.push(value);
  });
  (chart.box_lower || []).forEach((value) => {
    if (Number.isFinite(value)) priceSeries.push(value);
  });

  const maxHigh = Math.max(...priceSeries);
  const minLow = Math.min(...priceSeries);
  const paddedRange = Math.max((maxHigh - minLow) * 0.08, maxHigh * 0.015, 0.5);
  const axisHigh = maxHigh + paddedRange;
  const axisLow = Math.max(minLow - paddedRange, 0);
  const maxVolume = Math.max(...candles.map((item) => item.volume || 0), 1);
  const scaleY = (value) => bottom - ((value - axisLow) / Math.max(axisHigh - axisLow, 0.0001)) * priceAreaHeight;
  const scaleX = (index) => left + ((right - left) / Math.max(candles.length - 1, 1)) * index;

  const drawGrid = (top, bottomLine, rows, color) => {
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    for (let i = 0; i <= rows; i += 1) {
      const y = top + ((bottomLine - top) / rows) * i;
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(right, y);
      ctx.stroke();
    }
  };

  const drawLine = (key, color) => {
    const points = candles
      .map((item, index) => ({ x: scaleX(index), y: item[key] }))
      .filter((item) => Number.isFinite(item.y));
    if (points.length < 2) return;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    points.forEach((point, index) => {
      const y = scaleY(point.y);
      if (index === 0) ctx.moveTo(point.x, y);
      else ctx.lineTo(point.x, y);
    });
    ctx.stroke();
  };

  const drawHorizontalLine = (value, color, dash, label) => {
    if (!Number.isFinite(value)) return;
    const y = scaleY(value);
    ctx.save();
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 1.2;
    ctx.setLineDash(dash);
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(right, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.font = "12px Noto Sans SC";
    ctx.fillText(`${label} ${value.toFixed(2)}`, right + 10, y + 4);
    ctx.restore();
  };

  const drawBoxBands = () => {
    const boxUpper = chart.box_upper;
    const boxLower = chart.box_lower;
    if (!boxUpper || !boxLower || !boxUpper.length) return;
    let firstValid = null;
    let lastValid = null;
    for (let i = 0; i < boxUpper.length; i += 1) {
      if (Number.isFinite(boxUpper[i]) && Number.isFinite(boxLower[i])) {
        if (firstValid === null) firstValid = i;
        lastValid = i;
      }
    }
    if (firstValid === null) return;

    ctx.save();
    ctx.strokeStyle = "#0891b2";
    ctx.lineWidth = 2;
    ctx.beginPath();
    let started = false;
    for (let i = firstValid; i <= lastValid; i += 1) {
      if (Number.isFinite(boxUpper[i])) {
        const x = scaleX(i);
        const y = scaleY(boxUpper[i]);
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      }
    }
    ctx.stroke();

    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    started = false;
    for (let i = firstValid; i <= lastValid; i += 1) {
      if (Number.isFinite(boxLower[i])) {
        const x = scaleX(i);
        const y = scaleY(boxLower[i]);
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      }
    }
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = "rgba(8, 145, 178, 0.08)";
    ctx.beginPath();
    started = false;
    for (let i = firstValid; i <= lastValid; i += 1) {
      if (Number.isFinite(boxUpper[i])) {
        const x = scaleX(i);
        if (!started) {
          ctx.moveTo(x, scaleY(boxUpper[i]));
          started = true;
        } else {
          ctx.lineTo(x, scaleY(boxUpper[i]));
        }
      }
    }
    for (let i = lastValid; i >= firstValid; i -= 1) {
      if (Number.isFinite(boxLower[i])) {
        ctx.lineTo(scaleX(i), scaleY(boxLower[i]));
      }
    }
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  };

  drawGrid(priceAreaTop, bottom, 5, "#e2e8f0");
  drawGrid(volumeAreaTop, volumeBottom, 2, "#edf2f7");
  drawBoxBands();
  drawHorizontalLine(chart.base_breakout_line, "#059669", [8, 4], "基底突破");
  drawHorizontalLine(chart.breakout_line, "#7c3aed", [6, 5], "动态压力");
  drawHorizontalLine(chart.resistance_line, "#ea580c", [10, 6], "压力线");
  drawHorizontalLine(chart.base_stop_line, "#dc2626", [4, 3], "止损线");
  if (isDaily) {
    drawLine("ema144", "#2563eb");
    drawLine("ema169", "#d97706");
  } else {
    drawLine("ema30w", "#2563eb");
  }

  candles.forEach((item, index) => {
    const x = scaleX(index);
    const openY = scaleY(item.open);
    const closeY = scaleY(item.close);
    const highY = scaleY(item.high);
    const lowY = scaleY(item.low);
    const color = item.close >= item.open ? "#16a34a" : "#dc2626";
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, highY);
    ctx.lineTo(x, lowY);
    ctx.stroke();
    const bodyTop = Math.min(openY, closeY);
    const bodyHeight = Math.max(Math.abs(closeY - openY), 1);
    ctx.fillStyle = color;
    ctx.fillRect(x - candleWidth / 2, bodyTop, candleWidth, bodyHeight);
    const volumeHeight = ((item.volume || 0) / maxVolume) * volumeAreaHeight;
    ctx.globalAlpha = 0.7;
    ctx.fillRect(x - candleWidth / 2, volumeBottom - volumeHeight, candleWidth, volumeHeight);
    ctx.globalAlpha = 1;
  });

  ctx.fillStyle = "#64748b";
  ctx.font = "12px Noto Sans SC";
  for (let i = 0; i <= 6; i += 1) {
    const value = axisLow + ((axisHigh - axisLow) / 6) * i;
    const y = scaleY(value);
    ctx.fillText(value.toFixed(2), 16, y + 4);
  }
  for (let i = 0; i < 5; i += 1) {
    const index = Math.floor((candles.length - 1) * (i / 4));
    ctx.fillText(candles[index]?.date || "", scaleX(index) - 24, bottom + 24);
  }
  ctx.fillText("价格", 18, priceAreaTop - 10);
  ctx.fillText("成交量", 18, bottom + 56);
  ctx.fillStyle = "#334155";
  ctx.font = "12px Space Grotesk";
  if (isDaily) {
    ctx.fillText("EMA144", 74, 20);
    ctx.fillText("EMA169", 164, 20);
  } else {
    ctx.fillText("EMA30W", 74, 20);
  }
  ctx.fillStyle = "#2563eb";
  ctx.fillRect(40, 10, 20, 3);
  if (isDaily) {
    ctx.fillStyle = "#d97706";
    ctx.fillRect(130, 10, 20, 3);
  }

  stateRef.current = { candles, chart, scaleX, scaleY, left, right, priceAreaTop, bottom };

  canvas.onmousemove = (event) => {
    if (!stateRef.current || !tooltipEl) return;
    const rect = canvas.getBoundingClientRect();
    const x = (event.clientX - rect.left) * (canvas.width / rect.width);
    if (x < left || x > right) {
      tooltipEl.classList.add("hidden");
      drawChart(canvas, chart, tooltipEl, stateRef);
      return;
    }
    let closestIndex = 0;
    let smallestDistance = Number.POSITIVE_INFINITY;
    candles.forEach((item, index) => {
      const distance = Math.abs(scaleX(index) - x);
      if (distance < smallestDistance) {
        smallestDistance = distance;
        closestIndex = index;
      }
    });
    drawChart(canvas, chart, tooltipEl, stateRef);
    const candle = candles[closestIndex];
    const highlightX = scaleX(closestIndex);
    const highlightY = scaleY(candle.close);
    ctx.save();
    ctx.strokeStyle = "rgba(37, 99, 235, 0.55)";
    ctx.lineWidth = 1;
    ctx.setLineDash([5, 4]);
    ctx.beginPath();
    ctx.moveTo(highlightX, priceAreaTop);
    ctx.lineTo(highlightX, bottom);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#2563eb";
    ctx.beginPath();
    ctx.arc(highlightX, highlightY, 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();

    const change = Number.isFinite(candle.open) && Number.isFinite(candle.close)
      ? (((candle.close - candle.open) / Math.max(candle.open, 0.0001)) * 100).toFixed(2)
      : "--";
    tooltipEl.innerHTML = [
      `<div><strong>日期</strong>${escapeHtml(candle.date || "--")}</div>`,
      `<div><strong>开盘</strong>${formatNumber(candle.open, 2)}</div>`,
      `<div><strong>最高</strong>${formatNumber(candle.high, 2)}</div>`,
      `<div><strong>最低</strong>${formatNumber(candle.low, 2)}</div>`,
      `<div><strong>收盘</strong>${formatNumber(candle.close, 2)}</div>`,
      `<div><strong>涨跌</strong>${change === "--" ? "--" : `${change}%`}</div>`,
      `<div><strong>成交量</strong>${formatVolume(candle.volume)}</div>`,
      ...(isDaily
        ? [
            `<div><strong>EMA144</strong>${formatNumber(candle.ema144, 2)}</div>`,
            `<div><strong>EMA169</strong>${formatNumber(candle.ema169, 2)}</div>`,
          ]
        : [
            `<div><strong>EMA30W</strong>${formatNumber(candle.ema30w, 2)}</div>`,
          ]),
    ].join("");
    tooltipEl.classList.remove("hidden");
    const leftPx = Math.min(event.clientX - rect.left + 18, rect.width - 220);
    const topPx = Math.min(event.clientY - rect.top + 18, rect.height - 210);
    tooltipEl.style.left = `${Math.max(12, leftPx)}px`;
    tooltipEl.style.top = `${Math.max(12, topPx)}px`;
  };

  canvas.onmouseleave = () => {
    if (tooltipEl) tooltipEl.classList.add("hidden");
    drawChart(canvas, chart, tooltipEl, stateRef);
  };
}

function formatVolume(value) {
  if (!Number.isFinite(value)) return "--";
  if (value >= 100000000) return `${(value / 100000000).toFixed(2)}亿`;
  if (value >= 10000) return `${(value / 10000).toFixed(2)}万`;
  return value.toFixed(0);
}

function scoreClassName(value) {
  if (!Number.isFinite(Number(value))) return "fundamental-score neu";
  if (Number(value) > 0) return "fundamental-score pos";
  if (Number(value) < 0) return "fundamental-score neg";
  return "fundamental-score neu";
}

function buildFundamentalCards(fundamental) {
  if (!fundamental) return [];
  const hasData = [
    fundamental.holder_score,
    fundamental.nb_score,
    fundamental.moneyflow_confirm,
  ].some((value) => value !== null && value !== undefined && !Number.isNaN(value));
  if (!hasData) return [];

  const holderDetail = Number.isFinite(fundamental.holder_change_pct)
    ? `环比 ${formatNumber(fundamental.holder_change_pct, 2)}%`
    : "暂无数据";
  const nbDetail = Number.isFinite(fundamental.nb_ratio)
    ? `持仓占比 ${formatNumber(fundamental.nb_ratio, 2)}%`
    : "暂无数据";
  let mfDetail = "暂无数据";
  if (Number.isFinite(fundamental.net_mf_amount)) {
    const absAmt = Math.abs(fundamental.net_mf_amount);
    if (absAmt >= 100000000) mfDetail = `净流向 ${(absAmt / 100000000).toFixed(2)} 亿`;
    else if (absAmt >= 10000) mfDetail = `净流向 ${(absAmt / 10000).toFixed(2)} 万`;
    else mfDetail = `净流向 ${absAmt.toFixed(0)}`;
  }

  return [
    { label: "股东人数", score: fundamental.holder_score, detail: holderDetail },
    { label: "北向资金", score: fundamental.nb_score, detail: nbDetail },
    { label: "资金流向", score: fundamental.moneyflow_confirm, detail: mfDetail },
  ];
}

function DetailModal({
  open,
  onClose,
  symbol,
  symbolList,
  onNavigate,
  onAddMonitor,
}) {
  const [loading, setLoading] = useState(false);
  const [analysisLoading, setAnalysisLoading] = useState(false);
  const [chartType, setChartType] = useState("weekly");
  const [detail, setDetail] = useState(null);
  const [analysisHtml, setAnalysisHtml] = useState("<p>点击“开始 AI 分析”获取个股解读。</p>");
  const [showAnalysisButton, setShowAnalysisButton] = useState(false);
  const canvasRef = useRef(null);
  const tooltipRef = useRef(null);
  const chartStateRef = useRef(null);
  const requestIdRef = useRef(0);

  useEffect(() => {
    if (!open || !symbol) return;
    const requestId = ++requestIdRef.current;
    setLoading(true);
    setDetail(null);
    setShowAnalysisButton(false);
    setAnalysisLoading(false);
    setAnalysisHtml("<p>点击“开始 AI 分析”获取个股解读。</p>");

    fetchJson(`/api/stock/${encodeURIComponent(symbol)}?chart=${chartType}`)
      .then((payload) => {
        if (requestId !== requestIdRef.current) return;
        setDetail(payload);
        if (payload.analysis) {
          setAnalysisHtml(renderMarkdown(payload.analysis));
          setShowAnalysisButton(false);
        } else {
          setShowAnalysisButton(true);
        }
      })
      .catch((error) => {
        if (requestId !== requestIdRef.current) return;
        setAnalysisHtml(renderMarkdown(`> ${error.message || "加载失败"}`));
      })
      .finally(() => {
        if (requestId === requestIdRef.current) {
          setLoading(false);
        }
      });
  }, [open, symbol, chartType]);

  useEffect(() => {
    if (!detail?.chart || !canvasRef.current) return;
    drawChart(canvasRef.current, detail.chart, tooltipRef.current, chartStateRef);
  }, [detail]);

  const canGoPrev = symbolList.indexOf(symbol) > 0;
  const canGoNext = symbolList.indexOf(symbol) >= 0 && symbolList.indexOf(symbol) < symbolList.length - 1;

  const startAnalysis = async () => {
    if (!symbol) return;
    setAnalysisLoading(true);
    setShowAnalysisButton(false);
    try {
      const payload = await fetchJson(`/api/stock/${encodeURIComponent(symbol)}/analysis`);
      setAnalysisHtml(renderMarkdown(payload.analysis || "--"));
    } catch (error) {
      setAnalysisHtml(renderMarkdown(`> ${error.message || "分析失败"}`));
      setShowAnalysisButton(true);
    } finally {
      setAnalysisLoading(false);
    }
  };

  const fundamentalCards = buildFundamentalCards(detail?.fundamental);

  return html`
    <${Modal}
      open=${open}
      onCancel=${onClose}
      footer=${null}
      width=${1320}
      className="modal-shell"
      destroyOnClose=${false}
      title=${html`
        <${Flex} vertical gap=${4}>
          <${Text} type="secondary">${detail?.symbol || symbol || ""}<//>
          <${Title} level=${3} style=${{ margin: 0 }}>
            ${detail ? `${detail.name || ""} ${detail.symbol || ""}`.trim() : symbol || "个股详情"}
          <//>
        <//>
      `}
    >
      <${Spin} spinning=${loading}>
        <div className="detail-layout">
          <div>
            <${Flex} justify="space-between" align="center" style=${{ marginBottom: 14, flexWrap: "wrap", gap: 12 }}>
              <${Space}>
                <${Button} onClick=${() => onNavigate?.("prev")} disabled=${!canGoPrev}>上一只<//>
                <${Button} onClick=${() => onNavigate?.("next")} disabled=${!canGoNext}>下一只<//>
              <//>
              <${Space}>
                <${Button}
                  type="primary"
                  ghost=${true}
                  onClick=${() => onAddMonitor?.(detail?.symbol || symbol, detail?.latest_close)}
                  disabled=${loading || !(detail?.symbol || symbol)}
                >
                  添加监控
                <//>
              <//>
              <${Segmented}
                value=${chartType}
                onChange=${setChartType}
                options=${[
                  { label: "周线", value: "weekly" },
                  { label: "日线", value: "daily" },
                ]}
              />
            <//>

            <div className="detail-stage-row">
              <${Tag} color="blue">Stage1: ${detail?.stage1_rank ? `第 ${detail.stage1_rank} 名` : "未上榜"}<//>
              <${Tag} color="cyan">Stage2: ${detail?.stage2_rank ? `第 ${detail.stage2_rank} 名` : "未上榜"}<//>
            </div>

            <div className="detail-chart-wrap">
              <canvas ref=${canvasRef} className="detail-chart-canvas" width="980" height="560"></canvas>
              <div ref=${tooltipRef} className="chart-tooltip hidden"></div>
            </div>

            <${Card} className="analysis-card" style=${{ marginTop: 18 }}>
              <${Flex} justify="space-between" align="center" style=${{ marginBottom: 14, flexWrap: "wrap", gap: 12 }}>
                <${Title} level=${4} style=${{ margin: 0 }}>个股分析<//>
                <${Space}>
                  ${showAnalysisButton ? html`<${Button} type="primary" ghost onClick=${startAnalysis}>开始 AI 分析<//>` : null}
                  ${analysisLoading ? html`<${Spin} size="small" />` : null}
                <//>
              <//>
              <div className="analysis-markdown" dangerouslySetInnerHTML=${{ __html: analysisHtml }}></div>
            <//>
          </div>

          <div>
            <${Card} className="panel-card">
              <${Title} level=${4} style=${{ marginTop: 0 }}>关键指标<//>
              <div className="detail-metric-grid">
                ${(detail?.metrics || []).map(
                  (item) => html`
                    <div className="detail-metric-item" key=${item.label}>
                      <div className="detail-metric-label">${item.label}</div>
                      <div className="detail-metric-value">${item.value || "--"}</div>
                    </div>
                  `
                )}
              </div>
            <//>

            ${fundamentalCards.length
              ? html`
                  <${Card} className="panel-card" style=${{ marginTop: 18 }}>
                    <${Title} level=${4} style=${{ marginTop: 0 }}>基本面补充<//>
                    <div className="fundamental-grid">
                      ${fundamentalCards.map(
                        (item) => html`
                          <div className="fundamental-card" key=${item.label}>
                            <div className="fundamental-head">
                              <div className="fundamental-label">${item.label}</div>
                              <div className=${scoreClassName(item.score)}>${formatInt(item.score)}</div>
                            </div>
                            <div className="fundamental-detail">${item.detail}</div>
                          </div>
                        `
                      )}
                    </div>
                  <//>
                `
              : null}
          </div>
        </div>
      <//>
    <//>
  `;
}

function OverviewPage({
  loading,
  data,
  statusText,
  onRefresh,
  onOpenDetail,
}) {
  const items = (data?.items || []).slice(0, 50);
  const metrics = useMemo(() => {
    const avgQuality = items.length
      ? (items.reduce((sum, item) => sum + Number(item.cont_quality_score || 0), 0) / items.length)
      : 0;
    const avgBox = items.length
      ? (items.reduce((sum, item) => sum + Number(item.cont_score_box || 0), 0) / items.length)
      : 0;
    return [
      { label: "榜单数量", value: formatInt(items.length || 0), extra: "展示今日前 50 名结果候选" },
      { label: "扫描股票", value: formatInt(data?.scanned || 0), extra: "来自缓存周线数据池" },
      { label: "平均质量", value: formatNumber(avgQuality, 1), extra: "续涨综合质量均值" },
      { label: "平均箱体", value: formatNumber(avgBox, 1), extra: "结构纪律分均值" },
    ];
  }, [data, items]);

  const columns = [
    {
      title: "排名",
      key: "rank",
      width: 84,
      render: (_, __, index) => html`<span className="rank-chip">#${index + 1}</span>`,
    },
    {
      title: "股票",
      dataIndex: "symbol",
      key: "symbol",
      width: 160,
      render: (_, row) => renderStockCell(row),
    },
    {
      title: "结构分",
      dataIndex: "cont_score_box",
      key: "cont_score_box",
      width: 110,
      render: (value) => html`<span className=${Number(value) >= 10 ? "positive-text" : ""}>${formatNumber(value, 0)}</span>`,
    },
    {
      title: "质量分",
      dataIndex: "cont_quality_score",
      key: "cont_quality_score",
      width: 110,
      render: (value) => formatNumber(value, 0),
    },
    {
      title: "等级",
      dataIndex: "cont_quality_grade",
      key: "cont_quality_grade",
      width: 90,
      render: (value) => {
        const colorMap = { S: "gold", A: "green", B: "blue", C: "default" };
        return html`<${Tag} color=${colorMap[value] || "default"}>${value || "--"}<//>`;
      },
    },
    {
      title: "箱体振幅",
      dataIndex: "cont_box_range_pct",
      key: "cont_box_range_pct",
      width: 110,
      render: (value) => formatPercent(value, 1),
    },
    {
      title: "箱体周数",
      dataIndex: "cont_box_duration_weeks",
      key: "cont_box_duration_weeks",
      width: 100,
      render: (value) => formatInt(value),
    },
    {
      title: "缩量",
      dataIndex: "cont_volume_trend_ok",
      key: "cont_volume_trend_ok",
      width: 90,
      render: (value) => html`<${Tag} color=${value ? "green" : "default"}>${formatBoolean(value)}<//>`,
    },
    {
      title: "最新日期",
      dataIndex: "latest_date",
      key: "latest_date",
      width: 120,
    },
    {
      title: "信号解读",
      dataIndex: "cont_quality_reason",
      key: "cont_quality_reason",
      render: (value) => html`<span className="reason-text" title=${value || ""}>${value || "--"}</span>`,
    },
  ];

  return html`
    <div className="page-shell">
      <div className="hero-grid">
        <${Card} className="hero-card">
          <div className="hero-panel">
            <div className="hero-kicker">Overview</div>
            <div className="hero-title">今日总览页</div>
            <div className="hero-copy">
              展示与“手动触发、目标日期为今天的全市场回测”一致的排行榜结果。页面默认复用当天已缓存结果，
              手动刷新时会重新触发今日扫描，但不会改变原有回测逻辑。
            </div>
            <div className="hero-actions">
              <${Button} type="primary" size="large" onClick=${onRefresh} loading=${loading}>刷新今日排行榜<//>
              <${Tag} color="geekblue">版本 ${APP_VERSION}<//>
              <${Tag} color="blue">目标日期 ${data?.target_date || TODAY}<//>
              <${Tag} color="cyan">全市场续涨结构扫描<//>
            </div>
          </div>
        <//>

        <${Card} className="hero-card">
          <div className="hero-meta">
            <div className="meta-pill">
              <div className="meta-pill-label">扫描模式</div>
              <div className="meta-pill-value">TOP 50</div>
            </div>
            <div className="meta-pill">
              <div className="meta-pill-label">返回机制</div>
              <div className="meta-pill-value">Async</div>
            </div>
            <div className="meta-pill">
              <div className="meta-pill-label">数据切片</div>
              <div className="meta-pill-value">截至今日</div>
            </div>
            <div className="meta-pill">
              <div className="meta-pill-label">详情联动</div>
              <div className="meta-pill-value">保留</div>
            </div>
          </div>
        <//>
      </div>

      <div className="page-grid">
        <div className="cards-grid">
          ${metrics.map(
            (item) => html`
              <${Card} className="metric-card" key=${item.label}>
                <div className="metric-label">${item.label}</div>
                <div className="metric-value">${item.value}</div>
                <div className="metric-extra">${item.extra}</div>
              <//>
            `
          )}
        </div>

        <${Card} className="status-card">
          <div className="toolbar-row" style=${{ marginBottom: 18 }}>
            <div>
              <h2 className="section-title">运行状态</h2>
              <div className="section-copy">总览页会优先复用今天的扫描结果；手动刷新时才会重新启动同日扫描。</div>
            </div>
            <div className="toolbar-actions">
              <${Tag} color=${loading ? "processing" : "success"}>${loading ? "扫描中" : "已就绪"}<//>
            </div>
          </div>
          <div className="status-message">${statusText}</div>
          <div className="status-grid" style=${{ marginTop: 18 }}>
            <div className="status-block">
              <div className="status-label">目标日期</div>
              <div className="status-value">${data?.target_date || TODAY}</div>
            </div>
            <div className="status-block">
              <div className="status-label">扫描用时</div>
              <div className="status-value">${data?.elapsed ? `${data.elapsed}s` : "--"}</div>
            </div>
            <div className="status-block">
              <div className="status-label">命中候选</div>
              <div className="status-value">${formatInt(data?.candidates_total)}</div>
            </div>
            <div className="status-block">
              <div className="status-label">可用结果</div>
              <div className="status-value">${formatInt(items.length)}</div>
            </div>
          </div>
        <//>

        <${Card} className="panel-card table-card">
          <div className="toolbar-row" style=${{ marginBottom: 18 }}>
            <div>
              <h2 className="section-title">今日排行榜</h2>
              <div className="toolbar-copy">点击任意一行即可打开个股详情，K 线、AI 分析和指标交互保持不变。</div>
            </div>
          </div>
          <${Table}
            className="overview-table"
            columns=${columns}
            dataSource=${items}
            rowKey=${(row) => row.symbol}
            rowClassName=${(record) => record.is_new_hit ? "ranking-row-new-hit" : ""}
            pagination=${false}
            loading=${loading}
            scroll=${{ x: 1280 }}
            locale=${{
              emptyText: html`<div className="empty-block">暂无今日排行榜数据</div>`,
            }}
            onRow=${(record) => ({
              onClick: () => onOpenDetail(record.symbol, items),
              style: { cursor: "pointer" },
            })}
          />
        <//>
      </div>
    </div>
  `;
}

function DemandSupportPage({
  loading,
  data,
  statusText,
  onRefresh,
  onOpenDetail,
}) {
  const items = data?.items || [];
  const metrics = useMemo(() => {
    const avgScore = items.length
      ? items.reduce((sum, item) => sum + Number(item.demand_support_score || 0), 0) / items.length
      : 0;
    const avgTouches = items.length
      ? items.reduce((sum, item) => sum + Number(item.demand_support_touch_count || 0), 0) / items.length
      : 0;
    const avgSwing = items.length
      ? items.reduce((sum, item) => sum + Number(item.demand_support_avg_swing_pct || item.demand_support_avg_rebound_pct || 0), 0) / items.length
      : 0;
    const maxScore = items.length
      ? Math.max(...items.map((item) => Number(item.demand_support_score || 0)))
      : 0;
    return [
      { label: "候选数量", value: formatInt(data?.count || items.length || 0), extra: "Demand回踩候选 Top 50" },
      { label: "最高分", value: formatNumber(maxScore, 1), extra: "支撑质量 + 历史反弹 + 当前距离" },
      { label: "平均Swing", value: formatPercent(avgSwing, 1), extra: "辅助观察，不再主导总分" },
      { label: "平均触底", value: formatNumber(avgTouches, 1), extra: "真实回踩Demand的分离次数" },
    ];
  }, [data, items]);

  const columns = [
    {
      title: "排名",
      key: "rank",
      width: 84,
      render: (_, __, index) => html`<span className="rank-chip">#${index + 1}</span>`,
    },
    {
      title: "股票",
      dataIndex: "symbol",
      key: "symbol",
      width: 170,
      render: (_, row) => renderStockCell(row),
    },
    {
      title: "支撑分",
      dataIndex: "demand_support_score",
      key: "demand_support_score",
      width: 110,
      render: (value) => html`<span className=${Number(value) >= 85 ? "positive-text" : ""}>${formatNumber(value, 1)}</span>`,
    },
    {
      title: "等级",
      dataIndex: "demand_support_grade",
      key: "demand_support_grade",
      width: 90,
      render: (value) => {
        const colorMap = { S: "gold", A: "green", B: "blue", C: "default" };
        return html`<${Tag} color=${colorMap[value] || "default"}>${value || "--"}<//>`;
      },
    },
    {
      title: "支撑质量",
      dataIndex: "demand_support_score_support_quality",
      key: "demand_support_score_support_quality",
      width: 110,
      render: (value) => formatNumber(value, 1),
    },
    {
      title: "历史反弹",
      dataIndex: "demand_support_score_historical_rebound",
      key: "demand_support_score_historical_rebound",
      width: 110,
      render: (value) => formatNumber(value, 1),
    },
    {
      title: "当前距离",
      dataIndex: "demand_support_score_current_distance",
      key: "demand_support_score_current_distance",
      width: 110,
      render: (value) => formatNumber(value, 1),
    },
    {
      title: "距Demand",
      dataIndex: "demand_support_approach_gap_pct",
      key: "demand_support_approach_gap_pct",
      width: 110,
      render: (value) => formatPercent(value, 1),
    },
    {
      title: "20D动能",
      dataIndex: "demand_support_approach_energy_pct",
      key: "demand_support_approach_energy_pct",
      width: 110,
      render: (value) => formatPercent(value, 1),
    },
    {
      title: "回踩缩量",
      dataIndex: "demand_support_pullback_volume_ratio",
      key: "demand_support_pullback_volume_ratio",
      width: 110,
      render: (value) => formatNumber(value, 2),
    },
    {
      title: "支撑区",
      key: "zone",
      width: 150,
      render: (_, row) => `${formatNumber(row.demand_support_lower, 2)} - ${formatNumber(row.demand_support_upper, 2)}`,
    },
    {
      title: "触底",
      dataIndex: "demand_support_touch_count",
      key: "demand_support_touch_count",
      width: 90,
      render: (value) => formatInt(value),
    },
    {
      title: "完整Cycle",
      dataIndex: "demand_support_success_rate",
      key: "demand_support_success_rate",
      width: 110,
      render: (_, row) => formatInt(row.demand_support_swing_count ?? row.demand_support_success_count),
    },
    {
      title: "平均Swing",
      dataIndex: "demand_support_avg_rebound_pct",
      key: "demand_support_avg_rebound_pct",
      width: 110,
      render: (_, row) => formatPercent(row.demand_support_avg_swing_pct ?? row.demand_support_avg_rebound_pct, 1),
    },
    {
      title: "箱顶",
      dataIndex: "demand_support_top_price",
      key: "demand_support_top_price",
      width: 100,
      render: (value) => formatNumber(value, 2),
    },
    {
      title: "反弹效率",
      dataIndex: "demand_support_rebound_efficiency",
      key: "demand_support_rebound_efficiency",
      width: 110,
      render: (value) => Number.isFinite(Number(value)) ? formatPercent(Number(value) * 100, 0) : "--",
    },
    {
      title: "箱体利用",
      dataIndex: "demand_support_box_utilization_pct",
      key: "demand_support_box_utilization_pct",
      width: 110,
      render: (value) => formatPercent(value, 0),
    },
    {
      title: "平均穿透",
      dataIndex: "demand_support_avg_penetration_pct",
      key: "demand_support_avg_penetration_pct",
      width: 110,
      render: (value) => formatPercent(value, 2),
    },
    {
      title: "箱体高度",
      dataIndex: "demand_support_box_height_pct",
      key: "demand_support_box_height_pct",
      width: 110,
      render: (value) => formatPercent(value, 1),
    },
    {
      title: "顶部稳定",
      dataIndex: "demand_support_top_stability_pct",
      key: "demand_support_top_stability_pct",
      width: 110,
      render: (value) => formatPercent(value, 1),
    },
    {
      title: "持续",
      dataIndex: "demand_support_duration_weeks",
      key: "demand_support_duration_weeks",
      width: 100,
      render: (_, row) => {
        const weeks = formatInt(row.demand_support_duration_weeks);
        const bars = Number(row.demand_support_duration_bars || 0);
        const unit = row.demand_support_duration_unit === "daily" ? "日线" : "周线";
        return bars ? `${weeks}周/${bars}${unit}bar` : `${weeks}周`;
      },
    },
    {
      title: "最近触底",
      dataIndex: "demand_support_latest_touch_date",
      key: "demand_support_latest_touch_date",
      width: 120,
    },
    {
      title: "信号解读",
      dataIndex: "demand_support_reason",
      key: "demand_support_reason",
      render: (value) => html`<span className="reason-text" title=${value || ""}>${value || "--"}</span>`,
    },
  ];

  return html`
    <div className="page-shell">
      <div className="hero-grid">
        <${Card} className="hero-card">
          <div className="hero-panel">
            <div className="hero-kicker">Demand Support</div>
            <div className="hero-title">Demand支撑回踩榜</div>
            <div className="hero-copy">
              这页先找市场反复认可的 Demand 支撑区，再看历史回弹能力和当前是否主动回踩到位，
              重点筛选“离已验证支撑很近、容易出现承接反弹”的观察机会。
            </div>
            <div className="hero-actions">
              <${Button} type="primary" size="large" onClick=${onRefresh} loading=${loading}>刷新榜单<//>
              <${Tag} color="green">Demand支撑<//>
              <${Tag} color="cyan">当前回踩<//>
            </div>
          </div>
        <//>

        <${Card} className="hero-card">
          <div className="hero-meta">
            <div className="meta-pill">
              <div className="meta-pill-label">窗口</div>
              <div className="meta-pill-value">52W</div>
            </div>
            <div className="meta-pill">
              <div className="meta-pill-label">支撑容差</div>
              <div className="meta-pill-value">±1.5%</div>
            </div>
            <div className="meta-pill">
              <div className="meta-pill-label">反弹验证</div>
              <div className="meta-pill-value">Touch间</div>
            </div>
            <div className="meta-pill">
              <div className="meta-pill-label">候选阈值</div>
              <div className="meta-pill-value">70+</div>
            </div>
          </div>
        <//>
      </div>

      <div className="page-grid">
        <div className="cards-grid">
          ${metrics.map(
            (item) => html`
              <${Card} className="metric-card" key=${item.label}>
                <div className="metric-label">${item.label}</div>
                <div className="metric-value">${item.value}</div>
                <div className="metric-extra">${item.extra}</div>
              <//>
            `
          )}
        </div>

        <${Card} className="panel-card table-card">
          <div className="toolbar-row" style=${{ marginBottom: 18 }}>
            <div>
              <h2 className="section-title">Demand支撑回踩排行榜</h2>
              <div className="toolbar-copy">${statusText || "展示箱底支撑与完整 Swing 候选。"}</div>
            </div>
            <div className="toolbar-actions">
              <${Tag} color=${loading ? "processing" : "success"}>${loading ? "加载中" : "已就绪"}<//>
              <${Tag} color="default">点击行查看个股详情<//>
            </div>
          </div>
          <${Table}
            className="backtest-table"
            columns=${columns}
            dataSource=${items}
            rowKey=${(row) => row.symbol}
            rowClassName=${(record) => record.is_new_hit ? "ranking-row-new-hit" : ""}
            pagination=${{ pageSize: 20, showSizeChanger: false, hideOnSinglePage: true }}
            loading=${loading}
            scroll=${{ x: 2200 }}
            locale=${{
              emptyText: html`<div className="empty-block">${loading ? "正在加载" : "暂无Demand回踩候选"}</div>`,
            }}
            onRow=${(record) => ({
              onClick: () => onOpenDetail(record.symbol, items),
              style: { cursor: "pointer" },
            })}
          />
        <//>
      </div>
    </div>
  `;
}

function SearchPanel({ onOpenDetail }) {
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState([]);

  const runSearch = async (value = query) => {
    setLoading(true);
    try {
      const payload = await fetchJson(`/api/search?q=${encodeURIComponent(value || "")}`);
      setItems(payload.items || []);
    } catch (error) {
      message.error(error.message || "查询失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    runSearch("");
  }, []);

  return html`
    <${Card} className="panel-card">
      <div className="toolbar-row" style=${{ marginBottom: 16 }}>
        <div>
          <h2 className="section-title">个股搜索</h2>
          <div className="toolbar-copy">保留原有搜索能力，支持从总览页直接跳转到个股详情。</div>
        </div>
        <div className="toolbar-actions" style=${{ minWidth: "min(100%, 480px)" }}>
          <${Input}
            value=${query}
            onChange=${(event) => setQuery(event.target.value)}
            onPressEnter=${() => runSearch()}
            placeholder="输入股票代码或名称"
            size="large"
          />
          <${Button} type="primary" size="large" onClick=${() => runSearch()} loading=${loading}>查询<//>
        </div>
      </div>
      <div className="search-result-grid">
        ${(items || []).map(
          (item) => html`
            <div className="search-result-card" key=${item.symbol} onClick=${() => onOpenDetail(item.symbol, items)}>
              <div className="symbol-code">${item.symbol}</div>
              <div className="symbol-name" style=${{ marginTop: 6 }}>${item.name || "--"}</div>
              <div className="muted-text" style=${{ marginTop: 10, fontSize: "13px" }}>
                Stage1: ${formatBoolean(item.stage1)} / Stage2: ${formatBoolean(item.stage2)}
              </div>
            </div>
          `
        )}
      </div>
      ${!loading && !(items || []).length ? html`<${Empty} description="暂无匹配结果" style=${{ marginTop: 18 }} />` : null}
    <//>
  `;
}

function MonitorPage({ loading, statusText, data, onRefresh, onDelete, onOpenDetail }) {
  const items = data?.items || [];

  const columns = [
    {
      title: "股票",
      dataIndex: "symbol",
      key: "symbol",
      width: 180,
      render: (_, row) => html`
        <div className="symbol-cell">
          <div className="symbol-code">${row.symbol}</div>
          <div className="symbol-name">${row.name || "--"}</div>
        </div>
      `,
    },
    {
      title: "现价",
      dataIndex: "latest_close",
      key: "latest_close",
      width: 110,
    },
    {
      title: "目标价",
      dataIndex: "target_price",
      key: "target_price",
      width: 110,
    },
    {
      title: "价差",
      dataIndex: "distance_amount",
      key: "distance_amount",
      width: 120,
      render: (value) => html`<span className=${String(value).startsWith("-") ? "negative-text" : "positive-text"}>${value || "--"}</span>`,
    },
    {
      title: "差距%",
      dataIndex: "distance_pct",
      key: "distance_pct",
      width: 120,
      render: (value) => html`<span className=${String(value).startsWith("-") ? "negative-text" : "positive-text"}>${value || "--"}</span>`,
    },
    {
      title: "最新日期",
      dataIndex: "latest_trade_date",
      key: "latest_trade_date",
      width: 130,
    },
    {
      title: "操作",
      key: "actions",
      width: 110,
      render: (_, row) => html`
        <${Button}
          danger=${true}
          size="small"
          onClick=${(event) => {
            event.stopPropagation();
            onDelete?.(row);
          }}
        >
          删除
        <//>
      `,
    },
  ];

  return html`
    <div className="page-shell">
      <${Card} className="hero-card">
        <div className="toolbar-row">
          <div>
            <div className="hero-kicker">Monitor</div>
            <div className="hero-title" style=${{ fontSize: "34px", marginTop: 16 }}>价格监控</div>
            <div className="hero-copy">
              在个股详情弹窗中填写目标价格后，个股会出现在这里。监控页会展示现价、目标价以及当前差距。
            </div>
          </div>
          <div className="toolbar-actions">
            <${Button} type="primary" size="large" onClick=${onRefresh} loading=${loading}>刷新监控<//>
          </div>
        </div>
      <//>

      <div className="page-grid">
        <${Card} className="status-card">
          <div className="status-message">${statusText}</div>
          <div className="status-grid" style=${{ marginTop: 18 }}>
            <div className="status-block">
              <div className="status-label">监控数量</div>
              <div className="status-value">${formatInt(data?.count || 0)}</div>
            </div>
            <div className="status-block">
              <div className="status-label">更新日期</div>
              <div className="status-value">${TODAY}</div>
            </div>
          </div>
        <//>

        <${Card} className="panel-card table-card">
          <div className="toolbar-row" style=${{ marginBottom: 16 }}>
            <div>
              <h2 className="section-title">监控列表</h2>
              <div className="toolbar-copy">点击股票行可直接打开个股详情。</div>
            </div>
          </div>

          <${Table}
            columns=${columns}
            dataSource=${items}
            rowKey=${(row) => row.id}
            pagination=${false}
            loading=${loading}
            scroll=${{ x: 980 }}
            locale=${{
              emptyText: html`<div className="empty-block">还没有添加任何价格监控</div>`,
            }}
            onRow=${(record) => ({
              onClick: () => onOpenDetail(record.symbol, items),
              style: { cursor: "pointer" },
            })}
          />
        <//>
      </div>
    </div>
  `;
}

function BacktestPage({
  loading,
  runningLabel,
  statusText,
  formState,
  onChangeForm,
  onRun,
  onOpenDetail,
  data,
}) {
  const items = data?.items || [];
  const isScanMode = data?.mode === "scan";
  const [symbolFilter, setSymbolFilter] = useState("");
  const normalizedFilter = symbolFilter.trim().toUpperCase();
  const filteredItems = useMemo(() => {
    if (!normalizedFilter) return items;
    return items.filter((item) => {
      const symbol = String(item.symbol || "").toUpperCase();
      const name = String(item.name || "").toUpperCase();
      return symbol.includes(normalizedFilter) || name.includes(normalizedFilter);
    });
  }, [items, normalizedFilter]);

  useEffect(() => {
    setSymbolFilter("");
  }, [data?.target_date, data?.mode, items.length]);

  const columns = [
    {
      title: "股票",
      dataIndex: "symbol",
      key: "symbol",
      width: 170,
      render: (_, row) => renderStockCell(row),
    },
    {
      title: "结构分",
      dataIndex: "cont_score_box",
      key: "cont_score_box",
      width: 96,
      render: (value) => formatNumber(value, 1),
    },
    {
      title: "质量分",
      dataIndex: "cont_quality_score",
      key: "cont_quality_score",
      width: 96,
      render: (value) => formatNumber(value, 0),
    },
    {
      title: "等级",
      dataIndex: "cont_quality_grade",
      key: "cont_quality_grade",
      width: 90,
      render: (value) => {
        const colorMap = { S: "gold", A: "green", B: "blue", C: "default" };
        return html`<${Tag} color=${colorMap[value] || "default"}>${value || "--"}<//>`;
      },
    },
    {
      title: "适用",
      dataIndex: "cont_is_applicable",
      key: "cont_is_applicable",
      width: 90,
      render: (value) => html`<${Tag} color=${value ? "green" : "default"}>${formatBoolean(value)}<//>`,
    },
    {
      title: "Pool",
      key: "pool",
      width: 90,
      render: (_, row) => row.cont_prior_trend_ok
        ? html`<${Tag} color="blue">A<//>`
        : row.cont_pool_b
          ? html`<${Tag} color="cyan">B<//>`
          : html`<${Tag}>--<//>`,
    },
    {
      title: "缩量",
      dataIndex: "cont_volume_trend_ok",
      key: "cont_volume_trend_ok",
      width: 90,
      render: (value) => html`<${Tag} color=${value ? "green" : "default"}>${formatBoolean(value)}<//>`,
    },
    {
      title: "振幅",
      dataIndex: "cont_box_range_pct",
      key: "cont_box_range_pct",
      width: 96,
      render: (value) => formatPercent(value, 1),
    },
    {
      title: "箱体周数",
      dataIndex: "cont_box_duration_weeks",
      key: "cont_box_duration_weeks",
      width: 100,
      render: (value) => formatInt(value),
    },
    {
      title: "走平周数",
      dataIndex: "cont_flatten_duration_weeks",
      key: "cont_flatten_duration_weeks",
      width: 100,
      render: (value) => formatInt(value),
    },
    {
      title: "基底周数",
      dataIndex: "cont_base_duration_weeks",
      key: "cont_base_duration_weeks",
      width: 100,
      render: (value) => formatInt(value),
    },
    {
      title: "成熟度",
      dataIndex: "cont_base_maturity_score",
      key: "cont_base_maturity_score",
      width: 96,
      render: (value) => formatNumber(value, 0),
    },
    {
      title: "可用周数",
      dataIndex: "available_weeks",
      key: "available_weeks",
      width: 100,
      render: (value) => formatInt(value),
    },
    {
      title: "原因",
      dataIndex: "cont_quality_reason",
      key: "cont_quality_reason",
      render: (value, row) => html`
        <span className="reason-text" title=${value || row.error || ""}>${value || row.error || "--"}</span>
      `,
    },
  ];

  return html`
    <div className="page-shell">
      <${Card} className="hero-card">
        <div className="toolbar-row">
          <div>
            <div className="hero-kicker">Backtest</div>
            <div className="hero-title" style=${{ fontSize: "34px", marginTop: 16 }}>回测页</div>
            <div className="hero-copy">
              保留原有两种模式：输入代码执行单股回测；不输入代码时按日期执行全市场扫描。
              详情弹窗、轮播切换和排序展示与原有逻辑保持一致。
            </div>
          </div>
        </div>
      <//>

      <div className="page-grid">
        <${Card} className="panel-card">
          <div className="toolbar-row" style=${{ marginBottom: 16 }}>
            <div>
              <h2 className="section-title">运行参数</h2>
              <div className="toolbar-copy">每行支持 code + date，也可以统一使用右侧日期输入框。留空代码即可执行全市场扫描。</div>
            </div>
          </div>

          <${Form} layout="vertical">
            <${Row} gutter=${16}>
              <${Col} xs=${24} lg=${16}>
                <${Form.Item} label="股票代码和目标日期">
                  <${TextArea}
                    rows=${6}
                    value=${formState.symbols}
                    onChange=${(event) => onChangeForm("symbols", event.target.value)}
                    placeholder=${"000831.SZ 2021-07-02\n601985.SH 2023-01-15\n600111.SH 2025-06-01"}
                  />
                  <div className="text-area-hint" style=${{ marginTop: 8 }}>
                    示例：每行一只股票。只输入代码时，会默认使用统一目标日期。
                  </div>
                <//>
              <//>
              <${Col} xs=${24} lg=${8}>
                <${Form.Item} label="统一目标日期">
                  <${DatePicker}
                    style=${{ width: "100%" }}
                    value=${formState.date ? dayjs(formState.date) : null}
                    onChange=${(value) => onChangeForm("date", value ? value.format("YYYY-MM-DD") : "")}
                  />
                <//>
                <${Space} direction="vertical" size="middle" style=${{ width: "100%" }}>
                  <${Button} type="primary" size="large" onClick=${onRun} loading=${loading} block>
                    ${runningLabel}
                  <//>
                  <${Alert}
                    className="floating-alert"
                    type=${loading ? "info" : "success"}
                    showIcon=${true}
                    message=${loading ? "任务执行中" : "任务等待运行"}
                    description=${statusText || "输入参数后点击运行回测。"}
                  />
                <//>
              <//>
            <//>
          <//>
        <//>

        <${Card} className="panel-card table-card">
          <div className="toolbar-row" style=${{ marginBottom: 16 }}>
            <div>
              <h2 className="section-title">回测结果</h2>
              <div className="toolbar-copy">
                ${isScanMode
                  ? `点击行可查看个股详情。全市场扫描${SCAN_RULE_LABEL}，并支持分页浏览。`
                  : "点击行可查看个股详情。手动模式按结构分排序，保留原有单股回测输出。"}
              </div>
            </div>
            <div className="toolbar-actions">
              <${Tag} color=${isScanMode ? "cyan" : "blue"}>${isScanMode ? "全市场扫描" : "单股回测"}<//>
              <${Tag} color="default">目标日期 ${data?.target_date || formState.date || TODAY}<//>
              ${isScanMode ? html`<${Tag} color="purple">${SCAN_RULE_LABEL}<//>` : null}
            </div>
          </div>
          <div className="toolbar-row" style=${{ marginBottom: 16, gap: 12 }}>
            <div className="toolbar-copy">
              ${normalizedFilter ? `当前筛选后 ${filteredItems.length} 条结果` : `当前共 ${items.length} 条结果`}
            </div>
            <div className="toolbar-actions" style=${{ minWidth: "min(100%, 360px)" }}>
              <${Input}
                allowClear=${true}
                value=${symbolFilter}
                onChange=${(event) => setSymbolFilter(event.target.value)}
                placeholder="按股票代码或名称筛选结果"
                size="large"
              />
            </div>
          </div>

          <${Table}
            className="backtest-table"
            columns=${columns}
            dataSource=${filteredItems}
            rowKey=${(row) => `${row.symbol}-${row.latest_date || ""}`}
            rowClassName=${(record) => record.is_new_hit ? "ranking-row-new-hit" : ""}
            pagination=${isScanMode ? { pageSize: 20, showSizeChanger: false, hideOnSinglePage: true } : false}
            loading=${loading}
            scroll=${{ x: 1320 }}
            locale=${{
              emptyText: html`<div className="empty-block">${normalizedFilter ? "没有匹配的股票结果" : "输入参数后运行回测"}</div>`,
            }}
            onRow=${(record) => ({
              onClick: () => onOpenDetail(record.symbol, filteredItems),
              style: { cursor: "pointer" },
            })}
          />
        <//>
      </div>
    </div>
  `;
}

function BoxBacktestPage({
  loading,
  runningLabel,
  statusText,
  formState,
  onChangeForm,
  onRun,
  onOpenDetail,
  data,
  pageKicker = "Box Backtest",
  pageTitle = "箱体回测页",
  pageCopy = "这个页面和普通回测页交互一致，但计算逻辑独立：按目标日期切片日线，重新识别 Demand 支撑、真实回踩 Touch、历史反弹能力和当前距离支撑的观察价值。",
  parameterCopy = "每行支持 code + date，也可以统一使用右侧日期输入框。留空代码即可执行全市场扫描。",
  textareaPlaceholder = "000831.SZ 2026-07-24\n300142.SZ 2026-06-09\n600984.SH 2026-07-24",
  idleDescription = "输入参数后点击运行回测。",
  resultTitle = "回测结果",
  scanModeText = "全市场扫描",
  manualModeText = "单股回测",
  emptyText = "输入参数后运行回测",
}) {
  const items = data?.items || [];
  const isScanMode = data?.mode === "scan";
  const [symbolFilter, setSymbolFilter] = useState("");
  const normalizedFilter = symbolFilter.trim().toUpperCase();
  const filteredItems = useMemo(() => {
    if (!normalizedFilter) return items;
    return items.filter((item) => {
      const symbol = String(item.symbol || "").toUpperCase();
      const name = String(item.name || "").toUpperCase();
      return symbol.includes(normalizedFilter) || name.includes(normalizedFilter);
    });
  }, [items, normalizedFilter]);

  useEffect(() => {
    setSymbolFilter("");
  }, [data?.target_date, data?.mode, items.length]);

  const columns = [
    {
      title: "股票",
      dataIndex: "symbol",
      key: "symbol",
      width: 170,
      render: (_, row) => renderStockCell(row),
    },
    {
      title: "候选",
      dataIndex: "demand_support_candidate",
      key: "demand_support_candidate",
      width: 90,
      render: (value) => html`<${Tag} color=${value ? "green" : "default"}>${formatBoolean(value)}<//>`,
    },
    {
      title: "支撑分",
      dataIndex: "demand_support_score",
      key: "demand_support_score",
      width: 100,
      render: (value) => html`<span className=${Number(value) >= 85 ? "positive-text" : ""}>${formatNumber(value, 1)}</span>`,
    },
    {
      title: "等级",
      dataIndex: "demand_support_grade",
      key: "demand_support_grade",
      width: 86,
      render: (value) => {
        const colorMap = { S: "gold", A: "green", B: "blue", C: "default" };
        return html`<${Tag} color=${colorMap[value] || "default"}>${value || "--"}<//>`;
      },
    },
    {
      title: "支撑质量",
      dataIndex: "demand_support_score_support_quality",
      key: "demand_support_score_support_quality",
      width: 110,
      render: (value) => formatNumber(value, 1),
    },
    {
      title: "历史反弹",
      dataIndex: "demand_support_score_historical_rebound",
      key: "demand_support_score_historical_rebound",
      width: 110,
      render: (value) => formatNumber(value, 1),
    },
    {
      title: "当前距离",
      dataIndex: "demand_support_score_current_distance",
      key: "demand_support_score_current_distance",
      width: 110,
      render: (value) => formatNumber(value, 1),
    },
    {
      title: "距Demand",
      dataIndex: "demand_support_approach_gap_pct",
      key: "demand_support_approach_gap_pct",
      width: 110,
      render: (value) => formatPercent(value, 1),
    },
    {
      title: "20D动能",
      dataIndex: "demand_support_approach_energy_pct",
      key: "demand_support_approach_energy_pct",
      width: 110,
      render: (value) => formatPercent(value, 1),
    },
    {
      title: "回踩缩量",
      dataIndex: "demand_support_pullback_volume_ratio",
      key: "demand_support_pullback_volume_ratio",
      width: 110,
      render: (value) => formatNumber(value, 2),
    },
    {
      title: "支撑区",
      key: "zone",
      width: 150,
      render: (_, row) => `${formatNumber(row.demand_support_lower, 2)} - ${formatNumber(row.demand_support_upper, 2)}`,
    },
    {
      title: "触底",
      dataIndex: "demand_support_touch_count",
      key: "demand_support_touch_count",
      width: 86,
      render: (value) => formatInt(value),
    },
    {
      title: "完整Cycle",
      dataIndex: "demand_support_swing_count",
      key: "demand_support_swing_count",
      width: 105,
      render: (value) => formatInt(value),
    },
    {
      title: "平均Swing",
      dataIndex: "demand_support_avg_swing_pct",
      key: "demand_support_avg_swing_pct",
      width: 110,
      render: (_, row) => formatPercent(row.demand_support_avg_swing_pct ?? row.demand_support_avg_rebound_pct, 1),
    },
    {
      title: "箱顶",
      dataIndex: "demand_support_top_price",
      key: "demand_support_top_price",
      width: 96,
      render: (value) => formatNumber(value, 2),
    },
    {
      title: "反弹效率",
      dataIndex: "demand_support_rebound_efficiency",
      key: "demand_support_rebound_efficiency",
      width: 110,
      render: (value) => Number.isFinite(Number(value)) ? formatPercent(Number(value) * 100, 0) : "--",
    },
    {
      title: "箱体利用",
      dataIndex: "demand_support_box_utilization_pct",
      key: "demand_support_box_utilization_pct",
      width: 110,
      render: (value) => formatPercent(value, 0),
    },
    {
      title: "平均穿透",
      dataIndex: "demand_support_avg_penetration_pct",
      key: "demand_support_avg_penetration_pct",
      width: 110,
      render: (value) => formatPercent(value, 2),
    },
    {
      title: "箱体高度",
      dataIndex: "demand_support_box_height_pct",
      key: "demand_support_box_height_pct",
      width: 110,
      render: (value) => formatPercent(value, 1),
    },
    {
      title: "顶部稳定",
      dataIndex: "demand_support_top_stability_pct",
      key: "demand_support_top_stability_pct",
      width: 110,
      render: (value) => formatPercent(value, 1),
    },
    {
      title: "持续",
      dataIndex: "demand_support_duration_weeks",
      key: "demand_support_duration_weeks",
      width: 116,
      render: (_, row) => {
        const weeks = formatInt(row.demand_support_duration_weeks);
        const bars = Number(row.demand_support_duration_bars || 0);
        const unit = row.demand_support_duration_unit === "daily" ? "日线" : "周线";
        return bars ? `${weeks}周/${bars}${unit}bar` : `${weeks}周`;
      },
    },
    {
      title: "数据日",
      dataIndex: "latest_date",
      key: "latest_date",
      width: 120,
    },
    {
      title: "原因",
      dataIndex: "demand_support_reason",
      key: "demand_support_reason",
      render: (value, row) => html`
        <span className="reason-text" title=${value || row.error || ""}>${value || row.error || "--"}</span>
      `,
    },
  ];

  return html`
    <div className="page-shell">
      <${Card} className="hero-card">
        <div className="toolbar-row">
          <div>
            <div className="hero-kicker">${pageKicker}</div>
            <div className="hero-title" style=${{ fontSize: "34px", marginTop: 16 }}>${pageTitle}</div>
            <div className="hero-copy">${pageCopy}</div>
          </div>
        </div>
      <//>

      <div className="page-grid">
        <${Card} className="panel-card">
          <div className="toolbar-row" style=${{ marginBottom: 16 }}>
            <div>
              <h2 className="section-title">运行参数</h2>
              <div className="toolbar-copy">${parameterCopy}</div>
            </div>
          </div>

          <${Form} layout="vertical">
            <${Row} gutter=${16}>
              <${Col} xs=${24} lg=${16}>
                <${Form.Item} label="股票代码和目标日期">
                  <${TextArea}
                    rows=${6}
                    value=${formState.symbols}
                    onChange=${(event) => onChangeForm("symbols", event.target.value)}
                    placeholder=${textareaPlaceholder}
                  />
                  <div className="text-area-hint" style=${{ marginTop: 8 }}>
                    示例：每行一只股票。只输入代码时，会默认使用统一目标日期。
                  </div>
                <//>
              <//>
              <${Col} xs=${24} lg=${8}>
                <${Form.Item} label="统一目标日期">
                  <${DatePicker}
                    style=${{ width: "100%" }}
                    value=${formState.date ? dayjs(formState.date) : null}
                    onChange=${(value) => onChangeForm("date", value ? value.format("YYYY-MM-DD") : "")}
                  />
                <//>
                <${Space} direction="vertical" size="middle" style=${{ width: "100%" }}>
                  <${Button} type="primary" size="large" onClick=${onRun} loading=${loading} block>
                    ${runningLabel}
                  <//>
                  <${Alert}
                    className="floating-alert"
                    type=${loading ? "info" : "success"}
                    showIcon=${true}
                    message=${loading ? "任务执行中" : "任务等待运行"}
                    description=${statusText || idleDescription}
                  />
                <//>
              <//>
            <//>
          <//>
        <//>

        <${Card} className="panel-card table-card">
          <div className="toolbar-row" style=${{ marginBottom: 16 }}>
            <div>
              <h2 className="section-title">${resultTitle}</h2>
              <div className="toolbar-copy">
                ${isScanMode
                  ? `点击行可查看个股详情。全市场Demand扫描${BOX_SCAN_RULE_LABEL}，并支持分页浏览。`
                  : "点击行可查看个股详情。手动模式会展示候选与非候选的Demand评分明细。"}
              </div>
            </div>
            <div className="toolbar-actions">
              <${Tag} color=${isScanMode ? "cyan" : "blue"}>${isScanMode ? scanModeText : manualModeText}<//>
              <${Tag} color="default">目标日期 ${data?.target_date || formState.date || TODAY}<//>
              ${isScanMode ? html`<${Tag} color="green">${BOX_SCAN_RULE_LABEL}<//>` : null}
            </div>
          </div>
          <div className="toolbar-row" style=${{ marginBottom: 16, gap: 12 }}>
            <div className="toolbar-copy">
              ${normalizedFilter ? `当前筛选后 ${filteredItems.length} 条结果` : `当前共 ${items.length} 条结果`}
            </div>
            <div className="toolbar-actions" style=${{ minWidth: "min(100%, 360px)" }}>
              <${Input}
                allowClear=${true}
                value=${symbolFilter}
                onChange=${(event) => setSymbolFilter(event.target.value)}
                placeholder="按股票代码或名称筛选结果"
                size="large"
              />
            </div>
          </div>

          <${Table}
            className="backtest-table"
            columns=${columns}
            dataSource=${filteredItems}
            rowKey=${(row) => `${row.symbol}-${row.latest_date || ""}`}
            rowClassName=${(record) => record.is_new_hit ? "ranking-row-new-hit" : ""}
            pagination=${isScanMode ? { pageSize: 20, showSizeChanger: false, hideOnSinglePage: true } : false}
            loading=${loading}
            scroll=${{ x: 2400 }}
            locale=${{
              emptyText: html`<div className="empty-block">${normalizedFilter ? "没有匹配的股票结果" : emptyText}</div>`,
            }}
            onRow=${(record) => ({
              onClick: () => onOpenDetail(record.symbol, filteredItems),
              style: { cursor: "pointer" },
            })}
          />
        <//>
      </div>
    </div>
  `;
}

function AppContent() {
  const [activePage, setActivePage] = useState(PAGE_OVERVIEW);
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [overviewStatus, setOverviewStatus] = useState("正在准备今日排行榜。");
  const [overviewData, setOverviewData] = useState({ items: [], target_date: TODAY });
  const [demandLoading, setDemandLoading] = useState(false);
  const [demandStatus, setDemandStatus] = useState("正在准备Demand支撑回踩榜。");
  const [demandData, setDemandData] = useState({ items: [], count: 0 });
  const [monitorLoading, setMonitorLoading] = useState(false);
  const [monitorStatus, setMonitorStatus] = useState("请从个股详情中添加价格监控。");
  const [monitorData, setMonitorData] = useState({ items: [], count: 0 });
  const [monitorModalOpen, setMonitorModalOpen] = useState(false);
  const [monitorSubmitting, setMonitorSubmitting] = useState(false);
  const [monitorDraft, setMonitorDraft] = useState({ symbol: "", latestClose: null, targetPrice: "" });
  const [backtestLoading, setBacktestLoading] = useState(false);
  const [backtestStatus, setBacktestStatus] = useState("输入股票代码和日期后点击运行回测。");
  const [backtestData, setBacktestData] = useState({ items: [], target_date: TODAY });
  const [demandBacktestLoading, setDemandBacktestLoading] = useState(false);
  const [demandBacktestStatus, setDemandBacktestStatus] = useState("输入股票代码和日期后点击运行Demand回测。");
  const [demandBacktestData, setDemandBacktestData] = useState({ items: [], target_date: TODAY });
  const [boxBacktestLoading, setBoxBacktestLoading] = useState(false);
  const [boxBacktestStatus, setBoxBacktestStatus] = useState("输入股票代码和日期后点击运行箱体回测。");
  const [boxBacktestData, setBoxBacktestData] = useState({ items: [], target_date: TODAY });
  const [formState, setFormState] = useState({ symbols: "", date: TODAY });
  const [demandBacktestFormState, setDemandBacktestFormState] = useState({ symbols: "", date: TODAY });
  const [boxFormState, setBoxFormState] = useState({ symbols: "", date: TODAY });
  const [detailState, setDetailState] = useState({ open: false, symbol: "", symbols: [] });
  const polling = usePollingBacktestJob();
  const demandBacktestPolling = usePollingBacktestJob();
  const boxPolling = usePollingBacktestJob();
  const latestDetailListRef = useRef([]);

  const openDetail = (symbol, sourceItems) => {
    latestDetailListRef.current = (sourceItems || []).map((item) => item.symbol).filter(Boolean);
    setDetailState({
      open: true,
      symbol,
      symbols: latestDetailListRef.current,
    });
  };

  const navigateDetail = (direction) => {
    const list = detailState.symbols || [];
    const currentIndex = list.indexOf(detailState.symbol);
    if (currentIndex < 0) return;
    const nextIndex = direction === "next" ? currentIndex + 1 : currentIndex - 1;
    if (nextIndex < 0 || nextIndex >= list.length) return;
    setDetailState((prev) => ({ ...prev, symbol: list[nextIndex] }));
  };

  const loadMonitors = async () => {
    setMonitorLoading(true);
    try {
      const payload = await fetchJson("/api/price-monitors");
      setMonitorData(payload);
      setMonitorStatus(payload.count ? `当前共有 ${payload.count} 条价格监控。` : "还没有添加任何价格监控。");
    } catch (error) {
      setMonitorStatus(error.message || "加载监控失败");
      message.error(error.message || "加载监控失败");
    } finally {
      setMonitorLoading(false);
    }
  };

  const submitMonitor = async () => {
    const targetPrice = Number(monitorDraft.targetPrice);
    if (!Number.isFinite(targetPrice) || targetPrice <= 0) {
      message.error("目标价格必须是大于 0 的数字");
      return;
    }
    setMonitorSubmitting(true);
    try {
      await fetchJson("/api/price-monitors", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: monitorDraft.symbol, target_price: targetPrice }),
      });
      message.success(`${monitorDraft.symbol} 已加入价格监控`);
      setMonitorModalOpen(false);
      loadMonitors();
    } catch (error) {
      message.error(error.message || "添加监控失败");
    } finally {
      setMonitorSubmitting(false);
    }
  };

  const handleAddMonitor = async (symbol, latestClose) => {
    setMonitorDraft({
      symbol: symbol || "",
      latestClose: Number.isFinite(Number(latestClose)) ? Number(latestClose) : null,
      targetPrice: Number.isFinite(Number(latestClose)) ? Number(latestClose).toFixed(2) : "",
    });
    setMonitorModalOpen(true);
  };
  const handleDeleteMonitor = async (row) => {
    if (!row?.id) return;
    const confirmed = window.confirm(`确认删除 ${row.symbol || ""} 的价格监控吗？`);
    if (!confirmed) return;
    try {
      await fetchJson(`/api/price-monitors/${encodeURIComponent(row.id)}`, { method: "DELETE" });
      message.success("监控已删除");
      loadMonitors();
    } catch (error) {
      message.error(error.message || "删除监控失败");
    }
  };

  const updateBacktestForm = (key, value) => {
    setFormState((prev) => ({ ...prev, [key]: value }));
  };

  const updateDemandBacktestForm = (key, value) => {
    setDemandBacktestFormState((prev) => ({ ...prev, [key]: value }));
  };

  const updateBoxBacktestForm = (key, value) => {
    setBoxFormState((prev) => ({ ...prev, [key]: value }));
  };

  const runBacktestRequest = async ({
    symbols,
    date,
    reuseScan = false,
    forceRefresh = false,
    target = "backtest",
    endpoint = "/api/backtest",
  }) => {
    const setters = target === "overview"
      ? { setLoading: setOverviewLoading, setStatus: setOverviewStatus, setData: setOverviewData }
      : target === "demandBacktest"
        ? { setLoading: setDemandBacktestLoading, setStatus: setDemandBacktestStatus, setData: setDemandBacktestData }
      : target === "boxBacktest"
        ? { setLoading: setBoxBacktestLoading, setStatus: setBoxBacktestStatus, setData: setBoxBacktestData }
        : { setLoading: setBacktestLoading, setStatus: setBacktestStatus, setData: setBacktestData };
    const { setLoading, setStatus, setData } = setters;
    const isScan = !symbols.trim();
    const isDemandBacktest = target === "demandBacktest";
    const isBoxBacktest = target === "boxBacktest";

    if (!date) {
      setStatus("请至少输入目标日期。");
      return;
    }

    setLoading(true);
    setStatus(isScan
      ? (isDemandBacktest ? "全市场Demand回踩扫描已启动，正在等待结果..." : isBoxBacktest ? "全市场箱体扫描已启动，正在等待结果..." : "全市场扫描已启动，正在等待结果...")
      : (isDemandBacktest ? "正在计算Demand回测结果..." : isBoxBacktest ? "正在计算箱体回测结果..." : "正在计算回测结果..."));

    try {
      const payload = await fetchJson(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbols, date, reuse_scan: reuseScan, force_refresh: forceRefresh }),
      });

      if (isScan && payload.job_id) {
        setStatus(`扫描中... (job=${payload.job_id})`);
        const poller = target === "demandBacktest" ? demandBacktestPolling : target === "boxBacktest" ? boxPolling : polling;
        poller.poll({
          jobId: payload.job_id,
          endpoint,
          onProgress: (_attempts, pollPayload) => {
            const processed = formatInt(pollPayload?.processed, "0");
            const total = formatInt(pollPayload?.total, "?");
            const candidates = formatInt(pollPayload?.candidates_total, "0");
            const elapsed = formatElapsedSeconds(pollPayload?.elapsed_seconds);
            setStatus(`扫描中... 已扫描 ${processed}/${total}，命中 ${candidates}，已运行 ${elapsed} (job=${payload.job_id})`);
          },
          onDone: (donePayload) => {
            setData(donePayload);
            setStatus(buildStatusText(donePayload));
            setLoading(false);
          },
          onError: (error) => {
            setStatus(error.message || "轮询失败");
            setLoading(false);
          },
        });
        return;
      }

      setData(payload);
      setStatus(buildStatusText(payload));
      if (isScan) {
        setLoading(false);
      }
    } catch (error) {
      setStatus(error.message || "请求失败");
      message.error(error.message || "请求失败");
    } finally {
      if (!isScan) {
        setLoading(false);
      }
    }
  };

  const buildStatusText = (payload) => {
    const items = payload.items || [];
    const modernScanInfo = payload.mode === "scan"
      ? ` 全市场扫描 ${formatInt(payload.scanned, "?")} 只，用时 ${formatElapsedSeconds(payload.elapsed)}，共命中 ${formatInt(payload.candidates_total, "?")} 个候选，当前显示前 ${formatInt(payload.items?.length, "0")} 个。`
      : "";
    const newHitInfo = payload.comparison_date
      ? ` 较 ${payload.comparison_date} 新增命中 ${formatInt(payload.new_hit_count || 0, "0")} 只。`
      : "";
    return `${formatInt(items.length, "0")} 条结果，目标日期 ${payload.target_date || TODAY}。${modernScanInfo}${newHitInfo}`.trim();
  };

  const loadOverview = (forceRefresh = false) => runBacktestRequest({
    symbols: "",
    date: TODAY,
    reuseScan: true,
    forceRefresh,
    target: "overview",
  });

  const loadDemandSupport = async () => {
    setDemandLoading(true);
    try {
      const payload = await fetchJson("/api/demand-support/ranking");
      setDemandData(payload);
      const newHitText = payload.comparison_date
        ? `，较 ${payload.comparison_date} 新增命中 ${formatInt(payload.new_hit_count || 0, "0")} 只`
        : "";
      setDemandStatus(payload.count
        ? `当前展示 ${payload.items?.length || 0} 条Demand回踩候选${newHitText}。`
        : "暂无Demand回踩候选。");
    } catch (error) {
      setDemandStatus(error.message || "加载Demand支撑回踩榜失败");
      message.error(error.message || "加载Demand支撑回踩榜失败");
    } finally {
      setDemandLoading(false);
    }
  };

  const handleRunBacktest = () => runBacktestRequest({
    symbols: formState.symbols || "",
    date: formState.date || TODAY,
    target: "backtest",
  });

  const handleRunDemandBacktest = () => runBacktestRequest({
    symbols: demandBacktestFormState.symbols || "",
    date: demandBacktestFormState.date || TODAY,
    target: "demandBacktest",
    endpoint: "/api/box-backtest",
  });

  const handleRunBoxBacktest = () => runBacktestRequest({
    symbols: boxFormState.symbols || "",
    date: boxFormState.date || TODAY,
    target: "boxBacktest",
    endpoint: "/api/box-backtest",
  });

  useEffect(() => {
    loadOverview(false);
    loadDemandSupport();
    loadMonitors();
  }, []);

  const menuItems = [
    { key: PAGE_OVERVIEW, label: "总览页" },
    { key: PAGE_DEMAND_SUPPORT, label: "Demand回踩" },
    { key: PAGE_DEMAND_BACKTEST, label: "Demand回测" },
    { key: PAGE_BOX_BACKTEST, label: "箱体回测" },
    { key: PAGE_BACKTEST, label: "回测页" },
  ];

  menuItems.push({ key: PAGE_MONITOR, label: "监控" });

  const activeView = activePage === PAGE_OVERVIEW
    ? html`
        <${React.Fragment}>
          <${OverviewPage}
            loading=${overviewLoading}
            data=${overviewData}
            statusText=${overviewStatus}
            onRefresh=${() => loadOverview(true)}
            onOpenDetail=${openDetail}
          />
          <div className="page-shell" style=${{ paddingTop: 0 }}>
            <${SearchPanel} onOpenDetail=${openDetail} />
          </div>
        <//>
      `
    : activePage === PAGE_DEMAND_SUPPORT
      ? html`
          <${DemandSupportPage}
            loading=${demandLoading}
            data=${demandData}
            statusText=${demandStatus}
            onRefresh=${loadDemandSupport}
            onOpenDetail=${openDetail}
          />
        `
    : activePage === PAGE_DEMAND_BACKTEST
      ? html`
          <${BoxBacktestPage}
            loading=${demandBacktestLoading}
            runningLabel=${!demandBacktestFormState.symbols.trim() ? "运行全市场Demand扫描" : "运行Demand回测"}
            statusText=${demandBacktestStatus}
            formState=${demandBacktestFormState}
            onChangeForm=${updateDemandBacktestForm}
            onRun=${handleRunDemandBacktest}
            onOpenDetail=${openDetail}
            data=${demandBacktestData}
            pageKicker="Demand Backtest"
            pageTitle="Demand回踩回测页"
            pageCopy="按目标日期切片日线，回到当时重新识别Demand支撑区，评估支撑质量、历史反弹能力、当前距离和回踩缩量，用来验证某一天是否已经值得观察或挂单。"
            parameterCopy="每行支持 code + date，也可以统一使用右侧日期输入框。留空代码即可执行全市场Demand回踩扫描。"
            textareaPlaceholder=${"600984.SH 2026-07-16\n301459.SZ 2026-07-20\n920239.BJ 2026-07-20"}
            idleDescription="输入参数后点击运行Demand回测。"
            resultTitle="Demand回测结果"
            scanModeText="全市场Demand扫描"
            manualModeText="单股Demand回测"
            emptyText="输入参数后运行Demand回测"
          />
        `
    : activePage === PAGE_MONITOR
      ? html`
          <${MonitorPage}
            loading=${monitorLoading}
            statusText=${monitorStatus}
            data=${monitorData}
            onRefresh=${loadMonitors}
            onDelete=${handleDeleteMonitor}
            onOpenDetail=${openDetail}
          />
        `
    : activePage === PAGE_BOX_BACKTEST
      ? html`
          <${BoxBacktestPage}
            loading=${boxBacktestLoading}
            runningLabel=${!boxFormState.symbols.trim() ? "运行全市场箱体扫描" : "运行箱体回测"}
            statusText=${boxBacktestStatus}
            formState=${boxFormState}
            onChangeForm=${updateBoxBacktestForm}
            onRun=${handleRunBoxBacktest}
            onOpenDetail=${openDetail}
            data=${boxBacktestData}
          />
        `
    : html`
        <${BacktestPage}
          loading=${backtestLoading}
          runningLabel=${!formState.symbols.trim() ? "运行全市场扫描" : "运行回测"}
          statusText=${backtestStatus}
          formState=${formState}
          onChangeForm=${updateBacktestForm}
          onRun=${handleRunBacktest}
          onOpenDetail=${openDetail}
          data=${backtestData}
        />
      `;

  return html`
    <div className="app-shell">
      <${Layout} className="app-frame">
        <${Sider} width=${288} breakpoint="lg" collapsedWidth="0" className="sidebar-shell">
          <div className="sidebar-inner">
            <div>
              <div className="brand-badge">Weinstein Console</div>
              <div className="brand-title">温斯坦回测看板</div>
              <div className="brand-copy">
                总览页展示今日全市场回测排行榜，Demand回踩页展示当前榜单，箱体回测页按历史日期重算Demand支撑与回踩机会，普通回测页继续承载原有逻辑。
              </div>
            </div>

            <${Menu}
              className="menu-shell"
              mode="inline"
              selectedKeys=${[activePage]}
              items=${menuItems}
              onClick=${({ key }) => setActivePage(key)}
            />

            <div className="side-note">
              <strong>UI 重构说明</strong>
              保留现有接口与核心计算逻辑，只重构前端结构、交互入口与视觉表现，并接入 Ant Design 组件体系。
            </div>
          </div>
        <//>

        <${Layout}>
          <${Content} className="content-shell">
            ${activeView}
          <//>
        <//>
      <//>

      <${DetailModal}
        open=${detailState.open}
        symbol=${detailState.symbol}
        symbolList=${detailState.symbols}
        onClose=${() => setDetailState({ open: false, symbol: "", symbols: [] })}
        onNavigate=${navigateDetail}
        onAddMonitor=${handleAddMonitor}
      />

      <${Modal}
        open=${monitorModalOpen}
        title="添加价格监控"
        onCancel=${() => {
          if (!monitorSubmitting) setMonitorModalOpen(false);
        }}
        onOk=${submitMonitor}
        okText="保存"
        cancelText="取消"
        confirmLoading=${monitorSubmitting}
        destroyOnClose=${false}
      >
        <${Form} layout="vertical">
          <${Form.Item} label="股票代码">
            <${Input} value=${monitorDraft.symbol} disabled=${true} />
          <//>
          <${Form.Item} label="现价">
            <${Input}
              value=${monitorDraft.latestClose !== null ? formatNumber(monitorDraft.latestClose, 2) : "--"}
              disabled=${true}
            />
          <//>
          <${Form.Item} label="目标价格" required=${true}>
            <${Input}
              inputMode="decimal"
              value=${monitorDraft.targetPrice}
              onChange=${(event) => setMonitorDraft((prev) => ({ ...prev, targetPrice: event.target.value }))}
              placeholder="请输入目标价格"
            />
          <//>
        <//>
      <//>
    </div>
  `;
}

function App() {
  return html`
    <${ConfigProvider}
      theme=${{
        token: {
          colorPrimary: "#1d4ed8",
          colorInfo: "#1d4ed8",
          borderRadius: 18,
          fontFamily: '"Noto Sans SC", "Microsoft YaHei", sans-serif',
          colorText: "#10213d",
          colorTextSecondary: "#5f7092",
          colorBgLayout: "transparent",
        },
        components: {
          Button: {
            controlHeightLG: 48,
            fontWeight: 700,
          },
          Card: {
            bodyPadding: 22,
          },
          Table: {
            headerBorderRadius: 16,
          },
          Modal: {
            titleFontSize: 28,
          },
        },
      }}
    >
      <${AntApp}>
        <${AppContent} />
      <//>
    <//>
  `;
}

const root = ReactDOM.createRoot(document.getElementById("root"));
root.render(html`<${App} />`);

