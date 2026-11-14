import { describeError, demoFiles, getScenario, readConfig, resourceApi, setDemoToken, setScenario, taskApi, uuid } from './api.js';
import { getInputMime, inspectInput, mimeLabels } from './media.js';
import {
  bytes, date, empty, escape, historyRows, isConfigured, jobPanel, languageLabels, message,
  resourceCard, tag, terminal, updateHtml,
} from './render.js';

const $ = (id) => document.getElementById(id);
const icons = {
  submit: '<path d="M12 5v14m-7-7h14"/><rect x="3" y="3" width="18" height="18" rx="4"/>',
  jobs: '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M8 8h8m-8 4h8m-8 4h5"/>',
  devices: '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m4 17 5-5 4 4 3-3 4 4"/><circle cx="15" cy="9" r="1"/>',
  audio: '<path d="M3 11v2m4-5v8m5-12v16m5-12v8m4-5v2"/>',
  upload: '<path d="M12 16V4m-4 4 4-4 4 4M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
  arrow: '<path d="M4 12h15m-6-6 6 6-6 6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  settings: '<path d="M4 7h16M4 12h16M4 17h16M8 5v4m8 1v4m-6 1v4"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  download: '<path d="M12 4v12m-4-4 4 4 4-4M4 17v2a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-2"/>',
};

function drawIcons(root = document) {
  for (const icon of root.querySelectorAll('[data-icon]:empty')) {
    icon.innerHTML = `<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">${icons[icon.dataset.icon]}</svg>`;
  }
}

const jobIdPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const views = new Set(['submit', 'jobs', 'resources']);
const state = {
  mode: '', view: 'submit', jobId: null, history: [], connections: { P1: 'pending', P2: 'pending' }, resources: null,
  resourceError: '', resourceChecked: 0, nodeModeBusy: new Set(), catalog: [], catalogError: '', catalogChecked: false, selected: '', language: '',
  files: [], checkingFiles: false, sending: false, formError: '', formNote: '', pendingCreate: null,
  job: null, results: null, jobError: '', resultsError: '', jobChecked: 0, cancelling: null, downloading: null, scenario: 'success',
};
let activePoll;
let toastTimer;
let storageAvailable = true;
let resultsFocusPending = false;

function storageKey(part) {
  return `edge-fabric:0.2:${state.mode}:${part}`;
}

function stored(part, fallback) {
  try {
    return JSON.parse(localStorage.getItem(storageKey(part))) ?? fallback;
  } catch {
    storageAvailable = false;
    return fallback;
  }
}

function persist(part, value) {
  try {
    if (value == null) localStorage.removeItem(storageKey(part));
    else localStorage.setItem(storageKey(part), JSON.stringify(value));
  } catch {
    storageAvailable = false;
  }
}

function setNotice(element, text) {
  element.textContent = text;
  element.hidden = !text;
}

function toast(text) {
  clearTimeout(toastTimer);
  setNotice($('toast'), text);
  toastTimer = setTimeout(() => setNotice($('toast'), ''), 4500);
}

function errorText(error) {
  return error.code ? `${describeError(error)} · ${error.code}` : describeError(error);
}

function updateConnections() {
  for (const [service, status] of Object.entries(state.connections)) {
    const element = $(`${service.toLowerCase()}-connection`);
    element.dataset.tone = { good: 'good', auth: 'warn', bad: 'bad' }[status] ?? '';
    element.textContent = { pending: '待连接', good: '已连接', auth: '需要令牌', bad: '未接通' }[status];
  }
}

function connectionError(service, error) {
  if (error.uncertain || error.code === 'CONTRACT_VERSION' || error.code === 'INVALID_RESPONSE') state.connections[service] = 'bad';
  if (error.status === 401 || error.status === 403) state.connections[service] = 'auth';
}

function currentType() {
  return state.catalog.find((type) => type.task_type === state.selected);
}

function hasLockedDraft() {
  return Boolean(state.pendingCreate && (!state.pendingCreate.editable || !state.files.length));
}

