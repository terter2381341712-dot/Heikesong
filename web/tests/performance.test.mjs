import { test } from 'node:test';
import assert from 'node:assert/strict';
import { performancePanel, jobPerformance } from '../public/performance.js';

test('CPU and GPU stay visible and missing measurements are not invented', () => {
  const html = performancePanel({worker_id: 'device'}, null, '');
  assert.match(html, /CPU/);
  assert.match(html, /<h5>GPU<\/h5>/);
  assert.match(html, /— 张\/秒/);
  assert.doesNotMatch(html, /0\.00%/);
  assert.match(html, /ΣPᵢ/);
});

test('observed success, standalone useful rate and pool efficiency remain separate', () => {
  const html = performancePanel({worker_id: 'device'}, {
    devices: {device: {cpu: {successful_images_per_second: 0.1}}},
    realtime: {devices: {device: {cpu: {processing_images_per_second: 0.5, completed: 2, measurement_seconds: 4, active_executions: 0}}}},
    formal: {devices: {device: {cpu: {standalone: {
      images_per_second: 1, rounds: 3, configuration: {max_slots: 1}, windows: [],
    }, pools: [{images_per_second: 2.5, aggregation_efficiency_percent: 83.3333, speedup: 1.25}]}}}},
  }, '');
  for (const value of ['0.500 张/秒', '0.100 张/秒', '1.000 张/秒', '2.500 张/秒', '83.33%', '1.25×']) {
    assert.ok(html.includes(value), value);
  }
});

test('job progress shows per-device CPU/GPU activity for the selected job only', () => {
  const job = {job_id: 'job', tasks: [{dispatches: [{worker_id: 'device'}]}]};
  const nodes = [{worker_id: 'device', name: '<device>', connection: 'ONLINE', mode: 'ACTIVE', telemetry: {cpu_percent: 30}}];
  const snapshot = {realtime: {job_id: 'job', devices: {device: {cpu: {processing_images_per_second: 2, completed: 4, active_executions: 1}}}}};
  snapshot.window_start = '2026-11-15T04:00:00Z';
  snapshot.window_end = '2026-11-15T04:05:00Z';
  snapshot.utilization = {device: {cpu_percent: {mean: 20, samples: 2}, gpu_percent: {mean: null, samples: 0}}};
  const html = jobPerformance(job, nodes, snapshot);
  assert.ok(html.includes('2.000 张/秒') && html.includes('20.0% · 2 次采样'));
  assert.ok(!html.includes('30.0%'));
  assert.ok(html.includes('未采样') && html.includes('近5分钟'));
  assert.ok(html.includes('12:00:00') && html.includes('12:05:00'));
  assert.ok(html.includes('<th>GPU</th>') && html.includes('&lt;device&gt;'));
  snapshot.realtime.job_id = 'another-job';
  assert.ok(!jobPerformance(job, nodes, snapshot).includes('2.000 张/秒'));
});

test('runtime errors are escaped and mock data cannot be imported', () => {
  const html = performancePanel({worker_id: 'device'}, null, '<script>unsafe</script>', false, true);
  assert.ok(html.includes('&lt;script&gt;unsafe&lt;/script&gt;'));
  assert.ok(!html.includes('data-performance-import'));
});

test('integrated graphics throughput appears under the unified GPU column', () => {
  const node = {worker_id: 'intel', name: 'Intel GPU', connection: 'ONLINE', mode: 'ACTIVE', capabilities: [{backend: 'directml'}]};
  const snapshot = {realtime: {job_id: 'job', devices: {intel: {directml: {processing_images_per_second: 3.5}}}}};
  const html = jobPerformance({job_id: 'job', tasks: []}, [node], snapshot);
  assert.ok(html.includes('<th>GPU</th>') && html.includes('3.500 张/秒') && html.includes('DirectML'));
  assert.ok(!html.includes('GPU · CUDA'));
});

test('one physical computer has one row with CPU and all GPU workers in their columns', () => {
  const nodes = ['cpu', 'gpu-cuda-0', 'gpu-directml-1'].map((key, index) => ({
    worker_id: key, name: `my-computer-${key}`, connection: 'ONLINE', mode: 'ACTIVE',
    hardware: {manual_labels: {physical_device_id: 'one-pc', execution_backend: ['cpu', 'cuda', 'directml'][index]}},
  }));
  const snapshot = {realtime: {job_id: 'job', devices: {
    cpu: {cpu: {processing_images_per_second: 1}},
    'gpu-cuda-0': {cuda: {processing_images_per_second: 2}},
    'gpu-directml-1': {directml: {processing_images_per_second: 3}},
  }}, utilization: {cpu: {cpu_percent: {mean: 20, samples: 10}}, 'gpu-cuda-0': {cpu_percent: {mean: 90, samples: 10}}}};
  const html = jobPerformance({job_id: 'job', tasks: []}, nodes, snapshot);
  assert.equal((html.match(/<th scope="row">/g) || []).length, 1);
  assert.ok(html.includes('my-computer<br>') && !html.includes('my-computer-gpu'));
  for (const value of ['1.000 张/秒', '2.000 张/秒', '3.000 张/秒', '20.0%']) assert.ok(html.includes(value));
  assert.ok(!html.includes('90.0%'));
  nodes[2].hardware.manual_labels.physical_device_id = 'another-pc';
  assert.equal((jobPerformance({job_id: 'job', tasks: []}, nodes, snapshot).match(/<th scope="row">/g) || []).length, 2);
});
