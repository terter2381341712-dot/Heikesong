'use strict';

// Edge Fabric desktop shell.
//
// Responsibilities, in order:
//   1. show a status window while the private runtime is prepared
//   2. run P4/scripts/bootstrap.* (which prepares the runtime and then starts
//      control.py, which in turn starts the workbench on port 5173)
//   3. load the workbench into this window once it answers
//   4. stop every child service on quit
//
// The center itself is still started by the user from the workbench, exactly as
// before. This shell replaces the terminal window, nothing else.

const { app, BrowserWindow, ipcMain, shell } = require('electron');
const { spawn, execFile } = require('node:child_process');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');

const isWindows = process.platform === 'win32';
const CONTROL_PORT = 8003;
const WEB_PORT = 5173;

let window = null;
let child = null;
let quitting = false;
let logTail = [];

/** Repo root: next to the app in development, under resources/app when packaged. */
function repoRoot() {
  if (app.isPackaged) return path.join(process.resourcesPath, 'app');
  return path.resolve(__dirname, '..');
}

/** Mirrors managed_runtime.default_directory() so both agree on one location. */
function runtimeDirectory() {
  if (process.env.P4_RUNTIME_DIR) return process.env.P4_RUNTIME_DIR;
  if (process.platform === 'darwin') {
    return path.join(os.homedir(), 'Library', 'Application Support', 'EdgeFabric', 'runtime');
  }
  if (isWindows) {
    return path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'), 'EdgeFabric', 'runtime');
  }
  return path.join(process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share'), 'EdgeFabric', 'runtime');
}

function launcherCommand() {
  const root = repoRoot();
  // --no-browser: this window shows the workbench, so control.py must not also
  // open the system browser.
  const passthrough = ['--no-browser'];
  if (isWindows) {
    return {
      file: 'powershell.exe',
      args: ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
        path.join(root, 'P4', 'scripts', 'bootstrap.ps1'), ...passthrough],
      options: { windowsHide: true },
    };
  }
  return {
    file: 'bash',
    args: [path.join(root, 'P4', 'scripts', 'bootstrap.sh'), ...passthrough],
    options: {},
  };
}

function send(channel, payload) {
  if (window && !window.isDestroyed()) window.webContents.send(channel, payload);
}

function record(text) {
  for (const line of String(text).split(/\r?\n/)) {
    if (!line.trim()) continue;
    logTail.push(line);
    send('log', line);
  }
  if (logTail.length > 400) logTail = logTail.slice(-400);
}

function startLauncher() {
  const { file, args, options } = launcherCommand();
  child = spawn(file, args, {
    cwd: repoRoot(),
    env: { ...process.env, P4_NO_BROWSER: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
    ...options,
  });
  child.stdout.on('data', (chunk) => record(chunk));
  child.stderr.on('data', (chunk) => record(chunk));
  child.on('error', (error) => send('failed', `无法启动准备脚本：${error.message}`));
  child.on('exit', (code) => {
    if (!quitting && code !== 0) {
      send('failed', `准备脚本退出（代码 ${code}）。日志见下方。`);
    }
  });
}

function probe(url, timeout = 1200) {
  return new Promise((resolve) => {
    const request = http.get(url, { timeout }, (response) => {
      response.resume();
      resolve(response.statusCode === 200);
    });
    request.on('timeout', () => request.destroy());
    request.on('error', () => resolve(false));
  });
}

/** Wait until the workbench answers, then load it. */
async function waitForWorkbench(deadlineMs = 15 * 60 * 1000) {
  const url = `http://127.0.0.1:${WEB_PORT}/`;
  const start = Date.now();
  while (Date.now() - start < deadlineMs) {
    if (await probe(url + 'config')) {
      send('ready', url);
      await window.loadURL(url);
      return;
    }
    if (child && child.exitCode !== null && child.exitCode !== 0) return;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  send('failed', '等待工作台超时。请查看下方日志。');
}

function createWindow() {
  window = new BrowserWindow({
    width: 1180,
    height: 820,
    minWidth: 900,
    minHeight: 640,
    backgroundColor: '#0f1115',
    show: false,
    title: 'Edge Fabric',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  window.once('ready-to-show', () => window.show());
  window.loadFile(path.join(__dirname, 'renderer', 'status.html'));
  // External links open in the system browser, never inside the app.
  window.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
  return window;
}

/** Ask control.py to stop its services, then fall back to a hard kill. */
async function stopServices() {
  await new Promise((resolve) => {
    const request = http.request(
      { host: '127.0.0.1', port: CONTROL_PORT, path: '/api/bridge-session', headers: { 'X-P4-Bridge': '1' }, timeout: 1500 },
      (response) => {
        let body = '';
        response.on('data', (chunk) => (body += chunk));
        response.on('end', () => {
          let session;
          try {
            session = JSON.parse(body).session;
          } catch {
            return resolve();
          }
          if (!session) return resolve();
          const stop = http.request(
            { host: '127.0.0.1', port: CONTROL_PORT, path: '/api/stop', method: 'POST',
              headers: { 'X-P4-Session': session, 'Content-Type': 'application/json', 'Content-Length': 2 }, timeout: 12000 },
            (reply) => {
              reply.resume();
              reply.on('end', () => setTimeout(resolve, 1500));
            },
          );
          stop.on('error', () => resolve());
          stop.on('timeout', () => { stop.destroy(); resolve(); });
          stop.end('{}');
        });
      },
    );
    request.on('error', () => resolve());
    request.on('timeout', () => { request.destroy(); resolve(); });
    request.end();
  });
}

function killChild() {
  if (!child || child.exitCode !== null) return;
  if (isWindows) {
    execFile('taskkill', ['/PID', String(child.pid), '/T', '/F'], () => {});
    return;
  }
  try {
    child.kill('SIGTERM');
  } catch {
    /* already gone */
  }
}

app.whenReady().then(async () => {
  createWindow();
  await new Promise((resolve) => window.webContents.once('did-finish-load', resolve));
  startLauncher();
  await waitForWorkbench();
});

ipcMain.on('retry', () => {
  if (child && child.exitCode === null) return;
  startLauncher();
  void waitForWorkbench();
});

ipcMain.on('open-log', () => {
  shell.showItemInFolder(path.join(runtimeDirectory(), 'setup.log'));
});

ipcMain.handle('log-path', () => path.join(runtimeDirectory(), 'setup.log'));

app.on('window-all-closed', () => app.quit());

app.on('before-quit', async (event) => {
  if (quitting) return;
  event.preventDefault();
  quitting = true;
  send('stopping', '正在停止本机服务…');
  // Ask control.py to stop P1/P2/本机计算 first; the shell child exits with it.
  await stopServices();
  killChild();
  app.quit();
});

module.exports = { runtimeDirectory };
