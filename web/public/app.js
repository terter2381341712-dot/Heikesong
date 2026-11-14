import { locale, restoreLocale, setLocale, t } from './i18n.js';
import { describeError, demoFiles, getScenario, readConfig, resourceApi, setDemoToken, setScenario, taskApi, uuid } from './api.js';
import { getInputMime, inspectInput, mimeLabels } from './media.js';
import {
  bytes, date, empty, escape, historyRows, isConfigured, jobPanel, languageName, message,
  resourceCard, terminal, typeName, updateHtml,
} from './render.js';
import { syncPlayback } from './workflow.js';

const $ = (id) => document.getElementById(id);
const icons = {
  jobs: '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M8 8h8m-8 4h8m-8 4h5"/>',
  devices: '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m4 17 5-5 4 4 3-3 4 4"/><circle cx="15" cy="9" r="1"/>',
  audio: '<path d="M3 11v2m4-5v8m5-12v16m5-12v8m4-5v2"/>',
  upload: '<path d="M12 16V4m-4 4 4-4 4 4M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
  arrow: '<path d="M4 12h15m-6-6 6 6-6 6"/>',
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
const panelTitles = {
  upload: ['上传识别文件', 'Upload files'], progress: ['任务进度', 'Job progress'],
  results: ['识别结果', 'Results'], resources: ['设备资源', 'Devices'],
};
const isJobView = (view) => view === 'progress' || view === 'results';
const workflowDialog = $('workflow-dialog');
const workflowBody = $('workflow-body');
const state = {
  mode: '', view: 'home', jobId: null, history: [], connections: { P1: 'pending', P2: 'pending' }, resources: null,
  resourceError: '', resourceChecked: 0, nodeModeBusy: new Set(), catalog: [], catalogError: '', catalogChecked: false, selected: '', language: '',
  files: [], checkingFiles: false, sending: false, formError: '', formNote: null, pendingCreate: null,
  job: null, results: null, jobError: '', resultsError: '', jobChecked: 0, cancelling: new Set(), downloading: new Set(), scenario: 'success',
};
let activePoll;
let toastTimer;
let storageAvailable = true;
let workflowOpener;
let returningHome = false;

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
  const target = workflowDialog.open ? $('workflow-toast') : $('toast');
  setNotice(workflowDialog.open ? $('toast') : $('workflow-toast'), '');
  setNotice(target, text);
  toastTimer = setTimeout(() => setNotice(target, ''), 4500);
}

function errorText(error) {
  return error.code ? `${describeError(error)} · ${error.code}` : describeError(error);
}

function updateConnections() {
  for (const [service, status] of Object.entries(state.connections)) {
    const element = $(`${service.toLowerCase()}-connection`);
    element.dataset.tone = { good: 'good', auth: 'warn', bad: 'bad' }[status] ?? '';
    element.textContent = { pending: t('待连接', 'Connecting'), good: t('已连接', 'Connected'), auth: t('需要令牌', 'Token required'), bad: t('未接通', 'Disconnected') }[status];
  }
  $('workflow-settings').hidden = state.mode !== 'live' || !Object.values(state.connections).includes('auth');
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
  const media = type.max_duration_ms != null ? t(`${type.max_duration_ms / 1000} 秒以内 · 16 kHz 单声道 PCM 16 位`, `Up to ${type.max_duration_ms / 1000} s · 16 kHz mono 16-bit PCM`) :
    type.max_pixels != null ? t(`最多 ${new Intl.NumberFormat('zh-CN').format(type.max_pixels)} 像素`, `Up to ${new Intl.NumberFormat(locale).format(type.max_pixels)} pixels`) : '';
  return [t(`${formats} · 每个文件 ≤ ${bytes(type.max_file_bytes)}`, `${formats} · ≤ ${bytes(type.max_file_bytes)} per file`), media, t(`最多 ${type.max_inputs_per_job} 个文件`, `Up to ${type.max_inputs_per_job} files`)].filter(Boolean).join(' · ');
}

