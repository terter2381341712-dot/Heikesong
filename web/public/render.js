const statusInfo = {
  QUEUED: ['排队中'], WAITING_FOR_RESOURCES: ['等待资源', 'warn'], DISPATCHING: ['分派对账中', 'active'],
  RUNNING: ['执行中', 'active'], CANCELLING: ['取消确认中', 'warn'], CANCEL_REQUESTED: ['已请求取消', 'warn'],
  SUCCEEDED: ['执行成功', 'good'], PARTIAL_FAILED: ['部分失败', 'warn'], FAILED: ['执行失败', 'bad'], CANCELLED: ['已取消'],
  REJECTED: ['分派拒绝', 'bad'], ACCEPTED: ['已受理', 'active'], ONLINE: ['在线', 'good'], OFFLINE: ['离线', 'bad'],
  ACTIVE: ['接单中', 'good'], DRAINING: ['退出中', 'warn'], PAUSED: ['已暂停'],
  UNAVAILABLE: ['不兼容'], NOT_PREPARED: ['未准备'], PREPARING: ['准备中', 'warn'], READY: ['已就绪', 'good'], ERROR: ['准备失败', 'bad'],
};

export const terminal = new Set(['SUCCEEDED', 'PARTIAL_FAILED', 'FAILED', 'CANCELLED']);
export const languageLabels = { auto: '自动识别', zh: '中文', en: '英语' };
const typeLabels = { 'ocr.image.v1': '图片文字识别', 'asr.file.v1': '短录音文字识别' };
const sourceLabels = { detected: '自动检测', unavailable: 'N/A', 'nvidia-smi': 'nvidia-smi' };
const beijing = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
});
const renderedHtml = new WeakMap();

export const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})[char]);
export const typeName = (type) => typeLabels[type] ?? type;
export const isConfigured = (type) => type?.configuration_state === 'CONFIGURED' && Boolean(type.profile_digest);
export const tag = (status) => {
  const [label, tone = ''] = Object.hasOwn(statusInfo, status) ? statusInfo[status] : [status];
  return `<span class="tag ${tone}" title="${escape(status)}">${escape(label)}</span>`;
};
export const date = (value) => value ? `${beijing.format(new Date(value))} 北京时间` : 'N/A';
export const bytes = (value) => {
  if (value == null) return 'N/A';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  const exponent = value ? Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024))) : 0;
  return `${Number((value / (1024 ** exponent)).toFixed(exponent ? 1 : 0))} ${units[exponent]}`;
};

export function updateHtml(element, html) {
  if (renderedHtml.get(element) === html) return;
  const details = new Set([...element.querySelectorAll('details[open][data-detail]')].map(({ dataset }) => dataset.detail));
  const focus = element.contains(document.activeElement) ? document.activeElement.dataset.focusKey : null;
  element.innerHTML = html;
  renderedHtml.set(element, html);
  for (const item of element.querySelectorAll('details[data-detail]')) item.open = details.has(item.dataset.detail);
  if (focus) element.querySelector(`[data-focus-key="${CSS.escape(focus)}"]`)?.focus({ preventScroll: true });
}

export function message(text, tone = '') {
  return text ? `<p class="notice ${tone}">${escape(text)}</p>` : '';
}

export function empty(title, text) {
  return `<div class="empty"><h2>${escape(title)}</h2><p>${escape(text)}</p></div>`;
}

export function historyRows(entries, current, limit = 25) {
  if (!entries.length) return `<p class="muted">此浏览器暂无任务记录。</p>`;
  return entries.slice(0, limit).map((entry) => `
    <a class="recent-item" href="#jobs/${escape(entry.id)}" data-focus-key="job-${escape(entry.id)}"${entry.id === current ? ' aria-current="true"' : ''}>
      <strong>${escape(entry.type ? typeName(entry.type) : '任务详情')}</strong>
      <small class="mono">${escape(entry.id)}</small>
      ${entry.status ? tag(entry.status) : ''}
    </a>`).join('');
}

function issue(error) {
  if (!error) return '';
  const request = error.request_id ? `（请求 ${error.request_id}）` : '';
  return message(`${error.code} · ${error.message}${request}`, 'error');
}

function asTimestamp(ms) {
  const minutes = String(Math.floor(ms / 60000)).padStart(2, '0');
  const seconds = String(Math.floor(ms / 1000) % 60).padStart(2, '0');
  return `${minutes}:${seconds}.${String(ms % 1000).padStart(3, '0')}`;
}

