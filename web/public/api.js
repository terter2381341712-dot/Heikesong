import { t } from './i18n.js';
const RESOURCE = '/api/resource/v1';
const TASK = '/api/task/v1';
const filenames = {
  'image/png': '.png', 'image/jpeg': '.jpg', 'audio/wav': '.wav',
};
let demoToken = '';

export const CONTRACT_VERSION = '0.2.0';

export function setDemoToken(value) {
  demoToken = value.trim();
}

export function requestDeadline(signal, timeout) {
  const controller = new AbortController();
  const abort = () => controller.abort(signal.reason);
  if (signal?.aborted) abort();
  else signal?.addEventListener('abort', abort, { once: true });
  const timer = setTimeout(() => controller.abort(new DOMException('Request timed out', 'TimeoutError')), timeout);
  return {
    signal: controller.signal,
    release() {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
    },
  };
}

export class ApiError extends Error {
  constructor(service, status, message, code, requestId) {
    super(message);
    this.service = service;
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }

  get uncertain() {
    return !this.status || this.status >= 500 || this.status === 408;
  }
}

export function describeError(error) {
  const service = { P1: t('P1 资源服务', 'P1 Resource service'), P2: t('P2 任务服务', 'P2 Task service') }[error.service];
  if (!service) return error.message;
  const request = error.requestId ? t(`（请求 ${error.requestId}）`, ` (request ${error.requestId})`) : '';
  return `${service} · ${error.message}${request}`;
}

function fromResponse(service, status, problem) {
  const code = problem?.code;
  let message = problem?.message || t(`返回 HTTP ${status}`, `HTTP ${status}`);
  if (status === 401 || status === 403) message = t('请在连接设置中检查 DEMO_TOKEN。', 'Check DEMO_TOKEN in Connection settings.');
  return new ApiError(service, status, message, code, problem?.request_id);
}

function fromConnection(service, error) {
  if (error instanceof ApiError || error.name === 'AbortError') return error;
  const message = error.name === 'TimeoutError' ? t('请求超时。', 'The request timed out.') : t('连接已断开，请检查服务。', 'The connection was lost. Check the service.');
  return new ApiError(service, 0, message);
}

async function request(service, path, options = {}) {
  const { body, method = 'GET', signal, binary, timeout = 10000, token = demoToken } = options;
  const deadline = requestDeadline(signal, timeout);
  const headers = {};
  if (service && token && !path.endsWith('/healthz')) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  try {
    const response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: deadline.signal,
      redirect: 'error',
      cache: 'no-store',
    });
    if (!response.ok) {
      const problem = await response.json().catch(() => null);
      throw fromResponse(service, response.status, problem);
    }
    if (binary) return await response.blob();
    try {
      return await response.json();
    } catch (error) {
      if (deadline.signal.aborted) throw deadline.signal.reason;
      throw new ApiError(service, response.status, t('响应不是契约定义的 JSON。', 'The response is not the JSON specified by the API contract.'), 'INVALID_RESPONSE');
    }
  } catch (error) {
    // Preserve an explicit page-switch abort and distinguish it from a timeout.
    if (signal?.aborted) throw signal.reason;
    throw fromConnection(service, error);
  } finally {
    deadline.release();
  }
}

function verifyVersion(service, response) {
  if (response?.contract_version !== CONTRACT_VERSION) {
    throw new ApiError(service, 200, t(`接口契约应为 ${CONTRACT_VERSION}，实际为 ${response?.contract_version ?? 'N/A'}。`, `Expected API contract ${CONTRACT_VERSION}, received ${response?.contract_version ?? 'N/A'}.`), 'CONTRACT_VERSION');
  }
  return response;
}

async function catalog(service, path, signal, arrayField, token) {
  const response = verifyVersion(service, await request(service, path, { signal, token }));
  if (!Array.isArray(response[arrayField])) {
    throw new ApiError(service, 200, t(`响应缺少 ${arrayField} 数组。`, `The response is missing the ${arrayField} array.`), 'INVALID_RESPONSE');
  }
  return response;
}

