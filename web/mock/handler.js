import { readFile } from 'node:fs/promises';
import { createHash, randomUUID } from 'node:crypto';
import { emptyMedia, InputError, inspectInput } from '../public/media.js';
import { zip } from './zip.js';

const fixtures = JSON.parse(await readFile(new URL('./fixtures.json', import.meta.url), 'utf8'));
const configured = fixtures.catalog_configured;
const resourcePrefix = '/api/resource/v1';
const taskPrefix = '/api/task/v1';
const worker = fixtures.resources_ready.resources[0];
const capabilities = [...worker.capabilities, ...fixtures.resources_ready_asr.resources[0].capabilities];
const maxUpload = Math.max(...configured.task_types.map((type) => type.max_file_bytes));
const scenarios = new Set(['success', 'no-resources', 'preparing', 'partial-failure', 'p1-offline', 'p2-offline', 'unconfigured']);
const terminal = new Set(['SUCCEEDED', 'PARTIAL_FAILED', 'FAILED', 'CANCELLED']);
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const inputMime = { png: 'image/png', jpg: 'image/jpeg', wav: 'audio/wav' };
const preparationId = randomUUID();
const artifacts = new Map();
const uploads = new Map();
const jobs = new Map();
const submissions = new Map();
let scenario = 'success';
let workerMode = 'ACTIVE';

const iso = (time = Date.now()) => new Date(time).toISOString();
const hash = (buffer) => createHash('sha256').update(buffer).digest('hex');

class Problem extends Error {
  constructor(status, code, message, retryable = false) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryable = retryable;
  }
}

function json(response, status, body) {
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  response.end(JSON.stringify(body));
}

function sendBytes(response, bytes, type, filename) {
  const headers = { 'Content-Type': type, 'Content-Length': bytes.length };
  if (filename) headers['Content-Disposition'] = `attachment; filename*=UTF-8''${encodeURIComponent(filename)}`;
  response.writeHead(200, headers);
  response.end(bytes);
}

function withTime(fixture) {
  return { ...fixture, server_time: iso() };
}

async function readBody(request, limit) {
  if (Number(request.headers['content-length']) > limit) {
    request.resume();
    throw new Problem(413, 'INPUT_TOO_LARGE', '上传内容超出限制。');
  }
  const chunks = [];
  let size = 0;
  for await (const chunk of request.iterator({ destroyOnReturn: false })) {
    size += chunk.length;
    if (size > limit) {
      request.resume();
      throw new Problem(413, 'INPUT_TOO_LARGE', '上传内容超出限制。');
    }
    chunks.push(chunk);
  }
  return Buffer.concat(chunks, size);
}

async function readJson(request) {
  const body = await readBody(request, 32768);
  try {
    return JSON.parse(body.toString('utf8'));
  } catch {
    throw new Problem(422, 'INVALID_REQUEST', '请发送有效的 JSON 对象。');
  }
}

function saveArtifact(bytes, name, mime, kind, media) {
  const id = randomUUID();
  const artifact = {
    artifact_id: id, kind, original_name: name, content_type: mime, sha256: hash(bytes),
    size_bytes: bytes.length, media_info: media, download_path: `${resourcePrefix}/artifacts/${id}/content`, created_at: iso(),
  };
  artifacts.set(id, { artifact, bytes });
  return artifact;
}

