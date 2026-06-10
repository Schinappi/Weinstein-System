const navDashboard = document.getElementById('navDashboard');
const navStage1 = document.getElementById('navStage1');
const navRecommendations = document.getElementById('navRecommendations');
const navQuasiStage2 = document.getElementById('navQuasiStage2');
const pageDashboard = document.getElementById('pageDashboard');
const pageStage1 = document.getElementById('pageStage1');
const pageRecommendations = document.getElementById('pageRecommendations');
const pageQuasiStage2 = document.getElementById('pageQuasiStage2');
const updateStatusTitle = document.getElementById('updateStatusTitle');
const updateStatusMessage = document.getElementById('updateStatusMessage');
const updateStatusLevel = document.getElementById('updateStatusLevel');
const updateStatusMetrics = document.getElementById('updateStatusMetrics');
const systemLogButton = document.getElementById('systemLogButton');
const stage2Table = document.getElementById('stage2Table');
const quasiStage2Table = document.getElementById('quasiStage2Table');
const searchTable = document.getElementById('searchTable');
const searchInput = document.getElementById('searchInput');
const searchButton = document.getElementById('searchButton');
const detailModal = document.getElementById('detailModal');
const closeModal = document.getElementById('closeModal');
const systemLogModal = document.getElementById('systemLogModal');
const closeSystemLogModal = document.getElementById('closeSystemLogModal');
const systemLogStatusTag = document.getElementById('systemLogStatusTag');
const systemLogTitle = document.getElementById('systemLogTitle');
const systemLogSubtitle = document.getElementById('systemLogSubtitle');
const systemLogGrid = document.getElementById('systemLogGrid');
const systemLogMessage = document.getElementById('systemLogMessage');
const systemLogRaw = document.getElementById('systemLogRaw');
const modalTitle = document.getElementById('modalTitle');
const modalSymbol = document.getElementById('modalSymbol');
const analysisMarkdown = document.getElementById('analysisMarkdown');
const analysisLoading = document.getElementById('analysisLoading');
const metricGrid = document.getElementById('metricGrid');
const stage1Pill = document.getElementById('stage1Pill');
const stage2Pill = document.getElementById('stage2Pill');
const chartCanvas = document.getElementById('chartCanvas');
const chartTooltip = document.getElementById('chartTooltip');
const refreshStage2Btn = document.getElementById('refreshStage2Btn');
const refreshQuasiStage2Btn = document.getElementById('refreshQuasiStage2Btn');
const refreshStage1Btn = document.getElementById('refreshStage1Btn');
const refreshRecsBtn = document.getElementById('refreshRecsBtn');
const recsLoading = document.getElementById('recsLoading');
const recSTable = document.getElementById('recSTable');
const recATable = document.getElementById('recATable');
const recBpTable = document.getElementById('recBpTable');
const recBTable = document.getElementById('recBTable');
const recSSection = document.getElementById('recSSection');
const recASection = document.getElementById('recASection');
const recBpSection = document.getElementById('recBpSection');
const recBSection = document.getElementById('recBSection');
const stage1Table = document.getElementById('stage1Table');
const stage1DatePicker = document.getElementById('stage1DatePicker');
const precloseRunBtn = document.getElementById('precloseRunBtn');
const precloseLoading = document.getElementById('precloseLoading');

let currentStage1Data = [];

let currentChartState = null;
let currentDetailRequestId = 0;
let latestUpdateStatus = {};
let currentPage = 'dashboard';
let stage2DatePicker = document.getElementById('stage2DatePicker');
let quasiStage2DatePicker = document.getElementById('quasiStage2DatePicker');

async function boot() {
  bindEvents();
  switchPage('dashboard');
  await loadDashboard();
  await loadStage1();
  await loadRecommendations();
  await runSearch('');
}

function bindEvents() {
  [navDashboard, navStage1, navRecommendations, navQuasiStage2].forEach((button) => {
    button.addEventListener('click', () => switchPage(button.dataset.page));
  });
  searchButton.addEventListener('click', () => runSearch(searchInput.value));
  searchInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      runSearch(searchInput.value);
    }
  });
  systemLogButton.addEventListener('click', showSystemLogModal);
  closeModal.addEventListener('click', hideModal);
  closeSystemLogModal.addEventListener('click', hideSystemLogModal);
  stage2DatePicker.addEventListener('change', () => {
    loadRankingsByDate('stage2', stage2DatePicker.value);
  });
  refreshStage2Btn.addEventListener('click', () => refreshRankings('stage2'));
  refreshQuasiStage2Btn.addEventListener('click', () => refreshRankings('quasi-stage2'));
  refreshStage1Btn.addEventListener('click', () => refreshStage1());
  refreshRecsBtn.addEventListener('click', () => loadRecommendations({ force: true }));
  precloseRunBtn.addEventListener('click', runPreclose);
  quasiStage2DatePicker.addEventListener('change', () => {
    loadRankingsByDate('quasi-stage2', quasiStage2DatePicker.value);
  });
  stage1DatePicker.addEventListener('change', () => {
    loadStage1ByDate(stage1DatePicker.value);
  });
  detailModal.addEventListener('click', (event) => {
    if (event.target.dataset.close === 'true') {
      hideModal();
    }
  });
  systemLogModal.addEventListener('click', (event) => {
    if (event.target.dataset.systemLogClose === 'true') {
      hideSystemLogModal();
    }
  });
}