async function health(service, path, expected, signal) {
  const response = verifyVersion(service, await request(service, path, { signal }));
  if (response.service !== expected || response.status !== 'ok') {
    throw new ApiError(service, 200, t('健康检查的服务标识或状态不匹配。', 'The health check returned an unexpected service or status.'), 'INVALID_RESPONSE');
  }
  return response;
}

function upload(file, uploadId, mime, onProgress) {
  return new Promise((resolve, reject) => {
    const body = new FormData();
    body.append('upload_id', uploadId);
    const uploadFile = file.type === mime ? file : new File([file], file.name, { type: mime });
    body.append('file', uploadFile);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${RESOURCE}/artifacts`);
    xhr.timeout = 60000;
    xhr.responseType = 'json';
    if (demoToken) xhr.setRequestHeader('Authorization', `Bearer ${demoToken}`);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(fromResponse('P1', xhr.status, xhr.response));
      } else if (!xhr.response?.artifact_id || !xhr.response?.sha256) {
        reject(new ApiError('P1', xhr.status, t('上传响应缺少文件 ID 或 SHA-256。', 'The upload response is missing a file ID or SHA-256 hash.'), 'INVALID_RESPONSE'));
      } else {
        resolve(xhr.response);
      }
    };
    xhr.onerror = () => reject(new ApiError('P1', 0, t('上传连接已断开。', 'The upload connection was lost.')));
    xhr.ontimeout = () => reject(new ApiError('P1', 0, t('上传超时，重试将沿用原上传 ID。', 'The upload timed out. Retrying uses the original upload ID.')));
    xhr.send(body);
  });
}

function jobPath(id) {
  return `${TASK}/jobs/${encodeURIComponent(id)}`;
}

export const resourceApi = {
  health: (signal) => health('P1', `${RESOURCE}/healthz`, 'p1_resource', signal),
  snapshot: (signal) => catalog('P1', `${RESOURCE}/resources`, signal, 'resources'),
  performance: async (signal) => verifyVersion('P1', await request('P1', `${RESOURCE}/performance`, { signal })),
  upload,
  mode: (id, mode) => request('P1', `${RESOURCE}/resources/${encodeURIComponent(id)}/mode`, {
    method: 'POST', body: { mode },
  }),
  download: (artifact) => request('P1', `${RESOURCE}/artifacts/${encodeURIComponent(artifact.artifact_id)}/content`, {
    binary: true, timeout: 60000,
  }),
};

export const taskApi = {
  health: (signal) => health('P2', `${TASK}/healthz`, 'p2_task', signal),
  types: (signal) => catalog('P2', `${TASK}/task-types`, signal, 'task_types'),
  create: (payload) => request('P2', `${TASK}/jobs`, { method: 'POST', body: payload }),
  job: (id, signal) => request('P2', jobPath(id), { signal }),
  results: (id, signal) => request('P2', `${jobPath(id)}/results`, { signal }),
  cancel: (id) => request('P2', `${jobPath(id)}/cancel`, { method: 'POST' }),
  download: (id) => request('P2', `${jobPath(id)}/download`, { binary: true, timeout: 60000 }),
};

export const readConfig = () => request(null, '/config');
export async function verifyWorkbenchAccess(token, signal) {
  const [resources, types] = await Promise.allSettled([
    catalog('P1', `${RESOURCE}/resources`, signal, 'resources', token),
    catalog('P2', `${TASK}/task-types`, signal, 'task_types', token),
  ]);
  return { P1: resources, P2: types };
}
export const setScenario = (scenario) => request(null, '/mock/scenario', { method: 'POST', body: { scenario } });
export const getScenario = () => request(null, '/mock/scenario');

export async function demoFiles(mime, count) {
  const extension = filenames[mime];
  if (!extension) throw new Error(t('此类型没有演示文件。', 'No sample files are available for this task type.'));
  return Promise.all(Array.from({ length: count }, async (_, index) => {
    const name = `MOCK-${index + 1}${extension}`;
    const blob = await request(null, `/samples/${name}`, { binary: true });
    return new File([blob], name, { type: mime });
  }));
}

export function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
