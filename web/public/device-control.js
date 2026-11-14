import { locale, t } from './i18n.js';
import { createDeviceConstellation } from './workflow.js';

const $ = (id) => document.getElementById(id);
const labels = {
  resource: ['设备服务', 'Resource service'], task: ['任务服务', 'Task service'],
  web: ['工作台', 'Workbench'], node: ['本机计算', 'Local compute'],
  test: ['OCR 测试', 'OCR test'], 'env/setup': ['环境准备', 'Environment setup'],
  操作记录: ['操作记录', 'Activity'],
};

async function request(action, body, signal) {
  const response = await fetch('/api/p4/' + action, {
    cache: 'no-store',
    signal: AbortSignal.any([signal, AbortSignal.timeout(12000)]),
    ...(body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || data.message);
  return data;
}

export function setupDeviceControl({ authenticate, unauthenticate, navigate }) {
  const section = $('device-control');
  const notice = $('p4-notice');
  const inventory = $('device-inventory');
  const listButton = $('show-devices');
  const controlButton = $('show-device-control');
  const constellation = createDeviceConstellation($('device-constellation'));
  const logViews = new Map();
  let controller;
  let timer;
  let availability;
  let snapshot;
  let pending = false;
  let feedback;
  let addressList = '';
  let centers = [];
  let usingLocalCredential = false;
  const validJobId = (id) => /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(id || '');

  function renderLogs() {
    if (!$('p4-logs').open) return;
    for (const [name, value] of Object.entries(snapshot.logs || {})) {
      if (!logViews.has(name)) {
        const heading = document.createElement('h3');
        const output = document.createElement('pre');
        $('p4-log-list').append(heading, output);
        logViews.set(name, { heading, output });
      }
      const { heading, output } = logViews.get(name);
      heading.textContent = labels[name] ? t(...labels[name]) : name;
      if (output.textContent !== value) {
        const top = output.scrollTop;
        const follow = output.scrollHeight - output.clientHeight - top < 16;
        output.textContent = value;
        output.scrollTop = follow ? output.scrollHeight : top;
      }
    }
    for (const [name, { heading, output }] of logViews) {
      if (Object.hasOwn(snapshot.logs || {}, name)) continue;
      heading.remove();
      output.remove();
      logViews.delete(name);
    }
  }

  function renderCenters() {
    const select = $('p4-discovery');
    const selected = select.value;
    select.replaceChildren(new Option(t('请选择中心', 'Select a center'), ''),
      ...centers.map(({ name, address }) => new Option(`${name} · ${address}`, address)));
    if (centers.some(({ address }) => address === selected)) select.value = selected;
  }

  function render() {
    const available = availability?.available;
    const disconnected = snapshot && !snapshot.busy && (snapshot.mode === 'idle' ||
      (snapshot.mode === 'center' && (!snapshot.processes.resource || !snapshot.processes.task)));
    $('p4-content').hidden = !snapshot || !available;
    const launcher = $('p4-launcher');
    launcher.hidden = available || !availability?.localUrl;
    if (!launcher.hidden) launcher.href = availability.localUrl;
    let status = '';
    if (!availability) status = t('正在检查本机控制台…', 'Checking the local controller…');
    else if (!available) {
      status = {
        mock: t('演示模式中可查看设备列表。真实设备控制需要本机启动器。', 'The demo shows the device list. Live controls require the local launcher.'),
        remote: t('本机控制仅在运行 P4 的电脑上可用。', 'Local controls are available on the computer running P4.'),
        offline: t('请先运行 P4/start.cmd 或 P4/start.command。启动后本页会自动接入。', 'Run P4/start.cmd or P4/start.command. This view will connect automatically.'),
      }[availability.reason];
    } else if (snapshot) {
      if (snapshot.busy) status = t(`操作进行中 · ${snapshot.operation_seconds} 秒`, `Working · ${snapshot.operation_seconds}s`);
      const lastMessage = snapshot.message?.split('\n').at(-1);
      const failed = lastMessage?.startsWith('未完成：');
      if (failed || (snapshot.busy && locale !== 'en')) {
        const message = lastMessage;
        status = [status, message].filter(Boolean).join(' · ');
      }
      if (!snapshot.busy && snapshot.mode === 'idle' && !failed) {
        status = t('中心未连接。可在此启动中心。', 'Center disconnected. You can start it here.');
      } else if (disconnected && !failed) {
        status = t('中心服务连接已断开。可查看日志，或停止后重新启动。', 'Center services disconnected. View the logs or stop and restart the center.');
      }
      if (snapshot.discovery_error && snapshot.mode === 'center' && snapshot.address !== '127.0.0.1') {
        status = [status, t('自动发现暂不可用，可使用连接码。', 'Discovery is unavailable. Use the connection code.')].filter(Boolean).join(' · ');
      }
    }
    const text = feedback ? (Array.isArray(feedback.message) ? t(...feedback.message) : feedback.message) : status;
    if (notice.textContent !== (text || '')) notice.textContent = text || '';
    notice.hidden = !text;
    notice.dataset.tone = feedback?.error || availability?.reason === 'offline' || disconnected ? 'error' : '';
    if (!available || !snapshot) {
      constellation.show(false);
      return;
    }

    const { mode, processes, workbench, ocr_test: test } = snapshot;
    const idle = mode === 'idle';
    const center = mode === 'center';
    const busy = pending || snapshot.busy;
    const healthy = Object.values(processes).every(Boolean);
    if (usingLocalCredential && !center && !busy) {
      usingLocalCredential = false;
      unauthenticate();
    }
    $('p4-node').textContent = snapshot.node_name;
    $('p4-role').textContent = idle ? t('本机控制', 'Local controls') : center ? t('中心', 'Center') : t('计算节点', 'Compute node');
    const indicator = $('p4-state');
    indicator.textContent = busy ? t('处理中', 'Working') : !healthy ? t('服务已退出', 'Service stopped') :
      idle ? t('待启动', 'Idle') : t('运行中', 'Running');
    indicator.dataset.tone = !healthy ? 'bad' : busy ? 'warn' : idle ? '' : 'good';
    $('p4-setup').hidden = !idle;
    $('p4-toggle-legacy').hidden = !idle;
    if (!idle) {
      $('p4-legacy').hidden = true;
      $('p4-toggle-legacy').setAttribute('aria-expanded', 'false');
    }
    $('p4-running').hidden = idle;
    $('p4-reconnect').hidden = !snapshot.saved_pair;
    const localOnly = snapshot.address === '127.0.0.1';
    $('p4-pair').hidden = !center;
    $('p4-pair-local').hidden = !localOnly;
    $('p4-pair-fields').hidden = localOnly;
    $('p4-test-panel').hidden = !center;
    $('p4-workbench-panel').hidden = !center || !workbench;
    constellation.show(center && Boolean(workbench) && !section.hidden);
    $('p4-zip').hidden = busy || !snapshot.has_result;
    const testState = $('p4-test-state');
    testState.hidden = !test;
    testState.textContent = {
      running: validJobId(test?.job_id) ? t('已创建真实 OCR 任务。', 'Live OCR job created.') :
        t('正在准备并上传两张测试图。', 'Preparing and uploading two test images.'),
      passed: t('两图 OCR 和 ZIP 校验通过。', 'Two-image OCR and ZIP verification passed.'),
      failed: t('验收未通过，详情见服务日志。', 'Verification failed. See the service logs.'),
    }[test?.state] || '';
    if (snapshot.last_center) $('p4-address').placeholder = snapshot.last_center;
    const localNode = snapshot.local_node;
    $('p4-sharing').textContent = localNode?.connection === 'ONLINE'
      ? localNode.mode === 'ACTIVE' ? t('正在共享算力', 'Sharing compute')
        : localNode.mode === 'PAUSED' ? t('计算已暂停', 'Compute paused')
          : t('正在切换共享状态', 'Updating sharing status')
      : localNode?.connection === 'STALE' ? t('暂未收到中心心跳确认', 'Center heartbeat confirmation is stale')
        : localNode ? t('正在等待中心确认注册', 'Waiting for the center to confirm registration')
          : t('本机计算节点未启动', 'Local compute node is stopped');
    $('p4-worker-id').hidden = !localNode?.worker_id;
    $('p4-worker-id').textContent = localNode?.worker_id || '';
    $('p4-short-code').textContent = `${snapshot.address}:${snapshot.control_port} · ${snapshot.code || t('已过期', 'Expired')}`;
    const input = $('p4-code');
    if (input.value !== (snapshot.connection_code || '')) input.value = snapshot.connection_code || '';
    input.placeholder = t('连接码已过期', 'Connection code expired');
    const network = $('p4-network');
    const addresses = JSON.stringify([locale, ...snapshot.addresses]);
    if (addresses !== addressList) {
      const previous = network.value;
      network.replaceChildren(...snapshot.addresses.map((address) => new Option(address === '127.0.0.1'
        ? t('仅本机 · 127.0.0.1', 'This computer only · 127.0.0.1') : address, address)));
      network.value = snapshot.addresses.includes(previous) ? previous : snapshot.address;
      addressList = addresses;
    }
    const thisWorkbench = workbench && new URL(workbench.local_url).port === location.port;
    $('p4-use-token').hidden = !thisWorkbench;
    $('p4-upload').hidden = !thisWorkbench;
    $('p4-test-job').hidden = !thisWorkbench || !validJobId(test?.job_id);
    $('p4-test-outputs').hidden = $('p4-test-job').hidden || test.state === 'running';
    const workbenchLink = $('p4-workbench-link');
    workbenchLink.hidden = !workbench || thisWorkbench;
    if (workbench) workbenchLink.href = workbench.local_url + '#resources';
    $('p4-lan').hidden = !workbench?.lan_url;
    $('p4-lan').textContent = workbench?.lan_url || '';
    for (const button of section.querySelectorAll('#p4-content button')) button.disabled = busy;
    $('p4-on').disabled ||= Boolean(processes.node && snapshot.sharing_requested);
    $('p4-off').disabled ||= !processes.node || !snapshot.sharing_requested;
    $('p4-copy-code').disabled ||= !snapshot.connection_code;
    for (const id of ['p4-upload', 'p4-test-job', 'p4-test-outputs']) $(id).disabled = pending;
    renderLogs();
  }

  async function poll(run) {
    if (run.signal.aborted) return;
    try {
      const current = availability?.available ? availability : await request('availability', undefined, run.signal);
      const status = current.available ? await request('status', undefined, run.signal) : undefined;
      if (run.signal.aborted) return;
      availability = current;
      snapshot = status;
    } catch {
      if (run.signal.aborted) return;
      availability = { available: false, reason: 'offline', localUrl: availability?.localUrl };
      snapshot = undefined;
    }
    if (run.signal.aborted) return;
    render();
    if (availability.reason !== 'mock' && availability.reason !== 'remote') {
      timer = setTimeout(() => void poll(run), availability.available ? 2000 : 4000);
    }
  }

  function start() {
    if (controller) return;
    controller = new AbortController();
    void poll(controller);
  }

  function stop() {
    constellation.show(false);
    controller?.abort();
    clearTimeout(timer);
    controller = undefined;
    pending = false;
  }

  function select(active) {
    listButton.setAttribute('aria-pressed', String(!active));
    controlButton.setAttribute('aria-pressed', String(active));
    inventory.hidden = active;
    section.hidden = !active;
    if (active) {
      render();
      start();
    } else {
      stop();
      snapshot = undefined;
      feedback = undefined;
      $('p4-content').hidden = true;
    }
  }

  async function action(endpoint, data = {}, onDone) {
    if (!controller || pending) return;
    const run = controller;
    pending = true;
    feedback = undefined;
    render();
    try {
      const result = await request(endpoint, data, run.signal);
      if (run.signal.aborted) return;
      onDone?.(result);
      const status = await request('status', undefined, run.signal);
      if (!run.signal.aborted) snapshot = status;
    } catch (error) {
      if (!run.signal.aborted) feedback = { message: error.message, error: true };
    } finally {
      if (!run.signal.aborted) {
        pending = false;
        render();
      }
    }
  }

  listButton.addEventListener('click', () => select(false));
  controlButton.addEventListener('click', () => select(true));
  for (const [id, endpoint, payload] of [
    ['p4-center', 'center', () => ({ address: $('p4-network').value, local_node: $('p4-local-node').checked })],
    ['p4-join', 'connect', () => ({ connection_code: $('p4-join-code').value.trim() })],
    ['p4-legacy', 'join', () => ({ address: $('p4-address').value.trim() || $('p4-discovery').value, code: $('p4-pin').value })],
  ]) {
    $(id).addEventListener('submit', (event) => {
      event.preventDefault();
      const body = payload();
      if (endpoint === 'join' && !body.address) {
        feedback = { message: ['请选择或填写中心地址。', 'Select or enter a center address.'], error: true };
        render();
        return;
      }
      void action(endpoint, body);
    });
  }
  section.addEventListener('click', (event) => {
    const button = event.target.closest('[data-p4-action]');
    if (!button) return;
    void action(button.dataset.p4Action);
  });
  $('p4-toggle-legacy').addEventListener('click', () => {
    const legacy = $('p4-legacy');
    legacy.hidden = !legacy.hidden;
    $('p4-toggle-legacy').setAttribute('aria-expanded', String(!legacy.hidden));
    if (!legacy.hidden) $('p4-address').focus();
  });
  $('p4-find').addEventListener('click', () => void action('discover', {}, (result) => {
    centers = result.centers;
    renderCenters();
    if (!centers.length) feedback = { message: ['未发现中心，可输入中心地址。', 'No centers found. Enter the center address.'] };
  }));
  $('p4-use-token').addEventListener('click', () => void action('workbench-token', {}, ({ token }) => {
    authenticate(token);
    usingLocalCredential = true;
    feedback = { message: ['令牌已应用到当前工作台会话。', 'The credential was applied to this workbench session.'] };
  }));
  for (const [id, target] of [
    ['p4-upload', () => '#submit/upload'],
    ['p4-test-job', () => `#jobs/${snapshot.ocr_test.job_id}`],
    ['p4-test-outputs', () => `#jobs/${snapshot.ocr_test.job_id}/results`],
  ]) {
    $(id).addEventListener('click', () => {
      const destination = target();
      void action('workbench-token', {}, ({ token }) => {
        authenticate(token);
        usingLocalCredential = true;
        navigate(destination);
      });
    });
  }
  $('p4-copy-code').addEventListener('click', async () => {
    const input = $('p4-code');
    try {
      await navigator.clipboard.writeText(input.value);
      feedback = { message: ['连接码已复制。', 'Connection code copied.'] };
    } catch {
      input.select();
      feedback = { message: ['已选中连接码，可手动复制。', 'The connection code is selected for copying.'] };
    }
    render();
  });
  $('p4-logs').addEventListener('toggle', () => { if (snapshot) renderLogs(); });
  renderCenters();
  return {
    reset() {
      select(false);
      feedback = availability = snapshot = undefined;
      for (const id of ['p4-code', 'p4-short-code', 'p4-join-code', 'p4-pin']) {
        const element = $(id);
        if (element instanceof HTMLInputElement) element.value = '';
        else element.textContent = '';
      }
    },
    manualCredential() { usingLocalCredential = false; },
    render() { renderCenters(); render(); },
  };
}
