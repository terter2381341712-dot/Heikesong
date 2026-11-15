import { locale, t } from './i18n.js';
import { performancePanel, jobPerformance } from './performance.js';
const statusInfo = {
  QUEUED: ['排队中', 'Queued'], WAITING_FOR_RESOURCES: ['等待资源', 'Waiting for resources', 'warn'], DISPATCHING: ['分派对账中', 'Reconciling dispatch', 'active'],
  RUNNING: ['执行中', 'Running', 'active'], CANCELLING: ['取消确认中', 'Cancelling', 'warn'], CANCEL_REQUESTED: ['已请求取消', 'Cancellation requested', 'warn'],
  SUCCEEDED: ['执行成功', 'Succeeded', 'good'], PARTIAL_FAILED: ['部分失败', 'Partial failure', 'warn'], FAILED: ['执行失败', 'Failed', 'bad'], CANCELLED: ['已取消', 'Cancelled'],
  REJECTED: ['分派拒绝', 'Dispatch rejected', 'bad'], ACCEPTED: ['已受理', 'Accepted', 'active'], ONLINE: ['在线', 'Online', 'good'], OFFLINE: ['离线', 'Offline', 'bad'],
  ACTIVE: ['允许接单', 'Accepting jobs', 'good'], DRAINING: ['排空中', 'Draining', 'warn'], PAUSED: ['已暂停', 'Paused'],
  UNAVAILABLE: ['不兼容', 'Incompatible'], NOT_PREPARED: ['未准备', 'Not prepared'], PREPARING: ['准备中', 'Preparing', 'warn'], READY: ['已就绪', 'Ready', 'good'], ERROR: ['准备失败', 'Preparation failed', 'bad'],
};

export const terminal = new Set(['SUCCEEDED', 'PARTIAL_FAILED', 'FAILED', 'CANCELLED']);
const languageLabels = { auto: ['自动识别', 'Auto-detect'], zh: ['中文', 'Chinese'], en: ['英语', 'English'] };
const typeLabels = { 'ocr.image.v1': ['图片文字识别', 'Image OCR'], 'asr.file.v1': ['短录音文字识别', 'Short audio transcription'] };
const dateOptions = {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
};
const beijing = new Intl.DateTimeFormat('zh-CN', dateOptions);
const beijingEn = new Intl.DateTimeFormat('en-GB', dateOptions);
const renderedHtml = new WeakMap();

export const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})[char]);
export const typeName = (type, fallback = type) => Object.hasOwn(typeLabels, type) ? t(...typeLabels[type]) : fallback;
export const languageName = (language) => Object.hasOwn(languageLabels, language) ? t(...languageLabels[language]) : language;
export const isConfigured = (type) => type?.configuration_state === 'CONFIGURED' && Boolean(type.profile_digest);
export const tag = (status) => {
  const [chinese, english, tone = ''] = Object.hasOwn(statusInfo, status) ? statusInfo[status] : [status, status];
  return `<span class="tag ${tone}" title="${escape(status)}">${escape(t(chinese, english))}</span>`;
};
export const date = (value) => value ? `${(locale === 'en' ? beijingEn : beijing).format(new Date(value))} ${t('北京时间', 'Beijing time')}` : 'N/A';
export const bytes = (value) => {
  if (value == null) return 'N/A';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  const exponent = value ? Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024))) : 0;
  return `${Number((value / (1024 ** exponent)).toFixed(exponent ? 1 : 0))} ${units[exponent]}`;
};

export function updateHtml(element, html, fallbackFocus) {
  if (renderedHtml.get(element) === html) return;
  const details = new Set([...element.querySelectorAll('details[open][data-detail]')].map(({ dataset }) => dataset.detail));
  const focus = element.contains(document.activeElement) ? document.activeElement.dataset.focusKey : null;
  element.innerHTML = html;
  renderedHtml.set(element, html);
  for (const item of element.querySelectorAll('details[data-detail]')) item.open = details.has(item.dataset.detail);
  if (focus) {
    element.querySelector(`[data-focus-key="${CSS.escape(focus)}"]`)?.focus({ preventScroll: true });
    if (!element.contains(document.activeElement)) fallbackFocus?.focus({ preventScroll: true });
  }
}

export function message(text, tone = '') {
  return text ? `<p class="notice ${tone}">${escape(text)}</p>` : '';
}

export function empty(title, text) {
  return `<div class="empty"><h3>${escape(title)}</h3><p>${escape(text)}</p></div>`;
}