function switchPage(pageKey) {
  currentPage = pageKey;
  const pageMap = {
    dashboard: pageDashboard,
    stage1: pageStage1,
    recommendations: pageRecommendations,
    'quasi-stage2': pageQuasiStage2,
  };
  Object.entries(pageMap).forEach(([key, node]) => {
    node.classList.toggle('hidden', key !== pageKey);
  });
  [navDashboard, navStage1, navRecommendations, navQuasiStage2].forEach((button) => {
    button.classList.toggle('active', button.dataset.page === pageKey);
  });
}

async function refreshRankings(section) {
  const btn = section === 'stage2' ? refreshStage2Btn : refreshQuasiStage2Btn;
  btn.disabled = true;
  btn.textContent = '刷新中...';
  try {
    await fetch('/api/dashboard/refresh', { method: 'POST' });
    const response = await fetch('/api/dashboard');
    const payload = await response.json();
    if (section === 'stage2') {
      renderRankingTable(stage2Table, payload.stage2 || [], 'stage2');
    } else {
      renderQuasiStage2Table(quasiStage2Table, payload.quasi_stage2 || []);
    }
    populateDatePickers(payload.snapshot_dates || []);
  } catch (e) {
    console.error(e);
  } finally {
    btn.disabled = false;
    btn.textContent = '刷新';
  }
}

async function loadDashboard() {
  const response = await fetch('/api/dashboard');
  const payload = await response.json();
  renderUpdateStatus(payload.update_status || {});
  populateDatePickers(payload.snapshot_dates || []);
  renderRankingTable(stage2Table, payload.stage2 || [], 'stage2');
  renderQuasiStage2Table(quasiStage2Table, payload.quasi_stage2 || []);
}

async function runPreclose() {
  precloseRunBtn.disabled = true;
  precloseRunBtn.textContent = '⏳ 更新中...';
  precloseLoading.classList.remove('hidden');
  try {
    const resp = await fetch('/api/preclose/run', { method: 'POST' });
    const result = await resp.json();
    if (result.success) {
      // 重新加载所有排行榜
      await loadDashboard();
      await loadStage1();
      await loadRecommendations();
      precloseLoading.textContent = `✅ 更新完成（${result.elapsed_seconds}秒）`;
    } else {
      precloseLoading.textContent = `❌ ${result.message}`;
    }
  } catch (e) {
    precloseLoading.textContent = `❌ 网络错误: ${e.message}`;
  } finally {
    precloseRunBtn.disabled = false;
    precloseRunBtn.innerHTML = '<span class="btn-icon">🔄</span> 实时更新排行榜';
    setTimeout(() => {
      precloseLoading.classList.add('hidden');
      precloseLoading.textContent = '拉取腾讯行情并重算中...';
    }, 5000);
  }
}

function populateDatePickers(dates) {
  const fragment1 = document.createDocumentFragment();
  const fragment2 = document.createDocumentFragment();
  const fragment3 = document.createDocumentFragment();
  const makeOpt = (label, value) => {
    const o = document.createElement('option');
    o.value = value;
    o.textContent = label;
    return o;
  };
  fragment1.appendChild(makeOpt('最新数据', ''));
  fragment2.appendChild(makeOpt('最新数据', ''));
  fragment3.appendChild(makeOpt('最新数据', ''));
  dates.forEach((d) => {
    fragment1.appendChild(makeOpt(d, d));
    fragment2.appendChild(makeOpt(d, d));
    fragment3.appendChild(makeOpt(d, d));
  });
  stage2DatePicker.innerHTML = '';
  quasiStage2DatePicker.innerHTML = '';
  stage1DatePicker.innerHTML = '';
  stage2DatePicker.appendChild(fragment1);
  quasiStage2DatePicker.appendChild(fragment2);
  stage1DatePicker.appendChild(fragment3);
  stage2DatePicker.value = '';
  quasiStage2DatePicker.value = '';
  stage1DatePicker.value = '';
}

async function loadRankingsByDate(section, dateStr) {
  if (!dateStr) {
    // Reload latest data
    const response = await fetch('/api/dashboard');
    const payload = await response.json();
    if (section === 'stage2') {
      renderRankingTable(stage2Table, payload.stage2 || [], 'stage2');
    } else if (section === 'quasi-stage2') {
      renderQuasiStage2Table(quasiStage2Table, payload.quasi_stage2 || []);
    }
    return;
  }
  const response = await fetch(`/api/rankings/by-date?date=${encodeURIComponent(dateStr)}`);
  const payload = await response.json();
  if (payload.error || !payload.stage2) {
    if (section === 'stage2') {
      stage2Table.innerHTML = '<tr class="empty-row"><td colspan="10">无该日历史数据。</td></tr>';
    } else if (section === 'quasi-stage2') {
      quasiStage2Table.innerHTML = '<tr class="empty-row"><td colspan="10">无该日历史数据。</td></tr>';
    }
    return;
  }
  if (section === 'stage2') {
    renderRankingTable(stage2Table, payload.stage2 || [], 'stage2');
  } else if (section === 'quasi-stage2') {
    renderQuasiStage2Table(quasiStage2Table, payload.quasi_stage2 || []);
  }
}

