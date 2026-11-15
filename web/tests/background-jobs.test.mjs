import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile(new URL('../public/app.js', import.meta.url), 'utf8');

test('closing upload dialog while upload is pending still creates and tracks the accepted job', async () => {
  let uploaded, startUpload;
  const started = new Promise(resolve => { startUpload = resolve; });
  const upload = new Promise(resolve => { uploaded = resolve; });
  const job = {job_id: '00000000-0000-4000-8000-000000000001', status: 'QUEUED'};
  const state = {view: 'upload', sending: false, checkingFiles: false, files: [{file: {name: 'image.png'}, mime: 'image/png', uploadId: 'upload'}], history: []};
  let created = 0, polling = 0;
  const context = vm.createContext({
    state, returningHome: false, storageAvailable: true, jobIdPattern: /^[0-9a-f-]{36}$/,
    currentType: () => ({task_type: 'ocr.image.v1', profile_id: 'profile', profile_digest: 'digest'}),
    isConfigured: () => true, isJobView: view => ['progress', 'results'].includes(view),
    renderForm() {}, renderJobs() {}, renderBackgroundJobs() {}, restartPoll() { polling++; },
    setPending(value) { state.pendingCreate = value; }, uuid: () => 'request',
    resourceApi: {upload: () => { startUpload(); return upload; }},
    taskApi: {create: async () => { created++; return job; }},
    rememberJob: entry => state.history.push(entry),
    openJob() { assert.fail('closed dialog must not reopen itself'); },
    toast() {}, t: chinese => chinese, errorText: error => error.message, connectionError() {}, updateConnections() {},
  });
  vm.runInContext(source.slice(source.indexOf('async function submit(event)'), source.indexOf('\nfunction download(')), context);
  const submission = vm.runInContext('submit({preventDefault(){}})', context);
  await started;
  state.view = 'home'; // The close button changes the route while upload is in flight.
  uploaded({artifact_id: 'image', sha256: 'hash'});
  await submission;
  assert.equal(created, 1);
  assert.equal(state.history[0].job_id, job.job_id);
  assert.equal(state.jobId, job.job_id);
  assert.equal(state.sending, false);
  assert.ok(polling > 0);
});

test('home route continues tracking accepted jobs instead of stopping with the modal', async () => {
  let tracked = 0;
  const context = vm.createContext({
    state: {mode: 'live', view: 'home'}, activePoll: null, document: {hidden: false},
    performance, AbortController, clearTimeout() {}, setTimeout() {},
    isJobView: view => ['progress', 'results'].includes(view),
    checkHealth: async () => {}, refreshBackgroundJobs: async () => { tracked++; },
    updateConnections() {}, toast() {}, errorText: String,
  });
  vm.runInContext(source.slice(source.indexOf('function restartPoll()'), source.indexOf('\nfunction readRoute(')), context);
  vm.runInContext('restartPoll()', context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(tracked, 1);
});