function output(result, id) {
  if (!result) return '';
  const text = `<p class="result-text">${escape(result.text || '（结果为空）')}</p>`;
  if (result.task_type === 'asr.file.v1') {
    const segments = result.segments.map((segment) => `<li><span class="mono">${asTimestamp(segment.start_ms)} – ${asTimestamp(segment.end_ms)}</span><span>${escape(segment.text)}</span></li>`).join('');
    return `${text}<p class="task-meta">${asTimestamp(result.duration_ms)} · ${escape(languageLabels[result.language] ?? 'N/A')}</p>${segments ? `<ul class="segments" aria-label="录音时间戳">${segments}</ul>` : ''}`;
  }
  const blocks = result.blocks.map((block) => `<li><span class="mono">[${escape(block.bbox.join(', '))}] · 置信度 ${escape(block.confidence ?? 'N/A')}</span><span>${escape(block.text)}</span></li>`).join('');
  return `${text}<p class="task-meta">${escape(result.image_width)} × ${escape(result.image_height)} px</p>${blocks ? `<details data-detail="ocr-${escape(id)}"><summary data-focus-key="ocr-${escape(id)}">文字块与位置（${result.blocks.length}）</summary><ul class="segments">${blocks}</ul></details>` : ''}`;
}

const countLabels = {
  succeeded: '成功', running: '执行', failed: '失败', cancelled: '取消', dispatching: '对账',
  waiting_for_resources: '等待资源', queued: '排队', cancelling: '取消中',
};

function renderCounts(counts) {
  const completed = counts.succeeded + counts.failed + counts.cancelled;
  const bars = ['succeeded', 'failed', 'cancelled', 'running'].map((status) => {
    const share = counts.total ? counts[status] / counts.total * 100 : 0;
    return `<span class="count-segment ${status}" data-share="${share}"></span>`;
  }).join('');
  const labels = Object.entries(countLabels).map(([status, label]) => `<span>${label} ${escape(counts[status])}</span>`).join('');
  return `<div class="count-heading"><strong>输入处理</strong><span>${escape(completed)} / ${escape(counts.total)} 项已结束</span></div>
    <div class="count-bar" aria-hidden="true">${bars}</div><div class="count-labels">${labels}</div>`;
}

function taskRow(task, result, resourceNames) {
  const id = escape(task.task_id);
  const last = task.dispatches.at(-1);
  const name = last ? resourceNames.get(last.worker_id) || last.worker_id : '待分派';
  const dispatches = task.dispatches.map((dispatch) => `
    <li><span class="mono">${escape(dispatch.worker_id)}</span><span>尝试 ${escape(dispatch.attempt_no)} ${tag(dispatch.status)} <code>${escape(dispatch.execution_id)}</code></span></li>
    ${dispatch.error ? `<li>${issue(dispatch.error)}</li>` : ''}`).join('');
  const artifact = task.input;
  return `<li class="task-row">
    <div class="task-header"><h3>${escape(task.input_index + 1)}. ${escape(artifact.original_name)}</h3>${tag(task.status)}</div>
    <p class="task-meta"><span>${escape(name)}</span><span>已受理 ${escape(task.attempts_used)} 次</span><span>${bytes(artifact.size_bytes)}</span></p>
    ${message(task.waiting_reason, 'warn')}${issue(result?.error ?? task.error)}
    ${output(result?.result, task.task_id)}
    ${dispatches ? `<details data-detail="dispatch-${id}"><summary data-focus-key="dispatch-${id}">分派记录（${task.dispatches.length}）</summary><ul class="segments">${dispatches}</ul></details>` : ''}
    <button class="button subtle" type="button" data-action="download-input" data-id="${id}" data-focus-key="input-${id}">下载原文件</button>
  </li>`;
}

export function jobPanel(job, results, resourceNames, { jobError, resultsError, cancelling, downloading }) {
  const id = escape(job.job_id);
  const orderedInputs = [...job.tasks].sort((left, right) => left.input_index - right.input_index);
  const items = new Map((results?.items ?? []).map((result) => [result.task_id, result]));
  const canDownload = terminal.has(job.status) && job.download_path;
  const canCancel = !terminal.has(job.status) && !job.cancel_requested;
  const needsP1 = job.dependency_status === 'UNREACHABLE';
  return `<section class="panel compact job-summary">
    <div class="panel-heading"><h2>${escape(typeName(job.task_type))}</h2>${tag(job.status)}</div>
    <p class="mono">Job ID ${id}</p><p class="task-meta">创建于 ${escape(date(job.created_at))} · ${escape(languageLabels[job.parameters.language])}</p>
    ${message(jobError && `快照已过期 · ${jobError}`, 'error')}
    ${message(needsP1 ? 'P2 报告 P1 资源服务不可达，等待恢复和对账。' : '', 'warn')}
    ${message(job.status === 'CANCELLING' ? '正在等待活动任务确认停止。' : '', 'warn')}
    ${renderCounts(job.counts)}
    <div class="summary-actions">
      <button class="button secondary" type="button" data-action="copy-id" data-focus-key="copy-id">复制 Job ID</button>
      ${canDownload ? `<button class="button primary" type="button" data-action="download-job" data-focus-key="download-job"${downloading || jobError ? ' disabled' : ''}><span data-icon="download"></span>${downloading ? '下载中…' : '下载 ZIP'}</button>` : ''}
      ${canCancel ? `<button class="button danger" type="button" data-action="cancel-job" data-focus-key="cancel-job"${cancelling || jobError ? ' disabled' : ''}>${cancelling ? '请求中…' : '取消任务'}</button>` : ''}
    </div>
  </section>
  <section id="job-results-panel" class="panel compact" tabindex="-1" data-focus-key="job-results-panel" aria-label="输入与结果">
    <div class="panel-heading"><h2>输入与结果</h2></div>
    ${message(resultsError && `${results ? '结果快照已过期' : '结果读取失败'} · ${resultsError}`, 'error')}
    <ul class="task-list">${orderedInputs.map((task) => taskRow(task, items.get(task.task_id), resourceNames)).join('')}</ul>
    ${results?.combined_text ? `<details data-detail="combined"><summary data-focus-key="combined">查看合并文本</summary><p class="result-text">${escape(results.combined_text)}</p></details>` : ''}
  </section>`;
}