function renderUpdateStatus(status) {
  latestUpdateStatus = status || {};
  const title = status.title || '缓存更新状态';
  const message = status.message || '--';
  const level = status.level === 'ok' ? '正常' : '提醒';
  updateStatusTitle.textContent = title;
  updateStatusMessage.textContent = message;
  updateStatusLevel.textContent = level;
  updateStatusLevel.className = `tag ${status.level === 'ok' ? 'ok' : 'warn'}`;

  const metrics = [
    { label: '最近完成时间', value: formatDateTime(status.finished_at) },
    { label: '最新交易日', value: status.latest_trade_date || '--' },
    { label: '总耗时', value: formatSeconds(status.total_runtime_seconds) },
    { label: '补数耗时', value: formatSeconds(status.stock_update_runtime_seconds) },
    { label: 'Phase1耗时', value: formatSeconds(status.phase1_runtime_seconds) },
  ];
  updateStatusMetrics.innerHTML = metrics.map((item) => `
    <div class="update-status-metric">
      <div class="label">${item.label}</div>
      <div class="value">${item.value}</div>
    </div>
  `).join('');
}

async function loadStage1() {
  try {
    const response = await fetch('/api/stage1');
    const payload = await response.json();
    currentStage1Data = payload.stage1 || [];
    renderStage1Table(currentStage1Data);
  } catch (e) {
    console.error('loadStage1 error:', e);
    stage1Table.innerHTML = '<tr class="empty-row"><td colspan="8">加载失败。</td></tr>';
  }
}

async function refreshStage1() {
  refreshStage1Btn.disabled = true;
  refreshStage1Btn.textContent = '刷新中...';
  try {
    await fetch('/api/dashboard/refresh', { method: 'POST' });
    await loadStage1();
  } catch (e) {
    console.error(e);
  } finally {
    refreshStage1Btn.disabled = false;
    refreshStage1Btn.textContent = '刷新';
  }
}

async function loadStage1ByDate(dateStr) {
  if (!dateStr) {
    await loadStage1();
    return;
  }
  try {
    const response = await fetch(`/api/rankings/by-date?date=${encodeURIComponent(dateStr)}`);
    const payload = await response.json();
    const items = payload.stage1 || [];
    renderStage1Table(items);
  } catch (e) {
    stage1Table.innerHTML = '<tr class="empty-row"><td colspan="8">无该日历史数据。</td></tr>';
  }
}

function renderStage1Table(items) {
  if (!items || !items.length) {
    stage1Table.innerHTML = '<tr class="empty-row"><td colspan="8">暂无 Stage1 候选数据，请先运行筛选。</td></tr>';
    return;
  }
  stage1Table.innerHTML = items.map((item) => `
    <tr class="clickable" data-symbol="${item.symbol}">
      <td>${item.rank ?? '--'}</td>
      <td>${item.symbol}</td>
      <td>${escapeHtml(item.name || '')}</td>
      <td>${escapeHtml(item.stage || '')}</td>
      <td title="${escapeHtml(item.analysis || '')}">${escapeHtml(item.watch_reason || '')}</td>
      <td>${item.watch_score ?? '--'}</td>
      <td>${item.total_score ?? '--'}</td>
      <td>${item.close ?? '--'}</td>
    </tr>
  `).join('');

  stage1Table.querySelectorAll('tr[data-symbol]').forEach((row) => {
    row.addEventListener('click', () => openStockDetail(row.dataset.symbol, 'stage1'));
  });
}

async function loadRecommendations(options = {}) {
  if (options.force) {
    // Refresh cache first
    try { await fetch('/api/dashboard/refresh', { method: 'POST' }); } catch (e) {}
  }
  refreshRecsBtn.disabled = true;
  recsLoading.classList.remove('hidden');
  try {
    const response = await fetch('/api/recommendations');
    const payload = await response.json();
    const items = payload.recommendations || [];
    renderRecommendations(items);
  } catch (e) {
    console.error('loadRecommendations error:', e);
  } finally {
    refreshRecsBtn.disabled = false;
    recsLoading.classList.add('hidden');
  }
}