async function upload(request, response) {
  const bytes = await readBody(request, maxUpload + 65536);
  let form;
  try {
    form = await new Response(bytes, { headers: { 'Content-Type': request.headers['content-type'] || '' } }).formData();
  } catch {
    throw new Problem(422, 'INVALID_REQUEST', '上传应包含 multipart 的 upload_id 和 file。');
  }
  const id = form.get('upload_id');
  const file = form.get('file');
  if (typeof id !== 'string' || !uuidPattern.test(id) || !(file instanceof File) || [...form].length !== 2) {
    throw new Problem(422, 'INVALID_REQUEST', '上传应包含一个 UUID 和一个文件。');
  }
  const name = (file.name.split(/[/\\]/).at(-1) ?? '').replace(/[\u0000-\u001f\u007f]/g, '') || 'upload';
  if (name.length > 255) throw new Problem(422, 'INPUT_INVALID', '文件名超过 255 个字符。');
  const buffer = Buffer.from(await file.arrayBuffer());
  const previous = uploads.get(id.toLowerCase());
  if (previous) {
    if (previous.sha256 !== hash(buffer) || previous.original_name !== name || previous.content_type !== file.type) {
      throw new Problem(409, 'IDEMPOTENCY_CONFLICT', '相同 upload_id 已用于另一个文件。');
    }
    json(response, 200, previous);
    return;
  }
  const descriptor = configured.task_types.find((type) => type.input_mime_types.includes(file.type));
  if (!descriptor) throw new Problem(415, 'UNSUPPORTED_MEDIA_TYPE', '演示只接受 JPG、PNG 或 WAV。');
  if (buffer.length > descriptor.max_file_bytes) throw new Problem(413, 'INPUT_TOO_LARGE', '文件大小超出任务上限。');
  const media = inspectInput(buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.length), file.type, descriptor);
  const artifact = saveArtifact(buffer, name, file.type, 'INPUT', media);
  uploads.set(id.toLowerCase(), artifact);
  json(response, 201, artifact);
}

function activeExecution() {
  for (const record of jobs.values()) {
    const task = record.job.tasks.find(({ status }) => status === 'RUNNING' || status === 'CANCELLING');
    if (task) return task.current_execution_id;
  }
  return null;
}

function snapshot() {
  const now = iso();
  if (scenario === 'no-resources') return { contract_version: configured.contract_version, server_time: now, resources: [] };
  const executionId = activeExecution();
  const preparing = scenario === 'preparing' && workerMode !== 'PAUSED' && !executionId;
  const reserved = Number(Boolean(executionId || preparing));
  if (workerMode === 'DRAINING' && !reserved) workerMode = 'PAUSED';
  const unconfigured = scenario === 'unconfigured';
  const node = {
    ...worker,
    mode: workerMode,
    reserved_slots: reserved,
    available_slots: Number(!reserved && !unconfigured && workerMode === 'ACTIVE'),
    active_execution_ids: executionId ? [executionId] : [],
    active_preparation_id: preparing ? preparationId : null,
    last_seen_at: now,
    telemetry: { ...worker.telemetry, sampled_at: now },
    capabilities: capabilities.map((capability) => {
      if (preparing || unconfigured) {
        return {
          ...capability, state: preparing ? 'PREPARING' : 'NOT_PREPARED', reason: preparing ? 'MOCK 正在加载模型' : 'MOCK 模型配置待发布',
          verified: false, calibration: null, backend: null, runtime_provider: null, model_sha256: null,
          profile_digest: unconfigured ? null : capability.profile_digest, model_cached: false,
        };
      }
      return { ...capability, calibration: { ...capability.calibration, finished_at: now } };
    }),
  };
  return { contract_version: configured.contract_version, server_time: now, resources: [node] };
}

function summarize(job, now) {
  const counts = { total: job.tasks.length, queued: 0, waiting_for_resources: 0, dispatching: 0, running: 0, cancelling: 0, succeeded: 0, failed: 0, cancelled: 0 };
  for (const task of job.tasks) counts[task.status.toLowerCase()]++;
  job.counts = counts;
  if (counts.succeeded + counts.failed + counts.cancelled === counts.total) {
    job.status = job.cancel_requested ? 'CANCELLED' : counts.succeeded === counts.total ? 'SUCCEEDED' : counts.failed === counts.total ? 'FAILED' : 'PARTIAL_FAILED';
    job.finished_at = iso(now);
    job.download_path = `${taskPrefix}/jobs/${job.job_id}/download`;
  } else if (job.cancel_requested) job.status = 'CANCELLING';
  else if (counts.running) job.status = 'RUNNING';
  else if (counts.waiting_for_resources) job.status = 'WAITING_FOR_RESOURCES';
  else job.status = 'QUEUED';
}