function limits(type) {
  const formats = type.input_mime_types.map((mime) => mimeLabels[mime]).join(' / ');
  const media = type.max_duration_ms != null ? `${type.max_duration_ms / 1000} 秒以内 · 16 kHz 单声道 PCM 16 位` :
    type.max_pixels != null ? `最多 ${new Intl.NumberFormat('zh-CN').format(type.max_pixels)} 像素` : '';
  return [`${formats} · 每个文件 ≤ ${bytes(type.max_file_bytes)}`, media, `最多 ${type.max_inputs_per_job} 个文件`].filter(Boolean).join(' · ');
}

function renderForm() {
  const type = currentType();
  const locked = state.sending || state.checkingFiles || hasLockedDraft();
  const hasType = isConfigured(type);
  const canEdit = hasType && !locked;
  const catalogPresent = state.catalog.length > 0;
  const hasPending = Boolean(state.pendingCreate);
  const disabled = !hasType || Boolean(state.catalogError);
  const rows = state.catalog.map((entry) => {
    const enabled = isConfigured(entry);
    const id = escape(entry.task_type);
    const formats = entry.input_mime_types.map((mime) => mimeLabels[mime]).join(' / ');
    const info = enabled ? formats : `未配置 · ${entry.reason || '等待模型配置发布'}`;
    return `<label class="type-card">
      <input type="radio" name="task-type" value="${id}" data-focus-key="type-${id}"${state.selected === entry.task_type ? ' checked' : ''}${locked || !enabled ? ' disabled' : ''}>
      <span class="type-icon" data-icon="${entry.task_type === 'asr.file.v1' ? 'audio' : 'image'}"></span>
      <span><strong>${escape(entry.display_name)}</strong><small>${escape(info)}</small></span><i class="type-indicator" aria-hidden="true"></i>
    </label>`;
  }).join('');
  const noCatalog = state.catalogError ? '暂时无法读取任务类型。' : state.catalogChecked ? 'P2 暂未发布任务类型。' : '正在读取任务类型…';
  updateHtml($('type-options'), catalogPresent ? rows : `<p class="muted">${noCatalog}</p>`);
  drawIcons($('type-options'));
  setNotice($('catalog-error'), state.catalogError && `${catalogPresent ? '类型目录已过期 · ' : ''}${state.catalogError}`);
  updateHtml($('language'), (type?.supported_languages ?? []).map((language) => `<option value="${escape(language)}">${escape(languageLabels[language])}</option>`).join(''));
  $('language').value = state.language;
  $('language').disabled = !canEdit;
  $('files').accept = hasType ? type.input_mime_types.map((mime) => ({'image/jpeg': '.jpg,.jpeg', 'image/png': '.png', 'audio/wav': '.wav'})[mime] || mime).join(',') : '';
  $('files').disabled = $('choose-files').disabled = !canEdit;
  $('dropzone').setAttribute('aria-disabled', String(!canEdit));
  $('use-samples').hidden = state.mode !== 'mock';
  $('use-samples').disabled = !canEdit;
  $('file-limits').textContent = hasType ? limits(type) : '请先选择已配置的任务类型。';
  const names = state.files.length ? state.files : state.pendingCreate?.names.map((name) => ({ file: { name }, artifact: true })) ?? [];
  updateHtml($('file-list'), names.map((entry, index) => {
    let summary = bytes(entry.file.size);
    if (entry.artifact) summary = entry.file.size == null ? '文件已上传' : `${summary} · 上传完成`;
    if (entry.uploading) summary += ` · 上传中 ${Math.round(100 * entry.progress)}%`;
    const progress = entry.uploading ? `<progress max="1" value="${entry.progress}" aria-label="${escape(entry.file.name)} 上传进度"></progress>` : '';
    return `<li class="file-row"><div><p class="file-name">${escape(entry.file.name)}</p><p class="file-meta">${summary}</p>${progress}</div>
      ${locked ? '' : `<button class="icon-button" type="button" data-remove="${index}" data-focus-key="remove-${index}" aria-label="移除 ${escape(entry.file.name)}"><span data-icon="close"></span></button>`}</li>`;
  }).join(''));
  drawIcons($('file-list'));
  $('file-list').hidden = !names.length;
  setNotice($('form-error'), state.formError);
  $('submit-button').disabled = state.sending || state.checkingFiles || (!hasPending && (disabled || !state.files.length));
  $('submit-label').textContent = state.sending ? '提交中…' : hasPending ? '重试原提交' : '提交任务';
  $('discard-pending').hidden = !hasPending || !hasLockedDraft() || state.sending;
  $('submit-note').textContent = state.formNote || (hasPending ? '重试会沿用原提交 ID 和载荷。' : names.length ? `已选 ${names.length} 个文件` : '');
}

