import { createServer, request as httpRequest } from 'node:http';
import { request as httpsRequest } from 'node:https';
import { readFile } from 'node:fs/promises';
import { loadEnvFile } from 'node:process';
import { randomUUID } from 'node:crypto';

try {
  loadEnvFile(new URL('./.env', import.meta.url));
} catch (error) {
  if (error.code !== 'ENOENT') throw error;
}

const mockMode = process.argv.includes('--mock');
const mock = mockMode ? (await import('./mock/handler.js')).handleMock : null;
const host = process.env.WEB_HOST || '127.0.0.1';
const port = Number(process.env.WEB_PORT || 5173);
const demoToken = process.env.DEMO_TOKEN?.trim();

function readBaseUrl(name) {
  const value = process.env[name];
  if (mockMode || !value?.trim()) return null;
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`${name} must be an HTTP origin.`);
  }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password ||
    url.pathname !== '/' || url.search || url.hash) {
    throw new Error(`${name} must contain only the protocol, host and port.`);
  }
  return url.origin;
}

const resourceBaseUrl = readBaseUrl('RESOURCE_BASE_URL');
const taskBaseUrl = readBaseUrl('TASK_BASE_URL');
const resourceId = '[A-Za-z0-9_-]{1,80}';
const jobId = '[a-fA-F0-9-]{36}';
const resourceRoutes = {
  GET: new RegExp(`^/api/resource/v1/(healthz|resources|artifacts/${resourceId}(/content)?)$`),
  POST: new RegExp(`^/api/resource/v1/(artifacts|resources/${resourceId}/mode)$`),
};
const taskRoutes = {
  GET: new RegExp(`^/api/task/v1/(healthz|task-types|jobs/${jobId}(/(results|download))?)$`),
  POST: new RegExp(`^/api/task/v1/jobs(/${jobId}/cancel)?$`),
};

const staticFiles = new Map([
  ['/', ['index.html', 'text/html; charset=utf-8']],
  ['/styles.css', ['styles.css', 'text/css; charset=utf-8']],
  ['/app.js', ['app.js', 'text/javascript; charset=utf-8']],
  ['/api.js', ['api.js', 'text/javascript; charset=utf-8']],
  ['/media.js', ['media.js', 'text/javascript; charset=utf-8']],
  ['/render.js', ['render.js', 'text/javascript; charset=utf-8']],
]);

function problem(response, status, code, message, retryable = false) {
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  response.end(JSON.stringify({ code, message, retryable, request_id: randomUUID(), details: {} }));
}

function proxy(request, response, path, baseUrl, service) {
  if (!baseUrl) {
    problem(response, 503, 'DEPENDENCY_UNAVAILABLE', `${service} 地址尚未配置。`, true);
    return;
  }
  const target = new URL(path, baseUrl);
  const send = target.protocol === 'https:' ? httpsRequest : httpRequest;
  const headers = {};
  for (const name of ['accept', 'content-type', 'content-length']) {
    if (request.headers[name] !== undefined) headers[name] = request.headers[name];
  }
  // Forward only the browser's demo credential or the server's DEMO_TOKEN.
  if (!path.endsWith('/healthz')) {
    const authorization = request.headers.authorization || (demoToken && `Bearer ${demoToken}`);
    if (authorization) headers.authorization = authorization;
  }
  const timeout = /\/(artifacts|content|download)$/.test(path) ? 60000 : 10000;
  const upstream = send(target, { method: request.method, headers, timeout }, (reply) => {
    if (reply.statusCode >= 300 && reply.statusCode < 400) {
      reply.resume();
      problem(response, 502, 'DEPENDENCY_UNAVAILABLE', `${service} 返回了重定向。`, true);
      return;
    }
    const replyHeaders = {};
    for (const name of ['content-type', 'content-length', 'content-encoding', 'content-disposition']) {
      if (reply.headers[name] !== undefined) replyHeaders[name] = reply.headers[name];
    }
    response.writeHead(reply.statusCode, replyHeaders);
    reply.on('error', () => response.destroy());
    reply.pipe(response);
  });
  upstream.on('timeout', () => upstream.destroy(new Error('Upstream timeout')));
  upstream.on('error', () => {
    if (response.destroyed) return;
    if (response.headersSent) response.destroy();
    else problem(response, 502, 'DEPENDENCY_UNAVAILABLE', `${service} 暂时不可达。`, true);
  });
  response.on('close', () => {
    if (!response.writableFinished) upstream.destroy();
  });
  request.pipe(upstream);
}

const server = createServer(async (request, response) => {
  response.setHeader('Cache-Control', 'no-store');
  response.setHeader('X-Content-Type-Options', 'nosniff');
  response.setHeader('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'");
  try {
    const authority = request.headers.host;
    if (!authority || ((host === '127.0.0.1' || host === 'localhost') && !/^(?:localhost|127\.0\.0\.1)(?::\d+)?$/.test(authority))) {
      problem(response, 421, 'INVALID_REQUEST', '请求的站点地址不匹配。');
      return;
    }
    const origin = request.headers.origin;
    if (request.headers['sec-fetch-site'] === 'cross-site' ||
      (request.method === 'POST' && origin && origin !== `http://${authority}` && origin !== `https://${authority}`)) {
      problem(response, 403, 'FORBIDDEN', '请从同源工作台发起请求。');
      return;
    }
    const path = new URL(request.url, 'http://localhost').pathname;
    const file = staticFiles.get(path);
    if (request.method === 'GET' && file) {
      const [filename, type] = file;
      const content = await readFile(new URL(`./public/${filename}`, import.meta.url));
      response.writeHead(200, { 'Content-Type': type });
      response.end(content);
    } else if (request.method === 'GET' && path === '/config') {
      response.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
      response.end(JSON.stringify({ mock: mockMode }));
    } else if (mock && await mock(request, response, path)) {
      return;
    } else if (resourceRoutes[request.method]?.test(path)) {
      proxy(request, response, path, resourceBaseUrl, 'P1 资源服务');
    } else if (taskRoutes[request.method]?.test(path)) {
      proxy(request, response, path, taskBaseUrl, 'P2 任务服务');
    } else {
      problem(response, 404, 'NOT_FOUND', '该地址不存在。');
    }
  } catch {
    if (response.headersSent) response.destroy();
    else problem(response, 500, 'INTERNAL_ERROR', '工作台暂时无法处理请求。', true);
  }
});

server.listen(port, host, () => {
  const address = server.address();
  const displayHost = ['0.0.0.0', '::'].includes(address.address) ? '127.0.0.1' : address.address;
  const authority = displayHost.includes(':') ? `[${displayHost}]` : displayHost;
  console.log(`Edge Fabric ${mockMode ? 'MOCK' : 'LIVE'} http://${authority}:${address.port}/`);
});