function finishInput(record, task, now) {
  const dispatch = task.dispatches.at(-1);
  if (record.partial && task.input_index === 1) {
    task.status = 'FAILED';
    task.error = { ...fixtures.input_error, request_id: randomUUID() };
  } else {
    const media = task.input.media_info;
    const result = { ...fixtures[record.job.task_type === 'ocr.image.v1' ? 'ocr_result' : 'asr_result'], input_artifact_id: task.input.artifact_id };
    if (result.task_type === 'ocr.image.v1') {
      result.image_width = media.width;
      result.image_height = media.height;
      result.blocks = result.blocks.map((block) => ({
        ...block,
        bbox: block.bbox.map((value, index) => Math.min(value, index % 2 ? media.height : media.width)),
      }));
    } else {
      result.duration_ms = media.duration_ms;
      if (record.job.parameters.language !== 'auto') result.language = record.job.parameters.language;
      result.segments = result.segments.map((segment) => ({ ...segment, end_ms: Math.min(segment.end_ms, result.duration_ms) }));
    }
    record.results.set(task.task_id, result);
    task.result_artifact = saveArtifact(Buffer.from(JSON.stringify(result)), `${task.task_id}.json`, 'application/json', 'RESULT', emptyMedia);
    task.status = 'SUCCEEDED';
  }
  Object.assign(dispatch, { status: task.status, error: task.error, finished_at: iso(now) });
  task.current_execution_id = null;
  task.waiting_reason = null;
}

function cancelInput(task, now) {
  if (task.current_execution_id) Object.assign(task.dispatches.at(-1), { status: 'CANCELLED', finished_at: iso(now) });
  task.current_execution_id = null;
  task.status = 'CANCELLED';
  task.waiting_reason = null;
}

function startInput(job, task, now) {
  const id = randomUUID();
  task.status = 'RUNNING';
  task.current_execution_id = id;
  task.attempts_used = 1;
  task.waiting_reason = null;
  task.dispatches.push({ execution_id: id, attempt_no: 1, worker_id: worker.worker_id, status: 'RUNNING', accepted_at: iso(now), finished_at: null, error: null });
  job.started_at ??= iso(now);
}

function advanceJobs(now) {
  const dependencyDown = scenario === 'p1-offline';
  for (const record of jobs.values()) {
    const { job } = record;
    if (terminal.has(job.status)) continue;
    job.dependency_status = dependencyDown ? 'UNREACHABLE' : 'AVAILABLE';
    const running = job.tasks.find((task) => task.status === 'RUNNING' || task.status === 'CANCELLING');
    if (running) {
      if (dependencyDown) running.waiting_reason = 'MOCK P1 连接断开，等待对账';
      else if (running.status === 'CANCELLING' && now - record.cancelledAt >= 2200) cancelInput(running, now);
      else if (running.status === 'RUNNING' && now - record.startedInputAt >= 4000) finishInput(record, running, now);
      else running.waiting_reason = null;
    }
    const pending = job.tasks.filter((task) => ['QUEUED', 'WAITING_FOR_RESOURCES'].includes(task.status));
    if (pending.length && now - record.createdAt >= 600) {
      const ready = !dependencyDown && !['no-resources', 'preparing', 'unconfigured'].includes(scenario) && workerMode === 'ACTIVE';
      if (ready && !activeExecution()) {
        startInput(job, pending.shift(), now);
        record.startedInputAt = now;
      }
      const reason = dependencyDown ? 'MOCK P1 连接断开，等待资源服务恢复' : scenario === 'preparing' ? 'MOCK 正在准备模型' : ready ? 'MOCK 等待空闲槽位' : fixtures.job_waiting.tasks[0].waiting_reason;
      for (const task of pending) Object.assign(task, { status: 'WAITING_FOR_RESOURCES', waiting_reason: reason });
    }
    summarize(job, now);
  }
}

