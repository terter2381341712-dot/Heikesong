'use strict';

// Status page: mirrors launcher output until the workbench takes over.
const bar = document.getElementById('bar');
const state = document.getElementById('state');
const tagline = document.getElementById('tagline');
const log = document.getElementById('log');
const actions = document.getElementById('actions');
const details = document.getElementById('details');

function append(line) {
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
  log.textContent += line + '\n';
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function fail(message) {
  bar.classList.add('error');
  state.classList.add('error');
  state.textContent = message;
  tagline.textContent = '启动失败';
  actions.hidden = false;
  details.open = true;
}

window.edgeFabric.onLog(append);

window.edgeFabric.onReady(() => {
  bar.classList.add('done');
  state.classList.remove('error');
  state.textContent = '环境已就绪，正在打开工作台…';
  tagline.textContent = '正在打开工作台';
});

window.edgeFabric.onFailed(fail);

window.edgeFabric.onStopping((message) => {
  tagline.textContent = '正在退出';
  state.textContent = message;
});

document.getElementById('copylog').addEventListener('click', async () => {
  await navigator.clipboard.writeText(log.textContent);
  const button = document.getElementById('copylog');
  const original = button.textContent;
  button.textContent = '已复制';
  setTimeout(() => (button.textContent = original), 1400);
});

document.getElementById('retry').addEventListener('click', () => {
  actions.hidden = true;
  details.open = false;
  log.textContent = '';
  bar.classList.remove('error', 'done');
  state.classList.remove('error');
  state.textContent = '正在重新准备本机运行环境…';
  tagline.textContent = '正在准备本机运行环境';
  window.edgeFabric.retry();
});

document.getElementById('openlog').addEventListener('click', () => {
  window.edgeFabric.openLog();
});