export function historyRows(entries, current, showResults) {
  if (!entries.length) return `<p class="muted">${t('此浏览器暂无任务记录。', 'No jobs in this browser yet.')}</p>`;
  const suffix = showResults ? '/results' : '';
  return entries.slice(0, 25).map((entry) => `
    <a class="recent-item" href="#jobs/${escape(entry.id)}${suffix}" data-focus-key="job-${escape(entry.id)}"${entry.id === current ? ' aria-current="true"' : ''}>
      <strong>${escape(entry.type ? typeName(entry.type) : t('任务详情', 'Job details'))}</strong>
      <small class="mono">${escape(entry.id)}</small>
      ${entry.status ? tag(entry.status) : ''}
    </a>`).join('');
}

function issue(error) {
  if (!error) return '';
  const request = error.request_id ? t(`（请求 ${error.request_id}）`, ` (request ${error.request_id})`) : '';
  return message(`${error.code} · ${error.message}${request}`, 'error');
}

function sharedDeviceStopped(dispatch) {
  return dispatch.status === 'CANCELLED' && dispatch.error?.code === 'NODE_UNAVAILABLE' &&
    dispatch.error.retryable && dispatch.error.details?.execution_id === dispatch.execution_id;
}

function asTimestamp(ms) {
  const minutes = String(Math.floor(ms / 60000)).padStart(2, '0');
  const seconds = String(Math.floor(ms / 1000) % 60).padStart(2, '0');
  return `${minutes}:${seconds}.${String(ms % 1000).padStart(3, '0')}`;
}

function output(result, id) {
  if (!result) return '';
  const text = `<p class="result-text">${escape(result.text || t('（结果为空）', '(No text recognized)'))}</p>`;
  if (result.task_type === 'asr.file.v1') {
    const segments = result.segments.map((segment) => `<li><span class="mono">${asTimestamp(segment.start_ms)} – ${asTimestamp(segment.end_ms)}</span><span>${escape(segment.text)}</span></li>`).join('');
    return `${text}<p class="task-meta">${asTimestamp(result.duration_ms)} · ${escape(languageName(result.language) ?? 'N/A')}</p>${segments ? `<ul class="segments" aria-label="${t('录音时间戳', 'Audio timestamps')}">${segments}</ul>` : ''}`;
  }
  const blocks = result.blocks.map((block) => `<li><span class="mono">[${escape(block.bbox.join(', '))}] · ${t('置信度', 'Confidence')} ${escape(block.confidence ?? 'N/A')}</span><span>${escape(block.text)}</span></li>`).join('');
  return `${text}<p class="task-meta">${escape(result.image_width)} × ${escape(result.image_height)} px</p>${blocks ? `<details data-detail="ocr-${escape(id)}"><summary data-focus-key="ocr-${escape(id)}">${t('文字块与位置', 'Text blocks and positions')} (${result.blocks.length})</summary><ul class="segments">${blocks}</ul></details>` : ''}`;
}

const countLabels = {
  succeeded: ['成功', 'Succeeded'], running: ['执行', 'Running'], failed: ['失败', 'Failed'], cancelled: ['取消', 'Cancelled'], dispatching: ['对账', 'Reconciling'],
  waiting_for_resources: ['等待资源', 'Waiting for resources'], queued: ['排队', 'Queued'], cancelling: ['取消中', 'Cancelling'],
};

function renderCounts(counts) {
  const completed = counts.succeeded + counts.failed + counts.cancelled;
  const bars = ['succeeded', 'failed', 'cancelled', 'running'].map((status) => {
    const share = counts.total ? counts[status] / counts.total * 100 : 0;
    return `<span class="count-segment ${status}" data-share="${share}"></span>`;
  }).join('');
  const labels = Object.entries(countLabels).map(([status, label]) => `<span>${label} ${escape(counts[status])}</span>`).join('');
  return `<div class="count-heading"><strong>${t('输入处理', 'Input processing')}</strong><span>${escape(completed)} / ${escape(counts.total)} ${t('项已结束', 'complete')}</span></div>
    <div class="count-bar" aria-hidden="true">${bars}</div><div class="count-labels">${labels}</div>`;
}