function createJob(payload, response, now) {
  const { client_request_id: id, task_type: type, profile_id: profile, profile_digest: digest, input_artifact_ids: ids, parameters } = payload ?? {};
  if (Object.keys(payload ?? {}).length !== 6 || !uuidPattern.test(id) || !Array.isArray(ids) || ids.length < 1 || ids.length > 200 ||
    !ids.every((artifactId) => typeof artifactId === 'string' && /^[\w-]{1,80}$/.test(artifactId)) || new Set(ids).size !== ids.length ||
    Object.keys(parameters ?? {}).length !== 1 || !['auto', 'zh', 'en'].includes(parameters?.language) ||
    typeof type !== 'string' || typeof profile !== 'string' || typeof digest !== 'string' || !/^[0-9a-f]{64}$/.test(digest)) {
    throw new Problem(422, 'INVALID_REQUEST', '任务请求缺少契约要求的字段或字段无效。');
  }
  const fingerprint = JSON.stringify([type, profile, digest, ids, parameters.language]);
  const previous = submissions.get(id.toLowerCase());
  if (previous) {
    if (previous.fingerprint !== fingerprint) throw new Problem(409, 'IDEMPOTENCY_CONFLICT', '相同提交 ID 已用于不同内容。');
    json(response, 200, previous.job);
    return;
  }
  if (scenario === 'p1-offline') throw new Problem(503, 'DEPENDENCY_UNAVAILABLE', fixtures.error_dependency.message, true);
  if (scenario === 'unconfigured') throw new Problem(503, 'PROFILE_UNCONFIGURED', fixtures.error_profile_unconfigured.message);
  const descriptor = configured.task_types.find((entry) => entry.task_type === type);
  if (!descriptor) throw new Problem(422, 'UNSUPPORTED_TASK_TYPE', '任务类型未知。');
  if (descriptor.profile_id !== profile || descriptor.profile_digest !== digest) throw new Problem(422, 'INPUT_INVALID', '执行配置已更改。');
  const inputs = ids.map((artifactId) => artifacts.get(artifactId)?.artifact);
  if (inputs.some((input) => input?.kind !== 'INPUT' || !descriptor.input_mime_types.includes(input.content_type)) ||
    new Set(inputs.map((input) => input.sha256)).size !== inputs.length) {
    throw new Problem(422, 'INPUT_INVALID', '输入文件不存在、格式不符或包含重复内容。');
  }
  const tasks = inputs.map((input, index) => ({
    task_id: randomUUID(), input_index: index, input, status: 'QUEUED', attempts_used: 0,
    current_execution_id: null, dispatches: [], result_artifact: null, waiting_reason: null, error: null,
  }));
  const job = {
    ...fixtures.job_succeeded_ocr, job_id: randomUUID(), client_request_id: id, task_type: type,
    profile_id: profile, profile_digest: digest, parameters, tasks, created_at: iso(now),
    started_at: null, finished_at: null, cancel_requested: false, dependency_status: 'AVAILABLE', download_path: null,
  };
  summarize(job, now);
  const record = { job, fingerprint, createdAt: now, results: new Map(), partial: scenario === 'partial-failure' };
  jobs.set(job.job_id, record);
  submissions.set(id.toLowerCase(), record);
  json(response, 201, job);
}

function resultsOf({ job, results }) {
  const items = job.tasks.map((task) => ({
    input_index: task.input_index, task_id: task.task_id, input_artifact_id: task.input.artifact_id,
    original_name: task.input.original_name, status: task.status, result: results.get(task.task_id) ?? null, error: task.error,
  }));
  return { job_id: job.job_id, task_type: job.task_type, status: job.status, items, combined_text: items.filter((item) => item.result).map((item) => item.result.text).join('\n\n') };
}

function jobDownload(response, record) {
  const { job } = record;
  if (!terminal.has(job.status)) throw new Problem(409, 'JOB_NOT_TERMINAL', '任务结束后可下载。');
  const results = resultsOf(record);
  const entries = [
    ['manifest.json', JSON.stringify({ mode: 'MOCK', ...job }, null, 2)],
    ['results.json', JSON.stringify(results, null, 2)],
    ['combined.txt', results.combined_text],
  ];
  for (const item of results.items) {
    if (item.result) entries.push([`results/${String(item.input_index).padStart(4, '0')}-${item.task_id}.json`, JSON.stringify(item.result, null, 2)]);
  }
  sendBytes(response, zip(entries), 'application/zip', `MOCK-${job.job_id}.zip`);
}