function renderRecommendations(items) {
  const sItems = items.filter(i => i.rec_level === 'S');
  const aItems = items.filter(i => i.rec_level === 'A');
  const bpItems = items.filter(i => i.rec_level === 'B+');
  const bItems = items.filter(i => i.rec_level === 'B');

  recSSection.classList.toggle('hidden', !sItems.length);
  recASection.classList.toggle('hidden', !aItems.length);
  recBpSection.classList.toggle('hidden', !bpItems.length);
  recBSection.classList.toggle('hidden', !bItems.length);

  // S级
  if (sItems.length) {
    recSTable.innerHTML = sItems.map(i => {
      const wb = i.w_bottom || {};
      return `<tr class="clickable" data-symbol="${i.symbol}">
        <td>${i.rank}</td>
        <td>${i.symbol}</td>
        <td>${escapeHtml(i.name || '')}</td>
        <td>${i.weinstein_score || '--'}</td>
        <td title="${escapeHtml(i.analysis || '')}">${escapeHtml(i.rec_reason || '')}</td>
        <td>${wb.pattern_score || '--'}</td>
        <td>${wb.neckline || '--'}</td>
        <td>${i.close || '--'}</td>
        <td>${i.stop_loss || '--'}</td>
      </tr>`;
    }).join('');
    recSTable.querySelectorAll('tr[data-symbol]').forEach(r => {
      r.addEventListener('click', () => openStockDetail(r.dataset.symbol, 'recs'));
    });
  }

  // A级
  if (aItems.length) {
    recATable.innerHTML = aItems.map(i => {
      return `<tr class="clickable" data-symbol="${i.symbol}">
        <td>${i.rank}</td>
        <td>${i.symbol}</td>
        <td>${escapeHtml(i.name || '')}</td>
        <td>${i.weinstein_score || '--'}</td>
        <td title="${escapeHtml(i.analysis || '')}">${escapeHtml(i.rec_reason || '')}</td>
        <td>${i.close || '--'}</td>
        <td>${i.stop_loss || '--'}</td>
      </tr>`;
    }).join('');
    recATable.querySelectorAll('tr[data-symbol]').forEach(r => {
      r.addEventListener('click', () => openStockDetail(r.dataset.symbol, 'recs'));
    });
  }

  // B+级 提前埋伏
  if (bpItems.length) {
    recBpTable.innerHTML = bpItems.map(i => {
      const wb = i.w_bottom || {};
      const neck = parseFloat(wb.neckline) || 0;
      const close = parseFloat(i.close) || 0;
      const distPct = neck > 0 ? ((close / neck - 1) * 100).toFixed(1) : '--';
      return `<tr class="clickable" data-symbol="${i.symbol}">
        <td>${i.rank}</td>
        <td>${i.symbol}</td>
        <td>${escapeHtml(i.name || '')}</td>
        <td>${i.weinstein_score || '--'}</td>
        <td title="${escapeHtml(i.analysis || '')}">${escapeHtml(i.rec_reason || '')}</td>
        <td>${distPct}%</td>
        <td>${wb.pattern_score || '--'}</td>
        <td>${i.close || '--'}</td>
        <td>${i.stop_loss || '--'}</td>
      </tr>`;
    }).join('');
    recBpTable.querySelectorAll('tr[data-symbol]').forEach(r => {
      r.addEventListener('click', () => openStockDetail(r.dataset.symbol, 'recs'));
    });
  }

  // B级
  if (bItems.length) {
    recBTable.innerHTML = bItems.map(i => {
      const hasWb = i.has_w_bottom;
      return `<tr class="clickable" data-symbol="${i.symbol}">
        <td>${i.rank}</td>
        <td>${i.symbol}</td>
        <td>${escapeHtml(i.name || '')}</td>
        <td>${i.weinstein_score || '--'}</td>
        <td title="${escapeHtml(i.analysis || '')}">${escapeHtml(i.rec_reason || '')}</td>
        <td>${hasWb ? 'W底' : '--'}</td>
        <td>${i.close || '--'}</td>
        <td>${i.stop_loss || '--'}</td>
      </tr>`;
    }).join('');
    recBTable.querySelectorAll('tr[data-symbol]').forEach(r => {
      r.addEventListener('click', () => openStockDetail(r.dataset.symbol, 'recs'));
    });
  }
}

function showSystemLogModal() {
  renderSystemLog(latestUpdateStatus || {});
  systemLogModal.classList.remove('hidden');
}

function hideSystemLogModal() {
  systemLogModal.classList.add('hidden');
}