function renderForm() {
  if (state.view !== 'upload') return;
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
    const info = enabled ? formats : `${t('未配置', 'Not configured')} · ${entry.reason || t('等待模型配置发布', 'Model configuration pending')}`;
    const name = locale === 'en' ? typeName(entry.task_type, entry.display_name) : entry.display_name;
    return `<label class="type-card">
      <input type="radio" name="task-type" value="${id}" data-focus-key="type-${id}"${state.selected === entry.task_type ? ' checked' : ''}${locked || !enabled ? ' disabled' : ''}>
      <span class="type-icon" data-icon="${entry.task_type === 'asr.file.v1' ? 'audio' : 'image'}"></span>
      <span><strong>${escape(name)}</strong><small>${escape(info)}</small></span><i class="type-indicator" aria-hidden="true"></i>
    </label>`;
  }).join('');
  const noCatalog = state.catalogError ? t('暂时无法读取任务类型。', 'Task types are unavailable.') : state.catalogChecked ? t('P2 暂未发布任务类型。', 'P2 has not published any task types.') : t('正在读取任务类型…', 'Loading task types…');
  updateHtml($('type-options'), catalogPresent ? rows : `<p class="muted">${noCatalog}</p>`);
  drawIcons($('type-options'));
  setNotice($('catalog-error'), state.catalogError && `${catalogPresent ? t('类型目录已过期 · ', 'Task catalog is out of date · ') : ''}${state.catalogError}`);
  updateHtml($('language'), (type?.supported_languages ?? []).map((language) => `<option value="${escape(language)}">${escape(languageName(language))}</option>`).join(''));
  $('language').value = state.language;
  $('language').disabled = !canEdit;
  $('files').accept = hasType ? type.input_mime_types.map((mime) => ({'image/jpeg': '.jpg,.jpeg', 'image/png': '.png', 'audio/wav': '.wav'})[mime] || mime).join(',') : '';
  $('files').disabled = $('choose-files').disabled = !canEdit;
  $('dropzone').setAttribute('aria-disabled', String(!canEdit));
  $('use-samples').hidden = state.mode !== 'mock';
  $('use-samples').disabled = !canEdit;
  $('file-limits').textContent = hasType ? limits(type) : t('请先选择已配置的任务类型。', 'Choose a configured task type first.');
  const names = state.files.length ? state.files : state.pendingCreate?.names.map((name) => ({ file: { name }, artifact: true })) ?? [];
  updateHtml($('file-list'), names.map((entry, index) => {
    let summary = bytes(entry.file.size);
    if (entry.artifact) summary = entry.file.size == null ? t('文件已上传', 'File uploaded') : t(`${summary} · 上传完成`, `${summary} · Uploaded`);
    if (entry.uploading) summary += ` · ${t('上传中', 'Uploading')} ${Math.round(100 * entry.progress)}%`;
    const progress = entry.uploading ? `<progress max="1" value="${entry.progress}" aria-label="${t('上传进度', 'Upload progress')}: ${escape(entry.file.name)}"></progress>` : '';
    return `<li class="file-row"><div><p class="file-name">${escape(entry.file.name)}</p><p class="file-meta">${summary}</p>${progress}</div>
      ${locked ? '' : `<button class="icon-button" type="button" data-remove="${index}" data-focus-key="remove-${index}" aria-label="${t('移除', 'Remove')} ${escape(entry.file.name)}"><span data-icon="close"></span></button>`}</li>`;
  }).join(''), $('choose-files'));
  drawIcons($('file-list'));
  $('file-list').hidden = !names.length;
  setNotice($('form-error'), state.formError);
  $('submit-button').disabled = state.sending || state.checkingFiles || (!hasPending && (disabled || !state.files.length));
  $('submit-label').textContent = state.sending ? t('提交中…', 'Submitting…') : hasPending ? t('重试原提交', 'Retry submission') : t('提交任务', 'Submit job');
  $('discard-pending').hidden = !hasPending || !hasLockedDraft() || state.sending;
  $('submit-note').textContent = state.formNote?.() || (hasPending ? t('重试会沿用原提交 ID 和载荷。', 'Retrying uses the original submission ID and payload.') : names.length ? t(`已选 ${names.length} 个文件`, `Files selected: ${names.length}`) : '');
}

