import { t } from './i18n.js';

const safe = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = (value, digits = 3) => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—';
const time = value => value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—';
const gpuBackends = ['cuda', 'directml', 'coreml'];
function gpuBackend(node, snapshot) {
  return [...(node.capabilities || []).map(c => c.backend), node.hardware?.manual_labels?.execution_backend,
    ...Object.keys(snapshot?.realtime?.devices?.[node.worker_id] || {}),
    ...Object.keys(snapshot?.formal?.devices?.[node.worker_id] || {})].find(b => gpuBackends.includes(b)) || 'cuda';
}
const backendName = backend => ({cuda: 'CUDA', directml: 'DirectML', coreml: 'CoreML（系统选择 GPU/ANE/CPU）'}[backend] || backend);

export function performancePanel(node, snapshot, error, importing = false, mock = false) {
  const live = snapshot?.devices?.[node.worker_id] || {};
  const recent = snapshot?.realtime?.devices?.[node.worker_id] || {};
  const formal = snapshot?.formal?.devices?.[node.worker_id] || {};
  const utilization = snapshot?.utilization?.[node.worker_id] || {};
  const cards = [['cpu', 'CPU'], [gpuBackend(node, snapshot), 'GPU']].map(([backend, label]) => {
    const baseline = formal[backend]?.standalone;
    const pools = formal[backend]?.pools || [];
    const pool = pools.at(-1);
    const averages = utilization[backend === 'cpu' ? 'cpu_percent' : 'gpu_percent'];
    const fact = (name, value) => `<div><dt>${name}</dt><dd>${value}</dd></div>`;
    const windows = baseline?.windows || [];
    return `<section class="performance-backend"><h5>${label}</h5>${label === 'GPU' && recent[backend] ? `<p class="field-hint">${safe(backendName(backend))}</p>` : ''}<dl class="performance-facts">
      ${fact(t('近期处理速度', 'Recent processing throughput'), `${number(recent[backend]?.processing_images_per_second)} ${t('张/秒', 'images/s')}`)}
      ${fact(t('单张平均执行耗时', 'Mean worker time per image'), `${number(recent[backend]?.average_worker_seconds)} s`)}
      ${fact(t('正在执行', 'Running now'), recent[backend] ? `${recent[backend].active_executions} ${t('项', 'tasks')}` : '—')}
      ${fact(t('独立有效吞吐量 Pᵢ', 'Standalone useful throughput Pᵢ'), `${number(baseline?.images_per_second)} ${t('张/秒', 'images/s')}`)}
      ${fact(t('所属池有效吞吐量 Ppool', 'Pool useful throughput Ppool'), `${number(pool?.images_per_second)} ${t('张/秒', 'images/s')}`)}
      ${fact(t('所属池聚合效率 η', 'Pool aggregation efficiency η'), pool ? `${number(pool.aggregation_efficiency_percent, 2)}%` : '—')}
      ${fact(t('相对最快单机加速比 S', 'Speedup over best standalone S'), pool ? `${number(pool.speedup, 2)}×` : '—')}
      ${fact(t('整机平均利用率', 'Mean device utilization'), averages?.mean != null ? `${number(averages.mean, 1)}% · ${averages.samples} ${t('次采样', 'samples')}` : t('未采样', 'Not sampled'))}
    </dl>${recent[backend] ? `<p class="field-hint">${t('近期实际执行窗口', 'Recent execution window')}：${recent[backend].completed} ${t('张成功', 'successes')} / ${number(recent[backend].measurement_seconds, 2)} s；${safe(time(recent[backend].started_at))} → ${safe(time(recent[backend].finished_at))}</p>` : ''}<p class="field-hint">${baseline ? t(`${baseline.rounds}轮单机服务测量的中位数；配置：${baseline.configuration.max_slots}槽。`, `Median of ${baseline.rounds} standalone service rounds; ${baseline.configuration.max_slots} slots.`) : t('尚无本设备此后端的正确性核验基线。', 'No quality-checked standalone baseline for this backend.')}</p>
    <details><summary>${t('5分钟长期平均', '5-minute long-term average')}</summary><p>${number(live[backend]?.successful_images_per_second)} ${t('张/秒', 'images/s')}（${t('成功数÷300秒，含空闲', 'successes / 300s including idle time')}）</p></details>
    ${pool?.review_required ? `<p class="notice error">${t('效率超过100%，请复核基线、批量、任务难度与负载，并交由评委复核。', 'Efficiency exceeds 100%. Review baselines, batching, difficulty and load with judges.')}</p>` : ''}
    ${windows.length ? `<details><summary>${t('测量窗口与样本', 'Measurement windows and samples')}</summary>${windows.map(w => `<p class="field-hint">${safe(time(w.started_at))} → ${safe(time(w.finished_at))}<br>${w.correct_images}/${w.inputs} ${t('张正确', 'correct images')} · ${number(w.seconds, 2)}s · ${w.concurrency} ${t('并发槽位', 'concurrent slots')}</p>`).join('')}${pool ? `<p class="field-hint">Ppool=${number(pool.images_per_second)}；ΣPᵢ=${number(pool.baseline_sum)} ${t('张/秒', 'images/s')}；${pool.participants.length} ${t('台独立设备', 'devices')}</p>` : ''}</details>` : ''}</section>`;
  }).join('');
  return `<section class="device-performance"><h4>${t('吞吐量与聚合效率', 'Throughput and aggregation efficiency')}</h4>
    <p class="field-hint">${t('η = Ppool ÷ ΣPᵢ × 100%；S = Ppool ÷ max(Pᵢ)。η 是所属算力池的效率，不是单设备利用率。', 'η = Ppool / ΣPᵢ × 100%; S = Ppool / max(Pᵢ). η describes the pool, not device utilization.')}</p>
    <div class="performance-grid">${cards}</div>
    <p class="field-hint">${t('近期速度按最近30秒有结束记录或仍在执行的任务，使用成功数÷实际执行窗口（含窗口内等待及失败耗时），不再固定除以300。并发执行按同一墙钟窗口计时。没有完成结果时不估算速度；空闲30秒后清空。单张耗时来自节点计时。上述成功速度未核验识别正确性；PDF有效吞吐量和η仅来自同输入、同质量标准的测量报告。', 'Recent speed uses successes / actual elapsed execution window for tasks finishing in the last 30s or still running, including waits and failed work within that window. Concurrent slots share wall time. No predicted speed before completion; idle samples expire after 30s. Worker times come from node timings. Observed success rates are not quality-checked useful throughput; PDF efficiency requires matched benchmark reports.')}</p>
    <p class="field-hint">${t('实时观测窗口（北京时间）', 'Live window (Beijing time)')}：${safe(time(snapshot?.window_start))} → ${safe(time(snapshot?.window_end))}；${t('心跳采样目标间隔3秒，平均值仅包含实际收到的样本。', 'Heartbeat sampling targets 3 seconds; means use received samples only.')}</p>
    ${error ? `<p class="notice error">${safe(error)}</p>` : ''}
    ${mock ? `<p class="field-hint">${t('演示模式不提供真实性能结论。', 'Demo mode has no real performance measurements.')}</p>` : `<label class="button secondary performance-import">${importing ? t('正在导入…', 'Importing…') : t('导入单机及池测量报告', 'Import standalone and pool reports')}<input type="file" accept=".json,application/json" multiple data-performance-import ${importing ? 'disabled' : ''}></label><p class="field-hint">${t('选择各设备CPU/GPU独立测试和池测试的 throughput.json；每组至少3轮、20张不同图片并通过正确性核验。导入会替换当前展示的测量组，不改变设备或任务。', 'Select standalone CPU/GPU and pool throughput.json files: at least 3 rounds and 20 distinct quality-checked images per group. Import replaces displayed benchmark groups only.')}</p>`}
  </section>`;
}