function renderSystemLog(status) {
  const ok = status.level === 'ok' && status.success;
  const tagText = ok ? '成功' : status.skipped_non_trading_day ? '已跳过' : '失败/提醒';
  systemLogStatusTag.textContent = tagText;
  systemLogStatusTag.className = `tag ${ok ? 'ok' : 'warn'}`;
  systemLogTitle.textContent = status.title || '增量更新系统日志';
  systemLogSubtitle.textContent = buildSystemLogSubtitle(status);

  const metrics = [
    { label: '最近完成时间', value: formatDateTime(status.finished_at) },
    { label: '最新交易日', value: status.latest_trade_date || '--' },
    { label: '股票更新数', value: formatCount(status.stock_symbols_updated) },
    { label: '新增K线行数', value: formatCount(status.stock_rows_added) },
    { label: '指数是否更新', value: formatBoolean(status.index_updated) },
    { label: '指数新增行数', value: formatCount(status.index_rows_added) },
    { label: 'Phase1是否执行', value: formatBoolean(status.phase1_ran) },
    { label: '候选数', value: formatCount(status.phase1_candidate_count) },
    { label: 'Stage1榜单', value: formatCount(status.phase1_stage1_count) },
    { label: 'Stage2榜单', value: formatCount(status.phase1_stage2_count) },
    { label: '总耗时', value: formatSeconds(status.total_runtime_seconds) },
    { label: 'Phase1跳过原因', value: status.phase1_skipped_reason || '--' },
  ];
  systemLogGrid.innerHTML = metrics.map((item) => `
    <div class="metric-card">
      <div class="label">${item.label}</div>
      <div class="value">${escapeHtml(item.value || '--')}</div>
    </div>
  `).join('');

  const messageLines = [status.message || '--'];
  if (status.error) {
    messageLines.push(`错误信息：${status.error}`);
  }
  systemLogMessage.innerHTML = renderMarkdown(messageLines.map((line) => `- ${line}`).join('\n'));
  systemLogRaw.textContent = status.raw_payload_text || '暂无原始状态。';
}

function buildSystemLogSubtitle(status) {
  if (status.skipped_non_trading_day) {
    return '本次任务已识别为非交易日，因此自动跳过执行。';
  }
  if (status.success && status.phase1_ran) {
    return '本次增量更新成功，并已自动重跑排行榜。';
  }
  if (status.success) {
    return '本次增量更新成功，但未触发排行榜重算。';
  }
  return '最近一次增量更新存在失败或异常，请查看下方详情。';
}

function renderRankingTable(container, items, mode) {
  if (!items.length) {
    container.innerHTML = '<tr class="empty-row"><td colspan="9">暂无数据，请先运行筛选。</td></tr>';
    return;
  }
  container.innerHTML = items.map((item) => `
    <tr class="clickable" data-symbol="${item.symbol}">
      <td>${item.rank ?? '--'}</td>
      <td>${item.symbol}</td>
      <td>${escapeHtml(item.name || '')}</td>
      <td>${escapeHtml(item.stage || '')}</td>
      <td title="${escapeHtml(item.analysis || '')}">${escapeHtml(item.watch_reason || '')}</td>
      <td>${item.final_score ?? '--'}</td>
      <td>${item.target_entry_price ?? '--'}</td>
      <td>${item.stop_loss_reference ?? '--'}</td>
      <td>${item.close ?? '--'}</td>
    </tr>
  `).join('');

  container.querySelectorAll('tr[data-symbol]').forEach((row) => {
    row.addEventListener('click', () => openStockDetail(row.dataset.symbol, mode));
  });
}

function renderQuasiStage2Table(container, items) {
  if (!items.length) {
    container.innerHTML = '<tr class="empty-row"><td colspan="7">暂无准Stage2 候选。</td></tr>';
    return;
  }
  container.innerHTML = items.map((item) => `
    <tr class="clickable" data-symbol="${item.symbol}">
      <td>${item.rank ?? '--'}</td>
      <td>${item.symbol}</td>
      <td>${escapeHtml(item.name || '')}</td>
      <td>${escapeHtml(item.stage || '')}</td>
      <td>${item.final_score ?? '--'}</td>
      <td>${item.close ?? '--'}</td>
      <td>${escapeHtml(item.missing_gates || '')}</td>
    </tr>
  `).join('');

  container.querySelectorAll('tr[data-symbol]').forEach((row) => {
    row.addEventListener('click', () => openStockDetail(row.dataset.symbol, 'quasi-stage2'));
  });
} 

async function runSearch(query) {
  const response = await fetch(`/api/search?q=${encodeURIComponent(query || '')}`);
  const payload = await response.json();
  const items = payload.items || [];
  if (!items.length) {
    searchTable.innerHTML = '<tr class="empty-row"><td colspan="5">未找到匹配股票。</td></tr>';
    return;
  }

  searchTable.innerHTML = items.map((item) => `
    <tr class="clickable" data-symbol="${item.symbol}">
      <td>${item.symbol}</td>
      <td>${escapeHtml(item.name || '')}</td>
      <td>${renderTag(item.in_results, item.in_results ? '已覆盖' : '待补数')}</td>
      <td>${renderTag(item.in_stage1, item.in_stage1 ? '已上榜' : '未上榜')}</td>
      <td>${renderTag(item.in_stage2, item.in_stage2 ? '已上榜' : '未上榜')}</td>
    </tr>
  `).join('');

  searchTable.querySelectorAll('tr[data-symbol]').forEach((row) => {
    row.addEventListener('click', () => openStockDetail(row.dataset.symbol, 'search'));
  });
}

function renderTag(ok, text) {
  const cls = ok ? 'tag ok' : 'tag warn';
  return `<span class="${cls}">${text}</span>`;
}