function renderResources() {
  if (state.view !== 'resources') return;
  const { resources, resourceError } = state;
  const reason = resourceError && `${resources ? t('资源快照已过期', 'Resource snapshot is out of date') : t('资源未接通', 'Resource service unavailable')} · ${resourceError}`;
  let detail = message(reason, 'error');
  let counts = '';
  if (resources) {
    const devices = resources.resources;
    const slots = devices.reduce((total, node) => total + node.available_slots, 0);
    const online = devices.filter((node) => node.connection === 'ONLINE').length;
    counts = t(`在线设备 ${online} 台 · 可用槽位 ${slots} 个`, `Online devices: ${online} · Available slots: ${slots}`);
    detail += devices.length ? `<div class="resource-grid">${devices.map((node) => resourceCard(node, {
      stale: Boolean(resourceError), changing: state.nodeModeBusy.has(node.worker_id), mock: state.mode === 'mock',
    })).join('')}</div>` :
      empty(t('暂无设备', 'No devices'), t('P1 当前未报告已注册设备。', 'P1 has not reported any registered devices.'));
  } else if (!reason) {
    detail = empty(t('正在读取资源', 'Loading resources'), t('等待 P1 返回快照。', 'Waiting for a snapshot from P1.'));
  }
  setNotice($('resource-counts'), counts);
  updateHtml($('resource-detail'), detail, $('close-workflow'));
  $('snapshot-time').textContent = resources ? t(`快照时间 ${date(resources.server_time)}`, `Snapshot taken ${date(resources.server_time)}`) : reason ? t('等待 P1 资源服务连接', 'Waiting for the P1 resource service') : t('正在读取资源快照…', 'Loading resource snapshot…');
}

function renderJobs() {
  if (!isJobView(state.view)) return;
  const showResults = state.view === 'results';
  updateHtml($('job-history'), historyRows(state.history, state.jobId, showResults), $('lookup-id'));
  let content;
  if (state.job) {
    const names = new Map((state.resources?.resources ?? []).map((node) => [node.worker_id, node.name]));
    content = jobPanel(state.job, state.results, names, {
      showResults, jobError: state.jobError, resultsError: state.resultsError,
      cancelling: state.cancelling.has(state.jobId), downloading: state.downloading.has(state.jobId),
    });
  } else if (state.jobError) {
    content = empty(t('无法读取任务', 'Could not load the job'), state.jobError);
  } else if (state.jobId) {
    content = empty(t('正在读取任务', 'Loading job'), state.jobId);
  } else {
    content = empty(t('请选择任务', 'Choose a job'), t('从本地记录选择，或输入 Job ID 查询。', 'Choose from local history or enter a Job ID.'));
  }
  const detail = $('job-detail');
  updateHtml(detail, content, detail);
  drawIcons(detail);
  for (const segment of detail.querySelectorAll('[data-share]')) segment.style.width = `${Number(segment.dataset.share)}%`;
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
    if (!signal.aborted && state.connections[service] !== 'auth') state.connections[service] = 'good';
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
  if (!signal.aborted) {
    renderResources();
    renderJobs();
  }
}