function percent(value) {
  return value == null ? 'N/A' : `${Number(value.toFixed(1))}%`;
}

function nodeFacts(node) {
  const { hardware, telemetry } = node;
  const facts = [
    ['系统 / 架构', `${hardware.os} / ${hardware.arch}`],
    ['CPU（自动检测）', `${hardware.cpu_name ?? 'N/A'} · ${hardware.logical_cores ?? 'N/A'} 线程`],
    ['GPU（检测来源）', `${hardware.gpu_name ?? 'N/A'} · ${sourceLabels[hardware.gpu_info_source] ?? 'N/A'}`],
    ['GPU 显存', bytes(hardware.gpu_vram_bytes)],
    ['CPU / GPU 利用率', `${percent(telemetry?.cpu_percent)} / ${percent(telemetry?.gpu_percent)}`],
    ['内存已用 / 总量', `${bytes(telemetry?.ram_used_bytes)} / ${bytes(hardware.ram_total_bytes)}`],
    ['GPU 显存已用 / 采样来源', `${bytes(telemetry?.gpu_memory_used_bytes)} · ${sourceLabels[telemetry?.gpu_metric_source] ?? 'N/A'}`],
    ['槽位（占用 / 可用 / 总量）', `${node.reserved_slots} / ${node.available_slots} / ${node.max_slots}`],
  ];
  return facts.map(([label, value]) => `<div><dt>${escape(label)}</dt><dd>${escape(value)}</dd></div>`).join('');
}

function capabilityRows(capabilities) {
  if (!capabilities.length) return `<p class="field-hint">服务尚未报告能力。</p>`;
  return `<ul>${capabilities.map((capability) => `
    <li class="capability"><div><strong>${escape(typeName(capability.task_type))}</strong>
      <p class="field-hint">后端 ${escape(capability.backend ?? 'N/A')} · ${escape(capability.runtime_provider ?? 'N/A')}</p>
      <p class="field-hint">模型${capability.model_cached ? '已缓存' : '未缓存'} · ${capability.verified ? '自测已通过' : '自测未通过或未验证'}</p>
      ${capability.reason ? `<p class="field-hint">${escape(capability.reason)}</p>` : ''}</div>${tag(capability.state)}</li>`).join('')}</ul>`;
}

export function resourceCard(node, stale, changing) {
  const { hardware } = node;
  const id = escape(node.worker_id);
  const manual = Object.entries(hardware.manual_labels).map(([key, value]) => `${key} = ${value}`).join(' · ');
  const action = node.mode === 'ACTIVE' ? 'DRAINING' : 'ACTIVE';
  const label = action === 'DRAINING' ? '退出接单' : '恢复接单';
  return `<article class="panel compact resource-card">
    <div class="panel-heading"><h2>${escape(node.name)}</h2><div class="node-tags">${tag(node.connection)}${tag(node.mode)}</div></div>
    <p class="mono">${id}</p>
    <dl class="node-facts">${nodeFacts(node)}</dl>
    ${manual ? `<p class="field-hint">手动标注 · ${escape(manual)}</p>` : ''}
    ${hardware.detection_errors.map((error) => message(`检测异常 · ${error}`, 'warn')).join('')}
    ${message(node.mode === 'DRAINING' ? '现有工作完成后暂停接单。' : '', 'warn')}
    ${message(node.cleanup_pending ? '设备正在等待清理。' : '', 'warn')}
    <h3 class="node-subtitle">执行能力</h3>${capabilityRows(node.capabilities)}
    <div class="node-footer"><p class="field-hint">采样于 ${escape(date(node.telemetry?.sampled_at))}<br>心跳于 ${escape(date(node.last_seen_at))}</p>
      <button class="button secondary" type="button" data-action="node-mode" data-id="${id}" data-mode="${action}" data-focus-key="mode-${id}"${stale || changing || node.connection === 'OFFLINE' ? ' disabled' : ''}>${changing ? '请求中…' : label}</button></div>
  </article>`;
}