function formatSeconds(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return '--';
  }
  return `${Number(value).toFixed(2)} 秒`;
}

function formatDateTime(value) {
  if (!value) {
    return '--';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString('zh-CN', { hour12: false });
}

function formatCount(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return '--';
  }
  return `${Number(value)}`;
}

function formatPercentValue(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return '--';
  }
  return `${Number(value).toFixed(2)}%`;
}

function formatEntryMode(value) {
  if (value === 'breakout') {
    return '突破买点';
  }
  return value || '--';
}

function formatBoolean(value) {
  if (value === null || value === undefined || value === '') {
    return '--';
  }
  return value ? '是' : '否';
}

async function openStockDetail(symbol) {
  const requestId = ++currentDetailRequestId;
  modalTitle.textContent = '加载中...';
  modalSymbol.textContent = symbol;
  setAnalysisLoading(true);
  renderAnalysisMarkdown('> LLM 正在生成分析，请稍候...');
  metricGrid.innerHTML = '';
  stage1Pill.textContent = 'Stage1: --';
  stage2Pill.textContent = 'Stage2: --';
  hideTooltip();
  showModal();
  detailModal.querySelector('.modal-card').scrollTop = 0;
  detailModal.querySelector('.chart-panel').scrollTop = 0;
  detailModal.querySelector('.analysis-panel').scrollTop = 0;

  try {
    const response = await fetch(`/api/stock/${encodeURIComponent(symbol)}`);
    const payload = await response.json();
    if (!response.ok || payload.error) {
      throw new Error(payload.error || '加载失败');
    }
    if (requestId !== currentDetailRequestId) {
      return;
    }
    modalTitle.textContent = `${payload.name || ''} ${payload.symbol}`.trim();
    modalSymbol.textContent = payload.symbol;
    stage1Pill.textContent = payload.stage1_rank ? `Stage1: 第 ${payload.stage1_rank} 名` : 'Stage1: 未上榜';
    stage2Pill.textContent = payload.stage2_rank ? `Stage2: 第 ${payload.stage2_rank} 名` : 'Stage2: 未上榜';
    renderMetrics(payload.metrics || []);
    drawCandles(chartCanvas, payload.chart || {});
    if (payload.analysis) {
      renderAnalysisMarkdown(payload.analysis);
      setAnalysisLoading(false);
    } else {
      loadStockAnalysis(payload.symbol, requestId);
    }
  } catch (error) {
    if (requestId !== currentDetailRequestId) {
      return;
    }
    modalTitle.textContent = symbol;
    setAnalysisLoading(false);
    renderAnalysisMarkdown(`> ${error.message || '加载失败'}`);
    drawCandles(chartCanvas, {});
  }
}

async function loadStockAnalysis(symbol, requestId) {
  try {
    const response = await fetch(`/api/stock/${encodeURIComponent(symbol)}/analysis`);
    const payload = await response.json();
    if (!response.ok || payload.error) {
      throw new Error(payload.error || '分析生成失败');
    }
    if (requestId !== currentDetailRequestId) {
      return;
    }
    renderAnalysisMarkdown(payload.analysis || '--');
  } catch (error) {
    if (requestId !== currentDetailRequestId) {
      return;
    }
    renderAnalysisMarkdown(`> ${error.message || '分析生成失败'}`);
  } finally {
    if (requestId === currentDetailRequestId) {
      setAnalysisLoading(false);
    }
  }
}

function renderMetrics(metrics) {
  metricGrid.innerHTML = metrics.map((item) => `
    <div class="metric-card">
      <div class="label">${escapeHtml(item.label || '')}</div>
      <div class="value">${escapeHtml(item.value || '--')}</div>
    </div>
  `).join('');
}

function showModal() {
  detailModal.classList.remove('hidden');
}

function hideModal() {
  currentDetailRequestId += 1;
  detailModal.classList.add('hidden');
  hideTooltip();
}

function setAnalysisLoading(isLoading) {
  analysisLoading.classList.toggle('hidden', !isLoading);
}

function renderAnalysisMarkdown(markdown) {
  analysisMarkdown.innerHTML = renderMarkdown(markdown || '--');
}