function taskRow(task, result, resourceNames, showResults) {
  const id = escape(task.task_id);
  const current = task.dispatches.find((dispatch) => dispatch.execution_id === task.current_execution_id);
  const worker = current ?? (terminal.has(task.status) ? task.dispatches.at(-1) : null);
  const unassigned = task.current_execution_id ? t('分派待确认', 'Dispatch unconfirmed') : terminal.has(task.status) ? t('未分派', 'Unassigned') : t('待分派', 'Awaiting dispatch');
  const name = worker ? resourceNames.get(worker.worker_id) || worker.worker_id : unassigned;
  const stopped = task.dispatches.findLast(sharedDeviceStopped);
  const resumed = task.status === 'WAITING_FOR_RESOURCES' || (worker && worker.execution_id !== stopped?.execution_id);
  const handoff = stopped && resumed && t(`原分派中止 · ${stopped.error.message}`, `Previous dispatch stopped · ${stopped.error.message}`);
  const dispatches = showResults ? '' : task.dispatches.map((dispatch) => `
    <li><span class="mono">${escape(resourceNames.get(dispatch.worker_id) || dispatch.worker_id)}</span><span>${t('尝试', 'Attempt')} ${escape(dispatch.attempt_no)} ${tag(dispatch.status)} <code>${escape(dispatch.execution_id)}</code></span></li>
    ${dispatch.error ? `<li>${issue(dispatch.error)}</li>` : ''}`).join('');
  const artifact = task.input;
  return `<li class="task-row">
    <div class="task-header"><h4>${escape(task.input_index + 1)}. ${escape(artifact.original_name)}</h4>${tag(task.status)}</div>
    <p class="task-meta"><span>${escape(name)}</span><span>${t('已受理', 'Accepted attempts:')} ${escape(task.attempts_used)}${t(' 次', '')}</span><span>${bytes(artifact.size_bytes)}</span></p>
    ${message(handoff && task.waiting_reason !== stopped.error.message ? handoff : '', 'warn')}
    ${message(task.waiting_reason, 'warn')}${issue(result?.error ?? task.error)}
    ${output(result?.result, task.task_id)}
    ${dispatches ? `<details data-detail="dispatch-${id}"><summary data-focus-key="dispatch-${id}">${t('分派记录', 'Dispatch history')} (${task.dispatches.length})</summary><ul class="segments">${dispatches}</ul></details>` : ''}
    <button class="button subtle" type="button" data-action="download-input" data-id="${id}" data-focus-key="input-${id}">${t('下载原文件', 'Download original')}</button>
  </li>`;
}

export function jobPanel(job, results, resourceNames, { showResults, jobError, resultsError, cancelling, downloading, resources = [], performance, performanceError }) {
  const id = escape(job.job_id);
  const orderedInputs = [...job.tasks].sort((left, right) => left.input_index - right.input_index);
  const items = new Map((showResults ? results?.items ?? [] : []).map((result) => [result.task_id, result]));
  const finished = terminal.has(job.status);
  const canDownload = showResults && finished && job.download_path;
  const canCancel = !showResults && !finished && !job.cancel_requested;
  const needsP1 = job.dependency_status === 'UNREACHABLE';
  const switchPath = `#jobs/${id}${showResults ? '' : '/results'}`;
  const switchLabel = showResults ? t('查看任务进度', 'View job progress') : t('查看识别结果', 'View results');
  let resultNotice = '';
  if (showResults) {
    if (resultsError) resultNotice = message(`${results ? t('结果快照已过期', 'Result snapshot is out of date') : t('结果读取失败', 'Could not load results')} · ${resultsError}`, 'error');
    else if (!results) resultNotice = message(t('正在读取识别结果…', 'Loading results…'));
    else if (!finished && !results.items.some((item) => item.result)) resultNotice = message(t('识别完成后可预览结果。', 'Results will be available after recognition finishes.'));
  }
  return `<section class="panel compact job-summary">
    <div class="panel-heading"><h3>${escape(typeName(job.task_type))}</h3>${tag(job.status)}</div>
    <p class="mono">Job ID <span id="job-id-text">${id}</span></p><p class="task-meta">${t('创建于', 'Created')} ${escape(date(job.created_at))} · ${escape(languageName(job.parameters.language))}</p>
    ${message(jobError && t(`快照已过期 · ${jobError}`, `Snapshot is out of date · ${jobError}`), 'error')}
    ${message(needsP1 ? t('P2 报告 P1 资源服务不可达，等待恢复和对账。', 'P2 reports that P1 is unreachable. Waiting for recovery and reconciliation.') : '', 'warn')}
    ${message(job.status === 'CANCELLING' ? t('正在等待活动任务确认停止。', 'Waiting for active tasks to confirm cancellation.') : '', 'warn')}
    ${renderCounts(job.counts)}
    <div class="summary-actions">
      <button class="button secondary" type="button" data-action="copy-id" data-focus-key="copy-id">${t('复制 Job ID', 'Copy Job ID')}</button>
      <a class="button secondary" href="${switchPath}" data-focus-key="switch-job-view">${switchLabel}</a>
      ${canDownload ? `<button class="button primary" type="button" data-action="download-job" data-focus-key="download-job"${downloading || jobError ? ' disabled' : ''}><span data-icon="download"></span>${downloading ? t('下载中…', 'Downloading…') : t('下载 ZIP', 'Download ZIP')}</button>` : ''}
      ${canCancel ? `<button class="button danger" type="button" data-action="cancel-job" data-focus-key="cancel-job"${cancelling || jobError ? ' disabled' : ''}>${cancelling ? t('请求中…', 'Requesting…') : t('取消任务', 'Cancel job')}</button>` : ''}
    </div>
  </section>
  ${!showResults ? jobPerformance(job, resources, performance, performanceError) : ''}
  <section class="panel compact" aria-labelledby="job-inputs-title">
    <div class="panel-heading"><h3 id="job-inputs-title">${showResults ? t('逐文件结果', 'Results by file') : t('逐文件进度', 'Progress by file')}</h3></div>
    ${resultNotice}
    <ul class="task-list">${orderedInputs.map((task) => taskRow(task, items.get(task.task_id), resourceNames, showResults)).join('')}</ul>
    ${showResults && results?.combined_text ? `<details data-detail="combined"><summary data-focus-key="combined">${t('查看合并文本', 'View combined text')}</summary><p class="result-text">${escape(results.combined_text)}</p></details>` : ''}
  </section>`;
}