function renderResources() {
  const { resources, resourceError } = state;
  const reason = resourceError && `${resources ? '资源快照已过期' : '资源未接通'} · ${resourceError}`;
  let overview = message(reason, 'error');
  let detail = overview;
  if (resources) {
    const devices = resources.resources;
    const slots = devices.reduce((total, node) => total + node.available_slots, 0);
    const online = devices.filter((node) => node.connection === 'ONLINE').length;
    overview += `<div class="overview-stats"><div class="overview-stat"><strong>${online}</strong><span>在线设备</span></div>
      <div class="overview-stat"><strong>${slots}</strong><span>可用槽位</span></div></div>`;
    overview += devices.length ? `<ul class="overview-list">${devices.slice(0, 3).map((node) => `<li><span>${escape(node.name)}</span>${tag(node.mode)}</li>`).join('')}</ul>` : '<p class="field-hint overview-time">P1 当前未发现设备。</p>';
    detail += devices.length ? `<div class="resource-grid">${devices.map((node) => resourceCard(node, resourceError, state.nodeModeBusy.has(node.worker_id))).join('')}</div>` :
      empty('暂无设备', 'P1 当前未报告已注册设备。');
  } else if (!reason) {
    overview = '<p class="muted">正在读取资源…</p>';
    detail = empty('正在读取资源', '等待 P1 返回快照。');
  }
  updateHtml($('resource-overview'), overview);
  updateHtml($('resource-detail'), detail);
  $('snapshot-time').textContent = resources ? `快照时间 ${date(resources.server_time)}` : reason ? '等待 P1 资源服务连接' : '正在读取资源快照…';
}

function renderJobs() {
  updateHtml($('job-history'), historyRows(state.history, state.jobId));
  updateHtml($('recent-jobs'), historyRows(state.history, null, 3));
  let content;
  if (state.job) {
    const names = new Map((state.resources?.resources ?? []).map((node) => [node.worker_id, node.name]));
    content = jobPanel(state.job, state.results, names, {
      jobError: state.jobError, resultsError: state.resultsError, cancelling: state.cancelling === state.jobId, downloading: state.downloading === state.jobId,
    });
  } else if (state.jobError) {
    content = empty('无法读取任务', state.jobError);
  } else if (state.jobId) {
    content = empty('正在读取任务', state.jobId);
  } else {
    content = empty('请选择任务', '从本地记录选择，或输入 Job ID 查询。');
  }
  const detail = $('job-detail');
  updateHtml(detail, content);
  drawIcons(detail);
  for (const segment of detail.querySelectorAll('[data-share]')) segment.style.width = `${Number(segment.dataset.share)}%`;
  if (resultsFocusPending && state.results && $('job-results-panel')) {
    const { activeElement } = document;
    if (activeElement === document.body || activeElement === $('job-history-panel') || activeElement.closest('#job-history, #lookup-form')) {
      moveTo($('job-results-panel'));
    }
    resultsFocusPending = false;
  }
}

function rememberJob(job) {
  const existing = state.history.find((entry) => entry.id === job.job_id);
  if (existing) {
    if (existing.status === job.status && existing.type === job.task_type) return;
    existing.type = job.task_type;
    existing.status = job.status;
  } else {
    state.history.unshift({ id: job.job_id, type: job.task_type, status: job.status });
    state.history = state.history.slice(0, 25);
  }
  persist('history', state.history);
}

async function checkHealth(service, signal) {
  try {
    await (service === 'P1' ? resourceApi : taskApi).health(signal);
    if (!signal.aborted) state.connections[service] = 'good';
  } catch {
    if (!signal.aborted) state.connections[service] = 'bad';
  }
}

