import { createServer, request as httpRequest } from 'node:http';
import { request as httpsRequest } from 'node:https';
import { createReadStream } from 'node:fs';
import { readFile, stat } from 'node:fs/promises';
import { loadEnvFile } from 'node:process';
import { randomUUID } from 'node:crypto';
import { Readable } from 'node:stream';

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
const controlPort = Number(process.env.P4_CONTROL_PORT || 8003);
const controlOrigin = Number.isInteger(controlPort) && controlPort > 0 && controlPort <= 65535
  ? `http://127.0.0.1:${controlPort}` : null;
const controlRoutes = {
  GET: new Set(['availability', 'status', 'result.zip']),
  POST: new Set(['center', 'connect', 'join', 'reconnect', 'discover', 'code', 'on', 'off', 'test', 'stop', 'workbench-token']),
};
const controlPrefix = '/api/p4/';
const managedWorkbench = process.env.P4_MANAGED_WORKBENCH === '1';

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
const uuid = '[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}';
const resourceRoutes = {
  GET: new RegExp(`^/api/resource/v1/(healthz|resources|artifacts/${resourceId}(/content)?)$`),
  POST: new RegExp(`^/api/resource/v1/(artifacts|resources/${resourceId}/mode)$`),
};
const taskRoutes = {
  GET: new RegExp(`^/api/task/v1/(healthz|task-types|jobs/${uuid}(/(results|download))?)$`),
  POST: new RegExp(`^/api/task/v1/jobs(/${uuid}/cancel)?$`),
};
const nodePrefix = '/api/resource/internal/v1/nodes';
const nodeActions = `heartbeat|claim|confirm_idle|sharing|preparations/${uuid}/status|executions/${uuid}/(complete|cancel_ack)`;
const nodeRoutes = {
  GET: new RegExp(`^${nodePrefix}/${resourceId}/artifacts/${resourceId}/content$`),
  POST: new RegExp(`^${nodePrefix}/(register|${resourceId}/(${nodeActions}))$`),
};

const staticFiles = new Map([
  ['/', ['index.html', 'text/html; charset=utf-8']],
  ['/styles.css', ['styles.css', 'text/css; charset=utf-8']],
  ['/workflow.css', ['workflow.css', 'text/css; charset=utf-8']],
  ['/workflow.js', ['workflow.js', 'text/javascript; charset=utf-8']],
  ['/assets/anthropic-sans-normal.woff2', ['assets/anthropic-sans-normal.woff2', 'font/woff2']],
  ['/assets/anthropic-mono-normal.woff2', ['assets/anthropic-mono-normal.woff2', 'font/woff2']],
  ['/assets/noto-sans-sc-home.woff2', ['assets/noto-sans-sc-home.woff2', 'font/woff2']],
  ['/assets/ether-node.png', ['assets/ether-node.png', 'image/png']],
  ['/assets/ether-node-small.webp', ['assets/ether-node-small.webp', 'image/webp']],
  ['/app.js', ['app.js', 'text/javascript; charset=utf-8']],
  ['/i18n.js', ['i18n.js', 'text/javascript; charset=utf-8']],
  ['/api.js', ['api.js', 'text/javascript; charset=utf-8']],
  ['/media.js', ['media.js', 'text/javascript; charset=utf-8']],
  ['/render.js', ['render.js', 'text/javascript; charset=utf-8']],
  ['/device-control.js', ['device-control.js', 'text/javascript; charset=utf-8']],
]);
const workflowVideo = new URL('./public/assets/ether-node.mp4', import.meta.url);

async function serveWorkflowVideo(request, response) {
  const { size } = await stat(workflowVideo);
  const headers = { 'Content-Type': 'video/mp4', 'Accept-Ranges': 'bytes', 'Cache-Control': 'no-cache' };
  let start = 0;
  let end = size - 1;
  let status = 200;
  if (request.headers.range) {
    const match = /^bytes=(\d*)-(\d*)$/.exec(request.headers.range);
    const firstByte = match?.[1];
    const lastByte = match?.[2];
    start = firstByte ? Number(firstByte) : Math.max(0, size - Number(lastByte));
    if (firstByte && lastByte) end = Math.min(end, Number(lastByte));
    if ((!firstByte && !lastByte) || start >= size || end < start) {
      response.writeHead(416, { ...headers, 'Content-Range': `bytes */${size}` });
      response.end();
      return;
    }
    status = 206;
    headers['Content-Range'] = `bytes ${start}-${end}/${size}`;
  }
  response.writeHead(status, { ...headers, 'Content-Length': end - start + 1 });
  if (request.method === 'HEAD') {
    response.end();
    return;
  }
  const stream = createReadStream(workflowVideo, { start, end });
  stream.on('error', () => response.destroy());
  response.on('close', () => stream.destroy());
  stream.pipe(response);
}

function problem(response, status, code, message, retryable = false) {
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  response.end(JSON.stringify({ code, message, retryable, request_id: randomUUID(), details: {} }));
}