function percent(value) {
  return value == null ? 'N/A' : `${Number(value.toFixed(1))}%`;
}

function nodeFacts(node) {
  const { hardware, telemetry } = node;
  const facts = [
    [t('接单槽位', 'Job slots'), t(`${node.reserved_slots} 占用 / ${node.max_slots} 总量`, `${node.reserved_slots} of ${node.max_slots} in use`)],
    [t('空闲槽位', 'Available slots'), node.available_slots],
    ['CPU', [hardware.cpu_name, telemetry?.cpu_percent == null ? '' : percent(telemetry.cpu_percent)].filter(Boolean).join(' · ')],
    [t('内存', 'Memory'), hardware.ram_total_bytes == null ? '' : `${bytes(telemetry?.ram_used_bytes)} / ${bytes(hardware.ram_total_bytes)}`],
    ['GPU', [hardware.gpu_name, telemetry?.gpu_percent == null ? '' : percent(telemetry.gpu_percent), hardware.gpu_vram_bytes == null ? '' : bytes(hardware.gpu_vram_bytes)].filter(Boolean).join(' · ')],
    [t('系统', 'System'), [hardware.os, hardware.arch].filter(Boolean).join(' / ')],
  ];
  return facts.filter(([, value]) => value !== '').map(([label, value]) => `<div><dt>${escape(label)}</dt><dd>${escape(value)}</dd></div>`).join('');
}

export function resourceTile(node) {
  const id = escape(node.worker_id);
  const tone = node.connection !== 'ONLINE' ? 'offline' : node.mode === 'ACTIVE' ? 'active' : 'paused';
  return `<button type="button" class="resource-tile" data-device="${id}" data-tone="${tone}" data-focus-key="device-${id}" aria-controls="resource-detail" aria-expanded="false">
    <span class="device-model" aria-hidden="true">
      <span class="device-computer">
        <span class="device-back"></span><span class="device-side"></span>
        <span class="device-screen"><span class="device-display"><i></i><i></i><i></i><i></i></span></span>
        <span class="device-stand"></span><span class="device-base"></span>
      </span>
    </span>
    <span class="device-name">${escape(node.name)}</span>
    ${tag(node.connection === 'ONLINE' ? node.mode : node.connection)}
  </button>`;
}

export function resourceCard(node, { stale, changing, mock, pinned, performance, performanceError, performanceImporting }) {
  const id = escape(node.worker_id);
  const action = node.mode === 'ACTIVE' ? 'DRAINING' : 'ACTIVE';
  const label = action === 'DRAINING' ? (mock ? t('退出接单', 'Stop accepting jobs') : t('暂停接新任务', 'Pause new jobs')) : t('恢复接单', 'Resume accepting jobs');
  return `<article class="panel compact resource-card">
    <div class="panel-heading"><h3>${escape(node.name)}</h3>${pinned ? `<span class="device-pin">${t('已固定', 'Pinned')}</span>` : ''}</div>
    <div class="node-tags">${tag(node.connection)}${tag(node.mode)}</div>
    <dl class="node-facts">${nodeFacts(node)}</dl>
    ${performancePanel(node, performance, performanceError, performanceImporting, mock)}
    ${message(node.cleanup_pending ? t('正在等待设备确认停止。', 'Waiting for the device to confirm that work has stopped.') : '', 'warn')}
    ${node.capabilities.length ? `<ul class="device-capabilities">${node.capabilities.map((item) => `<li><strong>${escape(typeName(item.task_type))}</strong>${tag(item.state)}</li>`).join('')}</ul>` : ''}
    <div class="node-footer">
      <button class="button secondary" type="button" data-action="node-mode" data-id="${id}" data-mode="${action}" data-focus-key="mode-${id}"${stale || changing || node.cleanup_pending || node.connection === 'OFFLINE' ? ' disabled' : ''}>${changing ? t('请求中…', 'Requesting…') : label}</button></div>
  </article>`;
}