async function refreshJob(id, showResults, signal) {
  try {
    const job = await taskApi.job(id, signal);
    if (signal.aborted) return;
    if (job?.job_id?.toLowerCase() !== id.toLowerCase() || !Array.isArray(job.tasks) || !job.counts) throw new Error(t('任务响应与查询 ID 或字段不匹配。', 'The job response does not match the requested ID or expected fields.'));
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
  renderJobs();
  if (!showResults) return;
  try {
    const results = await taskApi.results(id, signal);
    if (signal.aborted) return;
    if (results?.job_id?.toLowerCase() !== id.toLowerCase() || !Array.isArray(results.items)) throw new Error(t('结果响应与查询 ID 或字段不匹配。', 'The results response does not match the requested ID or expected fields.'));
    state.results = results;
    state.resultsError = '';
  } catch (error) {
    if (signal.aborted) return;
    state.resultsError = errorText(error);
    connectionError('P2', error);
  }
  renderJobs();
}

function restartPoll() {
  if (activePoll) {
    activePoll.controller.abort();
    clearTimeout(activePoll.timer);
    activePoll = null;
  }
  if (!state.mode || document.hidden) return;
  const poll = { controller: new AbortController() };
  activePoll = poll;
  const { signal } = poll.controller;
  const { view, jobId } = state;
  const tick = async () => {
    const started = performance.now();
    const needsResources = view === 'resources' || (isJobView(view) && jobId &&
      (!state.resources || !state.job || !terminal.has(state.job.status) || state.job.dependency_status === 'UNREACHABLE'));
    const p1 = needsResources ? refreshResources(signal) : checkHealth('P1', signal);
    let p2;
    if (view === 'upload') p2 = refreshCatalog(signal);
    else if (isJobView(view) && jobId) p2 = refreshJob(jobId, view === 'results', signal);
    else p2 = checkHealth('P2', signal);
    const checked = await Promise.allSettled([p1, p2]);
    if (signal.aborted) return;
    for (const result of checked) {
      if (result.status === 'rejected') toast(errorText(result.reason));
    }
    updateConnections();
    poll.timer = setTimeout(tick, Math.max(0, 2000 - (performance.now() - started)));
  };
  void tick();
}

function readRoute() {
  const [section, id, action] = location.hash.slice(1).split('/');
  if (section === 'submit' && id === 'upload') return { view: 'upload' };
  if (section === 'resources') return { view: 'resources' };
  if (section !== 'jobs') return { view: 'home' };
  return {
    view: id === 'results' || action === 'results' ? 'results' : 'progress',
    jobId: jobIdPattern.test(id ?? '') ? id.toLowerCase() : state.job?.job_id ?? state.history[0]?.id ?? null,
  };
}

function markStale() {
  if (state.view === 'resources' && state.resources && Date.now() - state.resourceChecked > 6000 && !state.resourceError) {
    state.resourceError = t('等待下一次 P1 资源快照。', 'Waiting for the next P1 resource snapshot.');
  }
  if (isJobView(state.view) && state.job && Date.now() - state.jobChecked > 6000 && !state.jobError) {
    state.jobError = t('等待下一次 P2 任务快照。', 'Waiting for the next P2 job snapshot.');
  }
}

function route() {
  returningHome = false;
  const { view, jobId } = readRoute();
  const changedView = state.view !== view;
  const changedJob = isJobView(view) && state.jobId !== jobId;
  state.view = view;
  if (changedJob) {
    state.jobId = jobId;
    state.job = state.results = null;
    state.jobError = state.resultsError = '';
    state.jobChecked = 0;
  }
  if (view === 'home') {
    if (workflowDialog.open) {
      workflowDialog.close();
      syncPlayback();
      workflowOpener.focus({ preventScroll: true });
    }
  } else {
    workflowDialog.dataset.panel = view;
    $('workflow-title').textContent = t(...panelTitles[view]);
    $('view-upload').hidden = view !== 'upload';
    $('view-jobs').hidden = !isJobView(view);
    $('view-resources').hidden = view !== 'resources';
    markStale();
    renderForm();
    renderJobs();
    renderResources();
    if (!workflowDialog.open) {
      workflowOpener = $('workflow-graph').querySelector(`[data-workflow-node="${view}"]`);
      workflowDialog.showModal();
      syncPlayback();
    }
    if (changedView) {
      workflowBody.scrollTop = 0;
      $('workflow-title').focus({ preventScroll: true });
    } else if (changedJob) {
      $('job-detail').focus({ preventScroll: true });
      $('job-detail').scrollIntoView({ block: 'start' });
    }
  }
  restartPoll();
}

function navigate(hash) {
  if (location.hash !== hash) {
    if (state.view === 'home') history.pushState({ workflow: true }, '', hash);
    else history.replaceState(history.state, '', hash);
  }
  route();
}

function closeWorkflow() {
  if (returningHome) return;
  // Only go back if this entry was opened from our workflow home.
  if (history.state?.workflow) {
    returningHome = true;
    history.back();
  } else {
    history.replaceState(history.state, '', '#submit');
    route();
  }
}

function openJob(id) {
  navigate(`#jobs/${id}${state.view === 'results' ? '/results' : ''}`);
}

async function addFiles(incoming, replace = false) {
  const type = currentType();
  if (!isConfigured(type) || hasLockedDraft() || state.sending || state.checkingFiles) return;
  state.checkingFiles = true;
  state.formError = '';
  state.formNote = () => t('正在检查文件…', 'Checking files…');
  renderForm();
  const accepted = replace ? [] : [...state.files];
  const errors = [];
  try {
    if (accepted.length + incoming.length > type.max_inputs_per_job) {
      throw new Error(t(`此类型每批最多 ${type.max_inputs_per_job} 个文件。`, `This task type supports up to ${type.max_inputs_per_job} files per job.`));
    }
    for (const file of incoming) {
      const mime = getInputMime(file);
      try {
        if (!type.input_mime_types.includes(mime)) throw new Error(t('格式与所选类型不匹配。', 'The format does not match the selected task type.'));
        if (file.size > type.max_file_bytes) throw new Error(t(`文件超过 ${bytes(type.max_file_bytes)}。`, `The file exceeds ${bytes(type.max_file_bytes)}.`));
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
    if (errors.length) state.formError = errors.slice(0, 3).join('　') + (errors.length > 3 ? t(`　另有 ${errors.length - 3} 个文件未通过预检。`, ` · ${errors.length - 3} more files failed validation.`) : '');
  } catch (error) {
    state.formError = error.message;
  } finally {
    state.checkingFiles = false;
    state.formNote = null;
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
          state.formNote = () => t(`正在上传 ${index + 1} / ${state.files.length}`, `Uploading ${index + 1} / ${state.files.length}`);
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
        if (hashes.has(entry.artifact.sha256)) throw new Error(t(`文件 ${entry.file.name} 与同批另一文件内容相同，请移除重复项。`, `${entry.file.name} duplicates another file in the batch. Remove the duplicate to continue.`));
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
    state.formNote = () => t('正在创建任务…', 'Creating job…');
    renderForm();
    const job = await taskApi.create(state.pendingCreate.payload);
    if (!jobIdPattern.test(job?.job_id)) throw new Error(t('P2 未返回有效 Job ID，可用原提交重试。', 'P2 did not return a valid Job ID. Retry the original submission.'));
    rememberJob(job);
    setPending(null);
    state.files = [];
    if (state.view === 'upload' && !returningHome) openJob(job.job_id);
    else renderJobs();
    toast(storageAvailable ? t(`已创建任务 ${job.job_id}`, `Job created: ${job.job_id}`) : t(`已创建任务 ${job.job_id}，浏览器本地存储不可用，请保存此 ID。`, `Job created: ${job.job_id}. Save this ID because browser storage is unavailable.`));
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
    state.formNote = null;
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

async function copyJobId(id) {
  try {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(id);
      toast(t('Job ID 已复制。', 'Job ID copied.'));
      return;
    }
  } catch { /* Fall back to text selection if clipboard access was denied. */ }
  const selection = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents($('job-id-text'));
  selection.removeAllRanges();
  selection.addRange(range);
  toast(document.execCommand('copy') ? t('Job ID 已复制。', 'Job ID copied.') : t('已选中 Job ID，可手动复制。', 'Job ID selected. Copy it manually.'));
}

async function jobAction(event) {
  const button = event.target.closest('button[data-action]');
  const job = state.job;
  if (!button || !job) return;
  const id = job.job_id;
  const action = button.dataset.action;
  try {
    if (action === 'copy-id') {
      await copyJobId(id);
    } else if (action === 'download-input') {
      const input = job.tasks.find((task) => task.task_id === button.dataset.id)?.input;
      if (input) download(await resourceApi.download(input), input.original_name);
    } else if (action === 'cancel-job') {
      if (state.cancelling.has(id)) return;
      state.cancelling.add(id);
      renderJobs();
      try {
        const updated = await taskApi.cancel(id);
        rememberJob(updated);
        if (state.jobId === id) {
          state.job = updated;
          restartPoll();
        }
      } finally {
        state.cancelling.delete(id);
        renderJobs();
      }
    } else if (action === 'download-job') {
      if (state.downloading.has(id)) return;
      state.downloading.add(id);
      renderJobs();
      try {
        download(await taskApi.download(id), `${state.mode === 'mock' ? 'MOCK-' : ''}${id}.zip`);
      } finally {
        state.downloading.delete(id);
        renderJobs();
      }
    }
  } catch (error) {
    if (error.service) {
      connectionError(error.service, error);
      updateConnections();
    }
    toast(errorText(error));
  }
}

async function changeMode(event) {
  const button = event.target.closest('button[data-action="node-mode"]');
  if (!button) return;
  const { id, mode } = button.dataset;
  if (state.nodeModeBusy.has(id)) return;
  state.nodeModeBusy.add(id);
  renderResources();
  try {
    const node = await resourceApi.mode(id, mode);
    if (node?.worker_id !== id) throw new Error(t('P1 返回了不同的设备 ID。', 'P1 returned a different device ID.'));
    if (state.resources) {
      state.resources.resources = state.resources.resources.map((resource) => resource.worker_id === id ? node : resource);
    }
    if (state.view === 'resources') restartPoll();
  } catch (error) {
    if (error.service) {
      connectionError(error.service, error);
      updateConnections();
    }
    toast(errorText(error));
  } finally {
    state.nodeModeBusy.delete(id);
    renderResources();
  }
}

async function openSettings() {
  setNotice($('settings-error'), '');
  $('settings-dialog').showModal();
  syncPlayback();
  if (state.mode !== 'mock') return;
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

function bind() {
  window.addEventListener('hashchange', route);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      markStale();
      renderResources();
      renderJobs();
    }
    restartPoll();
  });
  document.addEventListener('click', (event) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
    const link = event.target.closest('[data-workflow-node], #workflow-dialog a[href^="#"]');
    if (!link) return;
    event.preventDefault();
    navigate(link.hash);
  });
  $('close-workflow').addEventListener('click', closeWorkflow);
  workflowDialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    closeWorkflow();
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
    if (!state.pendingCreate.editable && !confirm(t('重新选文件将开始新提交。如果 P2 已受理，原任务仍会继续。', 'Choosing new files starts a new submission. If P2 already accepted the original job, it will continue.'))) return;
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
    setNotice($('lookup-error'), valid ? '' : t('请填写完整 Job ID（UUID）。', 'Enter a complete Job ID (UUID).'));
    if (valid) openJob(id);
  });
  $('job-detail').addEventListener('click', jobAction);
  $('resource-detail').addEventListener('click', changeMode);
  $('open-settings').addEventListener('click', openSettings);
  $('workflow-settings').addEventListener('click', openSettings);
  $('ui-language').addEventListener('change', (event) => {
    setLocale(event.target.value);
    updateConnections();
    $('environment').textContent = t('MOCK · 合成演示', 'MOCK · Demo');
    if (workflowDialog.open) $('workflow-title').textContent = t(...panelTitles[state.view]);
    renderForm();
    renderJobs();
    renderResources();
  });
  $('close-settings').addEventListener('click', () => $('settings-dialog').close());
  $('settings-dialog').addEventListener('close', () => {
    $('token').value = '';
    syncPlayback();
  });
  $('settings-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    $('save-settings').disabled = true;
    try {
      if (state.mode === 'mock') {
        state.scenario = (await setScenario($('scenario').value)).scenario;
      } else {
        setDemoToken($('token').value);
        for (const service of ['P1', 'P2']) {
          if (state.connections[service] === 'auth') state.connections[service] = 'pending';
        }
        updateConnections();
      }
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
  restoreLocale();
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
  $('environment').textContent = t('MOCK · 合成演示', 'MOCK · Demo');
  $('open-settings').hidden = false;
  $('mock-settings').hidden = !mock;
  $('live-settings').hidden = mock;
  $('node-sharing-hint').hidden = mock;
  $('hub-address').textContent = location.origin;
  bind();
  route();
  if (mock) {
    try { state.scenario = (await getScenario()).scenario; }
    catch (error) { toast(errorText(error)); }
  }
}

boot().catch((error) => setNotice($('startup-error'), t(`工作台启动失败 · ${errorText(error)}`, `Could not start the workbench · ${errorText(error)}`)));
