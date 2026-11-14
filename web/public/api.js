const RESOURCE = '/api/resource/v1';
const TASK = '/api/task/v1';
const labels = { P1: 'P1 资源服务', P2: 'P2 任务服务' };
const filenames = {
  'image/png': '.png', 'image/jpeg': '.jpg', 'audio/wav': '.wav',
};
let demoToken = '';

export const CONTRACT_VERSION = '0.2.0';

export function setDemoToken(value) {
  demoToken = value.trim();
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
  const service = labels[error.service];
  if (!service) return error.message;
  const request = error.requestId ? `（请求 ${error.requestId}）` : '';
  return `${service} · ${error.message}${request}`;
}

function fromResponse(service, status, problem) {
  const code = problem?.code;
  let message = problem?.message || `返回 HTTP ${status}`;
  if (status === 401 || status === 403) message = '请在连接设置中检查 DEMO_TOKEN。';
  return new ApiError(service, status, message, code, problem?.request_id);
}

function fromConnection(service, error) {
  if (error instanceof ApiError || error.name === 'AbortError') return error;
  const message = error.name === 'TimeoutError' ? '请求超时。' : '连接已断开，请检查服务。';
  return new ApiError(service, 0, message);
}

async function request(service, path, options = {}) {
  const { body, method = 'GET', signal, binary, timeout = 10000 } = options;
  const requestTimeout = AbortSignal.timeout(timeout);
  const combinedSignal = signal ? AbortSignal.any([signal, requestTimeout]) : requestTimeout;
  const headers = {};
  if (service && demoToken && !path.endsWith('/healthz')) headers.Authorization = `Bearer ${demoToken}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  try {
    const response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: combinedSignal,
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
      if (combinedSignal.aborted) throw combinedSignal.reason;
      throw new ApiError(service, response.status, '响应不是契约定义的 JSON。', 'INVALID_RESPONSE');
    }
  } catch (error) {
    // Preserve an explicit page-switch abort and distinguish it from a timeout.
    if (signal?.aborted) throw signal.reason;
    throw fromConnection(service, error);
  }
}

function verifyVersion(service, response) {
  if (response?.contract_version !== CONTRACT_VERSION) {
    throw new ApiError(service, 200, `接口契约应为 ${CONTRACT_VERSION}，实际为 ${response?.contract_version ?? 'N/A'}。`, 'CONTRACT_VERSION');
  }
  return response;
}

async function catalog(service, path, signal, arrayField) {
  const response = verifyVersion(service, await request(service, path, { signal }));
  if (!Array.isArray(response[arrayField])) {
    throw new ApiError(service, 200, `响应缺少 ${arrayField} 数组。`, 'INVALID_RESPONSE');
  }
  return response;
}

async function health(service, path, expected, signal) {
  const response = verifyVersion(service, await request(service, path, { signal }));
  if (response.service !== expected || response.status !== 'ok') {
    throw new ApiError(service, 200, '健康检查的服务标识或状态不匹配。', 'INVALID_RESPONSE');
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
        reject(new ApiError('P1', xhr.status, '上传响应缺少文件 ID 或 SHA-256。', 'INVALID_RESPONSE'));
      } else {
        resolve(xhr.response);
      }
    };
    xhr.onerror = () => reject(new ApiError('P1', 0, '上传连接已断开。'));
    xhr.ontimeout = () => reject(new ApiError('P1', 0, '上传超时，重试将沿用原上传 ID。'));
    xhr.send(body);
  });
}

function jobPath(id) {
  return `${TASK}/jobs/${encodeURIComponent(id)}`;
}

export const resourceApi = {
  health: (signal) => health('P1', `${RESOURCE}/healthz`, 'p1_resource', signal),
  snapshot: (signal) => catalog('P1', `${RESOURCE}/resources`, signal, 'resources'),
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
export const setScenario = (scenario) => request(null, '/mock/scenario', { method: 'POST', body: { scenario } });
export const getScenario = () => request(null, '/mock/scenario');

export async function demoFiles(mime, count) {
  const extension = filenames[mime];
  if (!extension) throw new Error('此类型没有演示文件。');
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
