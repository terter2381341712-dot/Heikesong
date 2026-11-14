const graph = document.getElementById('workflow-graph');
const navigation = graph.querySelector('.workflow-nodes');
const video = document.getElementById('workflow-video');
const playback = document.getElementById('workflow-playback');
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
let hoveredNode = '';
let focusedNode = '';
let inView = false;
let pausedByUser = false;

function highlightNode() {
  graph.dataset.highlight = hoveredNode || focusedNode;
}

for (const link of navigation.querySelectorAll('[data-workflow-node]')) {
  const node = link.dataset.workflowNode;
  link.addEventListener('pointerenter', (event) => {
    if (event.pointerType === 'touch') return;
    hoveredNode = node;
    highlightNode();
  });
  link.addEventListener('pointerleave', () => {
    hoveredNode = '';
    highlightNode();
  });
  link.addEventListener('focus', () => {
    focusedNode = link.matches(':focus-visible') ? node : '';
    highlightNode();
  });
  link.addEventListener('blur', () => {
    focusedNode = '';
    highlightNode();
  });
}

function syncPlayback() {
  const usePoster = reducedMotion.matches || Boolean(video.error);
  playback.hidden = usePoster;
  playback.textContent = pausedByUser ? '播放动效' : '暂停动效';
  if (usePoster || pausedByUser || !inView || document.hidden) {
    video.pause();
    if (usePoster) delete graph.dataset.videoReady;
    return;
  }
  if (!video.hasAttribute('src')) video.src = video.dataset.src;
  video.play().catch(() => {
    if (pausedByUser || !inView || document.hidden || reducedMotion.matches || video.error) return;
    pausedByUser = true;
    playback.textContent = '播放动效';
  });
}

video.addEventListener('playing', () => { graph.dataset.videoReady = 'true'; });
video.addEventListener('error', syncPlayback);
playback.addEventListener('click', () => {
  pausedByUser = !pausedByUser;
  syncPlayback();
});
reducedMotion.addEventListener('change', syncPlayback);
document.addEventListener('visibilitychange', syncPlayback);

new IntersectionObserver(([entry]) => {
  inView = entry.isIntersecting;
  if (!inView) {
    hoveredNode = '';
    focusedNode = '';
    highlightNode();
  }
  syncPlayback();
}, { threshold: 0.12 }).observe(graph);