function localOperator(request) {
  const serverPort = request.socket.localPort;
  const authority = request.headers.host;
  const origin = request.headers.origin;
  return ['127.0.0.1', '::ffff:127.0.0.1', '::1'].includes(request.socket.remoteAddress) &&
    [`127.0.0.1:${serverPort}`, `localhost:${serverPort}`, `[::1]:${serverPort}`].includes(authority) &&
    (!origin || origin === `http://${authority}`) &&
    !['same-site', 'cross-site'].includes(request.headers['sec-fetch-site']) &&
    !request.headers.forwarded && !request.headers['x-forwarded-for'] &&
    !request.headers['x-forwarded-host'] && !request.headers['x-real-ip'];
}

async function control(request, response, action) {
  const json = (data) => {
    response.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
    response.end(JSON.stringify(data));
  };
  const local = localOperator(request);
  const enabled = local && !mockMode && Boolean(controlOrigin);
  if (action === 'availability' && !enabled) {
    json({ available: false, reason: mockMode ? 'mock' : local ? 'offline' : 'remote' });
    return;
  }
  if (!enabled) {
    problem(response, 403, 'FORBIDDEN', '此操作需要本机 P4 控制台。');
    return;
  }
  try {
    const init = { redirect: 'error', signal: AbortSignal.timeout(action === 'availability' ? 2000 : 10000) };
    const handshake = await fetch(controlOrigin + '/api/bridge-session', {
      ...init, headers: { 'X-P4-Bridge': '1' },
    });
    const session = handshake.ok && (await handshake.json()).session;
    if (!session) throw new Error('P4 control session unavailable');
    if (action === 'availability') {
      json({ available: true, localUrl: controlOrigin + '/' });
      return;
    }
    const headers = { 'X-P4-Session': session };
    let body;
    if (request.method === 'POST') {
      const chunks = [];
      let size = 0;
      for await (const chunk of request) {
        size += chunk.length;
        if (size > 8192) {
          problem(response, 413, 'INVALID_REQUEST', '请求过大。');
          return;
        }
        chunks.push(chunk);
      }
      body = Buffer.concat(chunks);
      headers['Content-Type'] = 'application/json';
    }
    const reply = await fetch(controlOrigin + '/api/' + action, { ...init, method: request.method, headers, body });
    if (action === 'result.zip' && reply.ok) {
      response.writeHead(reply.status, { 'Content-Type': 'application/zip', 'Content-Disposition': 'attachment; filename="p4-results.zip"' });
      const stream = Readable.fromWeb(reply.body);
      stream.on('error', () => response.destroy());
      response.on('close', () => stream.destroy());
      stream.pipe(response);
      return;
    }
    const data = await reply.text();
    response.writeHead(reply.status, { 'Content-Type': 'application/json; charset=utf-8' });
    response.end(data);
  } catch {
    if (response.headersSent) response.destroy();
    else if (action === 'availability') json({ available: false, reason: 'offline', localUrl: controlOrigin + '/' });
    else problem(response, 503, 'DEPENDENCY_UNAVAILABLE', '本机 P4 控制台暂不可达。', true);
  }
}

function proxy(request, response, path, baseUrl, service, fallbackToken) {
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
  if (!path.endsWith('/healthz')) {
    const authorization = request.headers.authorization || (fallbackToken && `Bearer ${fallbackToken}`);
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

async function upstreamFor(request, service) {
  if (!managedWorkbench) return service === 'resource' ? resourceBaseUrl : taskBaseUrl;
  if (!controlOrigin) return null;
  try {
    const reply = await fetch(controlOrigin + '/api/bridge-upstreams', {
      redirect: 'error', signal: AbortSignal.timeout(1000), headers: { 'X-P4-Bridge': '1' },
    });
    if (reply.ok) {
      const upstreams = await reply.json();
      return upstreams.lan || localOperator(request) ? upstreams[service] : null;
    }
  } catch {
    // The shell is available while the controller or its services are offline.
  }
  return null;
}

const server = createServer(async (request, response) => {
  response.setHeader('Cache-Control', 'no-store');
  response.setHeader('X-Content-Type-Options', 'nosniff');
  response.setHeader('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'");
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
    if (path === '/assets/ether-node.mp4' && ['GET', 'HEAD'].includes(request.method)) {
      await serveWorkflowVideo(request, response);
    } else if (request.method === 'GET' && file) {
      const [filename, type] = file;
      const content = await readFile(new URL(`./public/${filename}`, import.meta.url));
      response.writeHead(200, { 'Content-Type': type });
      response.end(content);
    } else if (request.method === 'GET' && path === '/config') {
      response.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
      response.end(JSON.stringify({ mock: mockMode, managed: managedWorkbench }));
    } else if (path.startsWith(controlPrefix) && controlRoutes[request.method]?.has(path.slice(controlPrefix.length))) {
      await control(request, response, path.slice(controlPrefix.length));
    } else if (mock && await mock(request, response, path)) {
      return;
    } else if (resourceRoutes[request.method]?.test(path)) {
      proxy(request, response, path, await upstreamFor(request, 'resource'), 'P1 资源服务', demoToken);
    } else if (taskRoutes[request.method]?.test(path)) {
      proxy(request, response, path, await upstreamFor(request, 'task'), 'P2 任务服务', demoToken);
    } else if (!mockMode && nodeRoutes[request.method]?.test(path)) {
      proxy(request, response, path, await upstreamFor(request, 'resource'), 'P1 资源服务');
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