async function dispatch(request, response, path) {
  const { method } = request;
  const resource = path.startsWith(`${resourcePrefix}/`);
  const task = path.startsWith(`${taskPrefix}/`);
  if (path === '/mock/scenario' && method === 'POST') {
    const body = await readJson(request);
    if (!scenarios.has(body?.scenario)) throw new Problem(422, 'INVALID_REQUEST', '未知模拟场景。');
    scenario = body.scenario;
    json(response, 200, { scenario });
    return true;
  }
  if (path === '/mock/scenario' && method === 'GET') {
    json(response, 200, { scenario });
    return true;
  }
  const sample = /^\/samples\/(MOCK-[12]\.(png|jpg|wav))$/.exec(path);
  if (sample && method === 'GET') {
    const data = await readFile(new URL(`./samples/${sample[1]}`, import.meta.url));
    sendBytes(response, data, inputMime[sample[2]], sample[1]);
    return true;
  }
  if (!resource && !task) return false;
  if ((resource && scenario === 'p1-offline') || (task && scenario === 'p2-offline')) {
    request.resume();
    throw new Problem(503, 'DEPENDENCY_UNAVAILABLE', 'MOCK 服务连接断开。', true);
  }
  const now = Date.now();
  if (scenario !== 'p2-offline') advanceJobs(now);
  const route = path.slice((resource ? resourcePrefix : taskPrefix).length);
  if (route === '/healthz' && method === 'GET') json(response, 200, withTime(resource ? fixtures.health_p1 : fixtures.health_p2));
  else if (route === '/resources' && resource && method === 'GET') json(response, 200, snapshot());
  else if (route === '/task-types' && method === 'GET') {
    if (scenario === 'p1-offline') throw new Problem(503, 'DEPENDENCY_UNAVAILABLE', fixtures.error_dependency.message, true);
    json(response, 200, withTime(scenario === 'unconfigured' ? fixtures.catalog_unconfigured : configured));
  } else if (resource && route === '/artifacts' && method === 'POST') await upload(request, response);
  else if (task && route === '/jobs' && method === 'POST') createJob(await readJson(request), response, now);
  else {
    const artifact = resource && /^\/artifacts\/([\w-]{1,80})(\/content)?$/.exec(route);
    const node = resource && /^\/resources\/([\w-]{1,80})\/mode$/.exec(route);
    const job = task && /^\/jobs\/([a-f\d-]+)(?:\/(results|download|cancel))?$/i.exec(route);
    if (artifact && method === 'GET') {
      const file = artifacts.get(artifact[1]);
      if (!file) throw new Problem(404, 'NOT_FOUND', '演示文件已不存在。');
      if (artifact[2]) sendBytes(response, file.bytes, file.artifact.content_type, file.artifact.original_name);
      else json(response, 200, file.artifact);
    } else if (node && method === 'POST') {
      const body = await readJson(request);
      if (node[1] !== worker.worker_id || scenario === 'no-resources') throw new Problem(404, 'NOT_FOUND', '该演示设备不存在。');
      if (Object.keys(body ?? {}).length !== 1 || !['ACTIVE', 'DRAINING'].includes(body?.mode)) throw new Problem(422, 'INVALID_REQUEST', '设备模式无效。');
      if (body.mode === 'ACTIVE' || workerMode !== 'PAUSED') workerMode = body.mode;
      json(response, 200, snapshot().resources[0]);
    } else if (job && uuidPattern.test(job[1]) && ((job[2] === 'cancel') === (method === 'POST')) && ['POST', 'GET'].includes(method)) {
      const record = jobs.get(job[1].toLowerCase());
      if (!record) throw new Problem(404, 'NOT_FOUND', '任务不存在于当前演示进程。');
      if (job[2] === 'results') json(response, 200, resultsOf(record));
      else if (job[2] === 'download') jobDownload(response, record);
      else if (job[2] === 'cancel' && !terminal.has(record.job.status)) {
        if (!record.job.cancel_requested) {
          record.cancelledAt = now;
          record.job.cancel_requested = true;
          for (const input of record.job.tasks) {
            if (terminal.has(input.status)) continue;
            if (!input.current_execution_id) cancelInput(input, now);
            else {
              input.status = 'CANCELLING';
              input.dispatches.at(-1).status = 'CANCEL_REQUESTED';
            }
          }
          summarize(record.job, now);
        }
        json(response, record.job.status === 'CANCELLING' ? 202 : 200, record.job);
      } else json(response, 200, record.job);
    } else throw new Problem(404, 'NOT_FOUND', '该模拟接口不存在。');
  }
  return true;
}

export async function handleMock(request, response, path) {
  try {
    return await dispatch(request, response, path);
  } catch (error) {
    const status = error.status ?? (error instanceof InputError ? (error.code === 'INPUT_TOO_LARGE' ? 413 : 422) : 500);
    json(response, status, {
      code: error.code ?? 'INTERNAL_ERROR',
      message: status === 500 ? 'MOCK 接口内部错误。' : error.message,
      retryable: error.retryable ?? false, request_id: randomUUID(), details: {},
    });
    return true;
  }
}
