import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';

test('workbench serves metrics module and proxies performance GET/POST', {timeout: 15000}, async () => {
  const received = [];
  const upstream = createServer(async (request, response) => {
    let body = '';
    for await (const chunk of request) body += chunk;
    received.push({method: request.method, path: request.url, auth: request.headers.authorization, body});
    response.writeHead(200, {'Content-Type': 'application/json'});
    response.end(JSON.stringify({devices: {}, formal: null}));
  });
  upstream.listen(0, '127.0.0.1');
  await once(upstream, 'listening');
  const process = spawn(globalThis.process.execPath, ['server.js'], {
    cwd: new URL('../', import.meta.url),
    env: {...globalThis.process.env, WEB_HOST: '127.0.0.1', WEB_PORT: '0',
      P4_MANAGED_WORKBENCH: '0', DEMO_TOKEN: 'fixture-user',
      RESOURCE_BASE_URL: `http://127.0.0.1:${upstream.address().port}`, TASK_BASE_URL: ''},
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  try {
    const origin = await new Promise((resolve, reject) => {
      let output = '';
      process.stdout.on('data', chunk => {
        output += chunk;
        const match = output.match(/http:\/\/127\.0\.0\.1:\d+/);
        if (match) resolve(match[0]);
      });
      process.on('error', reject);
      process.on('exit', code => reject(new Error(`Web exited ${code}`)));
    });
    const module = await fetch(origin + '/performance.js');
    assert.equal(module.status, 200);
    assert.match(await module.text(), /performancePanel/);
    assert.equal((await fetch(origin + '/api/resource/v1/performance?job_id=selected-job')).status, 200);
    const body = JSON.stringify({reports: [{fixture: true}]});
    assert.equal((await fetch(origin + '/api/resource/v1/performance', {
      method: 'POST', headers: {'Content-Type': 'application/json', Origin: origin}, body,
    })).status, 200);
    assert.equal(received.length, 2);
    assert.equal(received[1].body, body);
    assert.equal(received[0].auth, 'Bearer fixture-user');
    assert.equal(received[0].path, '/api/resource/v1/performance?job_id=selected-job');
    assert.equal(received[1].path, '/api/resource/v1/performance');
  } finally {
    process.kill();
    await once(process, 'exit');
    upstream.closeAllConnections();
    await new Promise(resolve => upstream.close(resolve));
  }
});
