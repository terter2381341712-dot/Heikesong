import { test } from 'node:test';
import assert from 'node:assert/strict';
import { performancePanel, jobPerformance } from '../public/performance.js';

test('CPU and GPU stay visible and missing measurements are not invented', () => {
  const html = performancePanel({worker_id: 'device'}, null, '');
  assert.match(html, /CPU/);
  assert.match(html, /GPU · CUDA/);
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
  const html = jobPerformance(job, nodes, snapshot);
  assert.ok(html.includes('2.000 张/秒') && html.includes('30.0%'));
  assert.ok(html.includes('GPU · CUDA') && html.includes('&lt;device&gt;'));
  snapshot.realtime.job_id = 'another-job';
  assert.ok(!jobPerformance(job, nodes, snapshot).includes('2.000 张/秒'));
});

test('runtime errors are escaped and mock data cannot be imported', () => {
  const html = performancePanel({worker_id: 'device'}, null, '<script>unsafe</script>', false, true);
  assert.ok(html.includes('&lt;script&gt;unsafe&lt;/script&gt;'));
  assert.ok(!html.includes('data-performance-import'));
});