export function jobPerformance(job, nodes, snapshot, error = '') {
  const realtime = snapshot?.realtime;
  const measurements = realtime?.job_id === job.job_id ? realtime.devices : {};
  const workerIds = new Set(job.tasks.flatMap(task => task.dispatches.map(dispatch => dispatch.worker_id)));
  const visible = nodes.filter(node => node.connection === 'ONLINE' || workerIds.has(node.worker_id));
  const rows = visible.map(node => {
    const measured = measurements?.[node.worker_id] || {};
    const formal = snapshot?.formal?.devices?.[node.worker_id] || {};
    const cells = [['cpu', 'cpu_percent'], [gpuBackend(node, snapshot), 'gpu_percent']].map(([backend, percentField]) => {
      const record = measured[backend];
      const capability = (node.capabilities || []).find(c => c.task_type === 'ocr.image.v1');
      const knownGpu = backend !== 'cpu' && (record || capability?.backend === backend || node.hardware?.manual_labels?.execution_backend === backend);
      const readiness = {NOT_PREPARED: t('等待模型准备', 'Waiting for model preparation'), PREPARING: t('模型准备中', 'Preparing model'), READY: t('可计算', 'Ready'), ERROR: t('准备失败', 'Preparation failed'), UNAVAILABLE: t('不兼容', 'Incompatible')};
      const route = knownGpu ? `<br>${safe(backendName(backend))}${capability ? `<br>${safe(readiness[capability.state] || capability.state || '')}${capability.reason ? `<br>${safe(capability.reason)}` : ''}` : ''}` : '';
      const utilization = snapshot?.utilization?.[node.worker_id]?.[percentField];
      const average = utilization?.mean == null ? t('未采样', 'Not sampled') : `${number(utilization.mean, 1)}% · ${utilization.samples} ${t('次采样', 'samples')}`;
      const pool = formal[backend]?.pools?.at(-1);
      const efficiency = pool ? `${number(pool.aggregation_efficiency_percent, 2)}%${pool.review_required ? ' ⚠' : ''}` : '—';
      return `<td><strong>${number(record?.processing_images_per_second)} ${t('张/秒', 'images/s')}</strong>${route}<br>${t('整机平均利用率', 'Mean device utilization')} ${average}<br>${t('执行中', 'Running')} ${record?.active_executions ?? 0} · ${t('成功', 'Succeeded')} ${record?.completed ?? 0}<br>${t('单张耗时', 'Worker time')} ${number(record?.average_worker_seconds)} s<br>${t('基准池η', 'Benchmark pool η')} ${efficiency}${pool ? `<br>Ppool ${number(pool.images_per_second)} ${t('张/秒', 'images/s')}` : ''}</td>`;
    }).join('');
    return `<tr><th scope="row">${safe(node.name)}<br><small>${safe(node.connection)} · ${safe(node.mode)}</small></th>${cells}</tr>`;
  }).join('');
  return `<section class="panel compact job-performance"><h3>${t('各设备 CPU / GPU 运行情况', 'CPU / GPU activity by device')}</h3>
    <p class="field-hint">${t('约每2秒刷新。速度 = 本批次在该设备/后端去重成功数 ÷ 首次执行至最后结束（仍执行则至现在）的墙钟秒数，包含中途等待及失败耗时，不包含首次执行前的排队。单张耗时来自节点计时。关闭窗口后，已受理任务仍由中心后台处理。', 'Refreshes about every 2s. Job speed = deduplicated successes on this device/backend / wall time from first execution to last finish (or now while running), including gaps and failures but excluding the initial queue. Per-image time comes from worker timing. Accepted jobs continue at the center after this window closes.')}</p>
    ${error ? `<p class="notice error">${safe(error)}</p>` : ''}
    <p class="field-hint">${t('利用率观察窗口：近5分钟（北京时间）', 'Utilization window: last 5 minutes (Beijing time)')} ${safe(time(snapshot?.window_start))} → ${safe(time(snapshot?.window_end))}。${t('目标每3秒采样，显示窗口内实际有效样本的算术平均及采样次数；包含其他程序，不是OCR进程专属占用。此滚动窗口与本批次执行窗口分开统计，历史任务仍显示当前近5分钟平均，不代表该任务当时的利用率。', 'Samples target 3-second intervals; values are arithmetic means of valid received samples, with sample counts. Whole-device usage includes other programs. This rolling window is separate from job execution timing; historical jobs still show the current 5-minute mean, not historical job utilization.')}</p>
    <div class="performance-table-scroll"><table class="performance-table"><thead><tr><th>${t('设备', 'Device')}</th><th>CPU</th><th>GPU</th></tr></thead><tbody>${rows || `<tr><td colspan="3">${t('等待设备及执行记录…', 'Waiting for devices and execution records…')}</td></tr>`}</tbody></table></div>
    <p class="field-hint">${t('实时成功速度尚未核验OCR正确性。η = Ppool / ΣPᵢ × 100%，来自已导入的同输入、同配置质量核验报告，属于基准池而非本次任务的实时效率；缺少报告或GPU数据时显示“—”。', 'Live successes are not OCR quality checks. η = Ppool / ΣPᵢ × 100%, from imported matched quality-checked reports; it describes that benchmark pool, not live job efficiency. Missing baselines or GPU data display —.')}</p>
  </section>`;
}