async function refreshCatalog(signal) {
  try {
    const response = await taskApi.types(signal);
    if (signal.aborted) return;
    state.catalog = response.task_types;
    state.catalogError = '';
    state.connections.P2 = 'good';
    const selected = currentType();
    if (!state.files.length && !state.pendingCreate && !state.checkingFiles && !state.sending && !isConfigured(selected)) {
      const first = state.catalog.find(isConfigured);
      state.selected = first?.task_type ?? '';
      state.language = first?.supported_languages[0] ?? '';
    } else if (selected && !state.pendingCreate && !state.sending && !selected.supported_languages.includes(state.language)) {
      state.language = selected.supported_languages[0];
    }
  } catch (error) {
    if (signal.aborted) return;
    state.catalogError = errorText(error);
    if (error.status === 503 && error.code === 'DEPENDENCY_UNAVAILABLE') {
      await checkHealth('P2', signal);
    } else connectionError('P2', error);
  }
  if (!signal.aborted) {
    state.catalogChecked = true;
    renderForm();
  }
}

async function refreshResources(signal) {
  try {
    const snapshot = await resourceApi.snapshot(signal);
    if (signal.aborted) return;
    state.resources = snapshot;
    state.resourceChecked = Date.now();
    state.resourceError = '';
    state.connections.P1 = 'good';
  } catch (error) {
    if (signal.aborted) return;
    state.resourceError = errorText(error);
    connectionError('P1', error);
  }
  if (!signal.aborted) renderResources();
}

async function refreshJob(id, signal) {
  try {
    const job = await taskApi.job(id, signal);
    if (signal.aborted) return;
    if (job?.job_id?.toLowerCase() !== id.toLowerCase() || !Array.isArray(job.tasks) || !job.counts) throw new Error('任务响应与查询 ID 或字段不匹配。');
    state.job = job;
    state.jobChecked = Date.now();
    state.jobError = '';
    state.connections.P2 = 'good';
    rememberJob(job);
  } catch (error) {
    if (signal.aborted) return;
    state.jobError = errorText(error);
    connectionError('P2', error);
    renderJobs();
    return;
  }
  try {
    const results = await taskApi.results(id, signal);
    if (signal.aborted) return;
    if (results?.job_id?.toLowerCase() !== id.toLowerCase() || !Array.isArray(results.items)) throw new Error('结果响应与查询 ID 或字段不匹配。');
    state.results = results;
    state.resultsError = '';
  } catch (error) {
    if (signal.aborted) return;
    state.resultsError = errorText(error);
  }
  renderJobs();
}

function restartPoll() {
  if (activePoll) {
    activePoll.controller.abort();
    clearTimeout(activePoll.timer);
  }
  if (!state.mode || document.hidden) return;
  const poll = { controller: new AbortController() };
  activePoll = poll;
  const { signal } = poll.controller;
  const { view, jobId } = state;
  const tick = async () => {
    const started = performance.now();
    const p1 = view !== 'jobs' || !state.job || !terminal.has(state.job.status) || state.job.dependency_status === 'UNREACHABLE'
      ? refreshResources(signal) : checkHealth('P1', signal);
    const p2 = view === 'submit' ? refreshCatalog(signal) : view === 'jobs' && jobId ? refreshJob(jobId, signal) : checkHealth('P2', signal);
    const checked = await Promise.allSettled([p1, p2]);
    if (signal.aborted) return;
    for (const result of checked) {
      if (result.status === 'rejected') toast(errorText(result.reason));
    }
    if (view === 'jobs') renderJobs();
    updateConnections();
    poll.timer = setTimeout(tick, Math.max(0, 2000 - (performance.now() - started)));
  };
  void tick();
}

function moveTo(element) {
  element.focus({ preventScroll: true });
  element.scrollIntoView({ block: 'start' });
}