function renderMarkdown(markdown) {
  const source = String(markdown || '').replace(/\r\n/g, '\n');
  const lines = source.split('\n');
  const blocks = [];
  let paragraph = [];
  let listItems = [];
  let listType = '';

  const flushParagraph = () => {
    if (!paragraph.length) {
      return;
    }
    blocks.push(`<p>${formatInlineMarkdown(paragraph.join('\n')).replace(/\n/g, '<br>')}</p>`);
    paragraph = [];
  };

  const flushList = () => {
    if (!listItems.length || !listType) {
      return;
    }
    const items = listItems.map((item) => `<li>${formatInlineMarkdown(item)}</li>`).join('');
    blocks.push(`<${listType}>${items}</${listType}>`);
    listItems = [];
    listType = '';
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
      if (listType && listType !== 'ul') {
        flushList();
      }
      listType = 'ul';
      listItems.push(unorderedMatch[1]);
      return;
    }

    const orderedMatch = line.match(/^\d+\.\s+(.*)$/);
    if (orderedMatch) {
      flushParagraph();
      if (listType && listType !== 'ol') {
        flushList();
      }
      listType = 'ol';
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
  return blocks.join('') || '<p>--</p>';
}

function formatInlineMarkdown(text) {
  return escapeHtml(text)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>');
}

function drawCandles(canvas, chart) {
  const ctx = canvas.getContext('2d');
  const width = canvas.width;
  const height = canvas.height;
  ctx.clearRect(0, 0, width, height);

  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, width, height);

  const candles = chart.candles || [];

  if (!candles.length) {
    currentChartState = null;
    ctx.fillStyle = '#64748b';
    ctx.font = '18px Microsoft YaHei';
    ctx.fillText('暂无K线数据', width / 2 - 56, height / 2);
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
  const candleWidth = Math.max(3, (right - left) / candles.length * 0.6);

  const priceSeries = candles.flatMap((item) => [item.high, item.low, item.ma144, item.ma169]).filter((value) => Number.isFinite(value));
  if (Number.isFinite(chart.breakout_line)) {
    priceSeries.push(chart.breakout_line);
  }
  if (Number.isFinite(chart.resistance_line)) {
    priceSeries.push(chart.resistance_line);
  }
  const maxHigh = Math.max(...priceSeries);
  const minLow = Math.min(...priceSeries);
  const paddedRange = Math.max((maxHigh - minLow) * 0.08, maxHigh * 0.015, 0.5);
  const axisHigh = maxHigh + paddedRange;
  const axisLow = Math.max(minLow - paddedRange, 0);
  const maxVolume = Math.max(...candles.map((item) => item.volume || 0), 1);
  const scaleY = (value) => bottom - ((value - axisLow) / Math.max(axisHigh - axisLow, 0.0001)) * priceAreaHeight;
  const scaleX = (index) => left + ((right - left) / Math.max(candles.length - 1, 1)) * index;

  drawGrid(ctx, left, right, priceAreaTop, bottom, 5, '#e2e8f0');
  drawGrid(ctx, left, right, volumeAreaTop, volumeBottom, 2, '#edf2f7');
  drawHorizontalLine(ctx, chart.breakout_line, scaleY, left, right, '#7c3aed', [6, 5], '突破线');
  drawHorizontalLine(ctx, chart.resistance_line, scaleY, left, right, '#ea580c', [10, 6], '压力线');
  drawLine(ctx, candles, 'ma144', '#2563eb', scaleX, scaleY);
  drawLine(ctx, candles, 'ma169', '#d97706', scaleX, scaleY);

  candles.forEach((item, index) => {
    const x = scaleX(index);
    const openY = scaleY(item.open);
    const closeY = scaleY(item.close);
    const highY = scaleY(item.high);
    const lowY = scaleY(item.low);
    const color = item.close >= item.open ? '#16a34a' : '#dc2626';

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

  currentChartState = { candles, chart, scaleX, scaleY, left, right, priceAreaTop, bottom, candleWidth };
  bindChartHover();
  drawAxisLabels(ctx, candles, left, right, bottom, axisLow, axisHigh, scaleX, scaleY, priceAreaTop, right);
  drawLegend(ctx);
}

function drawGrid(ctx, left, right, top, bottom, rows, color) {
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  for (let i = 0; i <= rows; i += 1) {
    const y = top + ((bottom - top) / rows) * i;
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(right, y);
    ctx.stroke();
  }
}

function drawLine(ctx, candles, key, color, scaleX, scaleY) {
  const points = candles
    .map((item, index) => ({ x: scaleX(index), y: item[key] }))
    .filter((item) => Number.isFinite(item.y));
  if (points.length < 2) {
    return;
  }
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  points.forEach((point, index) => {
    const y = scaleY(point.y);
    if (index === 0) {
      ctx.moveTo(point.x, y);
    } else {
      ctx.lineTo(point.x, y);
    }
  });
  ctx.stroke();
}

function drawHorizontalLine(ctx, value, scaleY, left, right, color, dash, label) {
  if (!Number.isFinite(value)) {
    return;
  }
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
  ctx.font = '12px Microsoft YaHei';
  ctx.fillText(`${label} ${value.toFixed(2)}`, right + 10, y + 4);
  ctx.restore();
}

function drawAxisLabels(ctx, candles, left, right, bottom, minLow, maxHigh, scaleX, scaleY, top) {
  ctx.fillStyle = '#64748b';
  ctx.font = '12px Microsoft YaHei';

  const priceTicks = 6;
  for (let i = 0; i <= priceTicks; i += 1) {
    const value = minLow + ((maxHigh - minLow) / priceTicks) * i;
    const y = scaleY(value);
    ctx.fillText(value.toFixed(2), 16, y + 4);
  }

  const labelCount = 5;
  for (let i = 0; i < labelCount; i += 1) {
    const index = Math.floor((candles.length - 1) * (i / Math.max(labelCount - 1, 1)));
    const x = scaleX(index);
    const text = candles[index]?.date || '';
    ctx.fillText(text, x - 24, bottom + 24);
  }

  ctx.fillText('价格', 18, top - 10);
  ctx.fillText('成交量', 18, bottom + 56);
}

function drawLegend(ctx) {
  ctx.fillStyle = '#334155';
  ctx.font = '12px Microsoft YaHei';
  ctx.fillText('MA144', 74, 20);
  ctx.fillText('MA169', 154, 20);
  ctx.fillStyle = '#2563eb';
  ctx.fillRect(40, 10, 20, 3);
  ctx.fillStyle = '#d97706';
  ctx.fillRect(120, 10, 20, 3);
}

function bindChartHover() {
  chartCanvas.onmousemove = handleChartHover;
  chartCanvas.onmouseleave = () => {
    hideTooltip();
    if (currentChartState) {
      drawCandles(chartCanvas, currentChartState.chart);
    }
  };
}

function handleChartHover(event) {
  if (!currentChartState) {
    hideTooltip();
    return;
  }

  const rect = chartCanvas.getBoundingClientRect();
  const scaleXFactor = chartCanvas.width / rect.width;
  const scaleYFactor = chartCanvas.height / rect.height;
  const x = (event.clientX - rect.left) * scaleXFactor;
  const { candles, left, right, scaleX, chart } = currentChartState;

  if (x < left || x > right) {
    hideTooltip();
    drawCandles(chartCanvas, chart);
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

  drawCandles(chartCanvas, chart);
  highlightCandle(closestIndex);
  showTooltip(
    candles[closestIndex],
    event.clientX - rect.left,
    event.clientY - rect.top,
    rect.width,
    rect.height,
    chartCanvas.parentElement,
  );
}

function highlightCandle(index) {
  if (!currentChartState) {
    return;
  }
  const { scaleX, candles, priceAreaTop, bottom } = currentChartState;
  const ctx = chartCanvas.getContext('2d');
  const x = scaleX(index);
  ctx.save();
  ctx.strokeStyle = 'rgba(37, 99, 235, 0.55)';
  ctx.lineWidth = 1;
  ctx.setLineDash([5, 4]);
  ctx.beginPath();
  ctx.moveTo(x, priceAreaTop);
  ctx.lineTo(x, bottom);
  ctx.stroke();
  ctx.setLineDash([]);
  const candle = candles[index];
  const priceY = currentChartState.scaleY(candle.close);
  ctx.fillStyle = '#2563eb';
  ctx.beginPath();
  ctx.arc(x, priceY, 4, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
}

function showTooltip(candle, x, y, width, height, container) {
  const change = Number.isFinite(candle.open) && Number.isFinite(candle.close)
    ? (((candle.close - candle.open) / Math.max(candle.open, 0.0001)) * 100).toFixed(2)
    : '--';
  chartTooltip.innerHTML = [
    `<div><strong>日期</strong>${escapeHtml(candle.date || '--')}</div>`,
    `<div><strong>开盘</strong>${formatChartNumber(candle.open)}</div>`,
    `<div><strong>最高</strong>${formatChartNumber(candle.high)}</div>`,
    `<div><strong>最低</strong>${formatChartNumber(candle.low)}</div>`,
    `<div><strong>收盘</strong>${formatChartNumber(candle.close)}</div>`,
    `<div><strong>涨跌</strong>${change === '--' ? '--' : `${change}%`}</div>`,
    `<div><strong>成交量</strong>${formatChartVolume(candle.volume)}</div>`,
    `<div><strong>MA144</strong>${formatChartNumber(candle.ma144)}</div>`,
    `<div><strong>MA169</strong>${formatChartNumber(candle.ma169)}</div>`,
  ].join('');

  chartTooltip.classList.remove('hidden');
  const tooltipWidth = 220;
  const tooltipHeight = 210;
  const offsetLeft = container ? chartCanvas.offsetLeft : 0;
  const offsetTop = container ? chartCanvas.offsetTop : 0;
  const left = Math.min(x + offsetLeft + 18, offsetLeft + width - tooltipWidth);
  const top = Math.min(y + offsetTop + 18, offsetTop + height - tooltipHeight);
  chartTooltip.style.left = `${Math.max(offsetLeft + 12, left)}px`;
  chartTooltip.style.top = `${Math.max(offsetTop + 12, top)}px`;
}

function hideTooltip() {
  chartTooltip.classList.add('hidden');
}

function formatChartNumber(value) {
  return Number.isFinite(value) ? value.toFixed(2) : '--';
}

function formatChartVolume(value) {
  if (!Number.isFinite(value)) {
    return '--';
  }
  if (value >= 100000000) {
    return `${(value / 100000000).toFixed(2)}亿`;
  }
  if (value >= 10000) {
    return `${(value / 10000).toFixed(2)}万`;
  }
  return value.toFixed(0);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

boot();
