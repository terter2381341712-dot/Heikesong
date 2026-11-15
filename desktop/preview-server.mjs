// Loopback-only static server for previewing the status page outside Electron.
// Not part of the app; used to check layout without launching the real shell.
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';

const port = Number(process.env.PORT || 8931);
const types = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.svg': 'image/svg+xml',
};

createServer(async (request, response) => {
  const name = normalize(new URL(request.url, 'http://localhost').pathname).replace(/^\/+/, '') || 'status.html';
  try {
    const body = await readFile(join(import.meta.dirname, 'renderer', name));
    response.writeHead(200, { 'Content-Type': types[extname(name)] || 'application/octet-stream' });
    response.end(body);
  } catch {
    response.writeHead(404).end();
  }
}).listen(port, '127.0.0.1', () => console.log(`status preview on http://127.0.0.1:${port}/`));