function route(placeFocus = false) {
  const [requested, id, action] = location.hash.slice(1).split('/');
  const previousView = state.view;
  state.view = views.has(requested) ? requested : 'submit';
  const nextId = state.view === 'jobs' ? (jobIdPattern.test(id ?? '') ? id : state.history[0]?.id ?? null) : null;
  if (nextId !== state.jobId) {
    resultsFocusPending = false;
    state.jobId = nextId;
    state.job = state.results = null;
    state.jobError = state.resultsError = '';
    state.jobChecked = 0;
  }
  if (state.resources && Date.now() - state.resourceChecked > 6000 && !state.resourceError) {
    state.resourceError = '等待下一次 P1 资源快照。';
  }
  for (const name of views) $(`view-${name}`).hidden = name !== state.view;
  for (const link of document.querySelectorAll('nav [data-view]')) {
    if (link.dataset.view === state.view) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  const showResults = state.view === 'jobs' && (id === 'results' || action === 'results');
  if (placeFocus) resultsFocusPending = showResults && Boolean(nextId);
  renderForm();
  renderJobs();
  renderResources();
  if (placeFocus) {
    if (state.view === 'submit' && id === 'upload') moveTo($('submit-form'));
    else if (state.view === 'jobs' && showResults) moveTo(state.results ? $('job-results-panel') : $('job-history-panel'));
    else if (state.view === 'resources') moveTo($('resources-title'));
    else if (state.view !== previousView || !id) window.scrollTo(0, 0);
  }
  restartPoll();
}

function openJob(id) {
  const [view, section, action] = location.hash.slice(1).split('/');
  const focusResults = view === 'jobs' && (section === 'results' || action === 'results');
  const hash = `#jobs/${id}${focusResults ? '/results' : ''}`;
  if (location.hash === hash) route(true);
  else location.hash = hash;
}

async function addFiles(incoming, replace = false) {
  const type = currentType();
  if (!isConfigured(type) || hasLockedDraft() || state.sending || state.checkingFiles) return;
  state.checkingFiles = true;
  state.formError = '';
  state.formNote = '正在检查文件…';
  renderForm();
  const accepted = replace ? [] : [...state.files];
  const errors = [];
  try {
    if (accepted.length + incoming.length > type.max_inputs_per_job) {
      throw new Error(`此类型每批最多 ${type.max_inputs_per_job} 个文件。`);
    }
    for (const file of incoming) {
      const mime = getInputMime(file);
      try {
        if (!type.input_mime_types.includes(mime)) throw new Error('格式与所选类型不匹配。');
        if (file.size > type.max_file_bytes) throw new Error(`文件超过 ${bytes(type.max_file_bytes)}。`);
        inspectInput(await file.arrayBuffer(), mime, type);
        if (mime.startsWith('image/')) (await createImageBitmap(file)).close();
        accepted.push({ file, mime, uploadId: uuid(), progress: 0 });
      } catch (error) {
        errors.push(`${file.name} · ${error.message}`);
      }
    }
    if (accepted.length !== state.files.length || accepted.some((entry, index) => entry !== state.files[index])) {
      setPending(null);
      state.files = accepted;
    }
    if (errors.length) state.formError = errors.slice(0, 3).join('　') + (errors.length > 3 ? `　另有 ${errors.length - 3} 个文件未通过预检。` : '');
  } catch (error) {
    state.formError = error.message;
  } finally {
    state.checkingFiles = false;
    state.formNote = '';
    renderForm();
  }
}

function setPending(pending) {
  state.pendingCreate = pending;
  persist('pending', pending);
}

async function submit(event) {
  event.preventDefault();
  const type = currentType();
  if (state.sending || state.checkingFiles || (!state.pendingCreate && (!state.files.length || !isConfigured(type) || state.catalogError))) return;
  state.sending = true;
  state.formError = '';
  renderForm();
  try {
    if (!state.pendingCreate) {
      const hashes = new Set();
      for (const [index, entry] of state.files.entries()) {
        if (!entry.artifact) {
          state.formNote = `正在上传 ${index + 1} / ${state.files.length}`;
          entry.uploading = true;
          entry.progress = 0;
          renderForm();
          try {
            entry.artifact = await resourceApi.upload(entry.file, entry.uploadId, entry.mime, (progress) => {
              if (Math.round(entry.progress * 100) === Math.round(progress * 100)) return;
              entry.progress = progress;
              renderForm();
            });
          } finally {
            entry.uploading = false;
          }
        }
        if (hashes.has(entry.artifact.sha256)) throw new Error(`文件 ${entry.file.name} 与同批另一文件内容相同，请移除重复项。`);
        hashes.add(entry.artifact.sha256);
      }
      setPending({
        names: state.files.map((entry) => entry.file.name),
        payload: {
          client_request_id: uuid(), task_type: type.task_type, profile_id: type.profile_id, profile_digest: type.profile_digest,
          input_artifact_ids: state.files.map((entry) => entry.artifact.artifact_id), parameters: { language: state.language },
        },
      });
    }
    state.formNote = '正在创建任务…';
    renderForm();
    const job = await taskApi.create(state.pendingCreate.payload);
    if (!jobIdPattern.test(job?.job_id)) throw new Error('P2 未返回有效 Job ID，可用原提交重试。');
    rememberJob(job);
    setPending(null);
    state.files = [];
    toast(storageAvailable ? `已创建任务 ${job.job_id}` : `已创建任务 ${job.job_id}，浏览器本地存储不可用，请保存此 ID。`);
    openJob(job.job_id);
  } catch (error) {
    state.formError = errorText(error);
    if (state.pendingCreate) {
      const editable = error.status >= 400 && error.status < 500 && !error.uncertain &&
        ![401, 403, 429].includes(error.status) && error.code !== 'IDEMPOTENCY_CONFLICT';
      setPending({ ...state.pendingCreate, editable });
    }
    if (error.service) {
      connectionError(error.service, error);
      updateConnections();
    }
  } finally {
    state.sending = false;
    state.formNote = '';
    renderForm();
  }
}

function download(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function jobAction(event) {
  const button = event.target.closest('button[data-action]');
  const job = state.job;
  if (!button || !job) return;
  const id = job.job_id;
  const action = button.dataset.action;
  try {
    if (action === 'copy-id') {
      if (navigator.clipboard) {
        await navigator.clipboard.writeText(id);
        toast('Job ID 已复制。');
      } else toast(`Job ID ${id}`);
    } else if (action === 'download-input') {
      const input = job.tasks.find((task) => task.task_id === button.dataset.id)?.input;
      if (input) download(await resourceApi.download(input), input.original_name);
    } else if (action === 'cancel-job') {
      state.cancelling = id;
      renderJobs();
      try {
        const updated = await taskApi.cancel(id);
        rememberJob(updated);
        if (state.jobId === id) {
          state.job = updated;
          restartPoll();
        }
      } finally {
        state.cancelling = null;
        renderJobs();
      }
    } else if (action === 'download-job') {
      state.downloading = id;
      renderJobs();
      try {
        download(await taskApi.download(id), `${state.mode === 'mock' ? 'MOCK-' : ''}${id}.zip`);
      } finally {
        state.downloading = null;
        renderJobs();
      }
    }
  } catch (error) {
    toast(errorText(error));
  }
}

async function changeMode(event) {
  const button = event.target.closest('button[data-action="node-mode"]');
  if (!button) return;
  const { id, mode } = button.dataset;
  state.nodeModeBusy.add(id);
  renderResources();
  try {
    const node = await resourceApi.mode(id, mode);
    if (node?.worker_id !== id) throw new Error('P1 返回了不同的设备 ID。');
    if (state.resources) {
      state.resources.resources = state.resources.resources.map((resource) => resource.worker_id === id ? node : resource);
    }
    if (state.view === 'resources') restartPoll();
  } catch (error) {
    toast(errorText(error));
  } finally {
    state.nodeModeBusy.delete(id);
    renderResources();
  }
}

function bind() {
  window.addEventListener('hashchange', () => route(true));
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      if (state.job && Date.now() - state.jobChecked > 6000 && !state.jobError) {
        state.jobError = '等待下一次 P2 任务快照。';
      }
      route();
    } else restartPoll();
  });
  $('workflow-graph').addEventListener('click', (event) => {
    const link = event.target.closest('[data-workflow-node]');
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
    if (link?.hash === location.hash) {
      event.preventDefault();
      route(true);
    }
  });
  $('submit-form').addEventListener('submit', submit);
  $('type-options').addEventListener('change', (event) => {
    setPending(null);
    state.selected = event.target.value;
    state.language = currentType().supported_languages[0];
    state.files = [];
    state.formError = '';
    renderForm();
  });
  $('language').addEventListener('change', (event) => {
    setPending(null);
    state.language = event.target.value;
    state.formError = '';
    renderForm();
  });
  $('choose-files').addEventListener('click', () => $('files').click());
  $('files').addEventListener('change', (event) => {
    void addFiles([...event.target.files]);
    event.target.value = '';
  });
  $('file-list').addEventListener('click', (event) => {
    const button = event.target.closest('button[data-remove]');
    if (!button) return;
    setPending(null);
    state.files.splice(Number(button.dataset.remove), 1);
    state.formError = '';
    renderForm();
  });
  $('discard-pending').addEventListener('click', () => {
    if (!state.pendingCreate.editable && !confirm('重新选文件将开始新提交。如果 P2 已受理，原任务仍会继续。')) return;
    setPending(null);
    state.files = [];
    state.formError = '';
    renderForm();
  });
  $('use-samples').addEventListener('click', async () => {
    const type = currentType();
    if (!isConfigured(type) || state.checkingFiles || state.sending || hasLockedDraft()) return;
    state.checkingFiles = true;
    renderForm();
    try {
      const files = await demoFiles(type.input_mime_types.includes('image/png') ? 'image/png' : type.input_mime_types[0], state.scenario === 'partial-failure' ? 2 : 1);
      state.checkingFiles = false;
      await addFiles(files, true);
    } catch (error) {
      state.formError = errorText(error);
    } finally {
      state.checkingFiles = false;
      renderForm();
    }
  });
  const dropzone = $('dropzone');
  for (const name of ['dragenter', 'dragover', 'dragleave', 'drop']) {
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      const allowed = isConfigured(currentType()) && !hasLockedDraft() && !state.sending && !state.checkingFiles;
      dropzone.classList.toggle('dragging', allowed && ['dragenter', 'dragover'].includes(name));
      if (allowed && name === 'drop') void addFiles([...event.dataTransfer.files]);
    });
  }
  for (const name of ['dragover', 'drop']) {
    window.addEventListener(name, (event) => {
      if (event.dataTransfer?.types.includes('Files')) event.preventDefault();
    });
  }
  $('lookup-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const id = $('lookup-id').value.trim();
    const valid = jobIdPattern.test(id);
    setNotice($('lookup-error'), valid ? '' : '请填写完整 Job ID（UUID）。');
    if (valid) openJob(id);
  });
  $('job-history').addEventListener('click', (event) => {
    if (!location.hash.endsWith('/results')) return;
    const link = event.target.closest('a');
    const jobId = link?.hash.split('/')[1];
    if (!jobId || event.button !== 0 || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
    event.preventDefault();
    openJob(jobId);
  });
  $('job-detail').addEventListener('click', jobAction);
  $('resource-detail').addEventListener('click', changeMode);
  $('open-settings').addEventListener('click', async () => {
    setNotice($('settings-error'), '');
    $('settings-dialog').showModal();
    if (state.mode === 'mock') {
      $('scenario').value = state.scenario;
      $('save-settings').disabled = true;
      try {
        const { scenario } = await getScenario();
        state.scenario = scenario;
        $('scenario').value = scenario;
      } catch (error) {
        setNotice($('settings-error'), errorText(error));
      } finally {
        $('save-settings').disabled = false;
      }
    }
  });
  $('close-settings').addEventListener('click', () => $('settings-dialog').close());
  $('settings-dialog').addEventListener('close', () => { $('token').value = ''; });
  $('settings-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    $('save-settings').disabled = true;
    try {
      if (state.mode === 'mock') state.scenario = (await setScenario($('scenario').value)).scenario;
      else setDemoToken($('token').value);
      $('settings-dialog').close();
      restartPoll();
    } catch (error) {
      setNotice($('settings-error'), errorText(error));
    } finally {
      $('save-settings').disabled = false;
    }
  });
}

async function boot() {
  drawIcons();
  const { mock } = await readConfig();
  state.mode = mock ? 'mock' : 'live';
  state.history = stored('history', []);
  const pending = stored('pending', null);
  if (pending?.payload && Array.isArray(pending.names)) {
    state.pendingCreate = pending;
    state.selected = pending.payload.task_type;
    state.language = pending.payload.parameters.language;
  }
  $('environment').hidden = !mock;
  $('environment').textContent = 'MOCK · 合成演示';
  $('settings-label').textContent = mock ? '模拟场景' : '连接设置';
  $('open-settings').hidden = false;
  $('mock-settings').hidden = !mock;
  $('live-settings').hidden = mock;
  $('dialog-title').textContent = mock ? '模拟场景' : '连接设置';
  bind();
  route(Boolean(location.hash));
  if (mock) {
    try { state.scenario = (await getScenario()).scenario; }
    catch (error) { toast(errorText(error)); }
  }
}

boot().catch((error) => setNotice($('startup-error'), `工作台启动失败 · ${errorText(error)}`));
