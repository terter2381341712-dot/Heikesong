const graph = document.getElementById('workflow-graph');
const video = document.getElementById('workflow-video');
const playback = document.getElementById('workflow-playback');
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
const playbackKey = 'edge-fabric:home-motion';
const dots = [];
const corners = [[1, 1, 1], [-1, -1, 1], [-1, 1, -1], [1, -1, -1]];
for (let i = 0; i < corners.length; i++) {
  for (let j = i + 1; j < corners.length; j++) {
    for (let step = 0; step < 15; step++) {
      const t = step / 15;
      dots.push({
        point: corners[i].map((value, axis) => value * (1 - t) + corners[j][axis] * t),
        phase: t + i * .13 + j * .23,
        size: step === 0 ? 1.9 : .86,
        edge: true,
      });
    }
  }
}
// Keep particle positions stable across frames so the glow travels along a coherent surface.
for (let i = 1; i <= 108; i++) {
  const z = 1 - 2 * i / 109;
  const angle = i * 2.39996;
  const radius = Math.sqrt(1 - z * z) * (.76 + .52 * ((i * .61803) % 1));
  dots.push({
    point: [Math.cos(angle) * radius, z * radius, Math.sin(angle) * radius],
    phase: i * .071,
    size: i % 6 === 0 ? .91 : .51,
    edge: false,
  });
}

const canvases = [...graph.querySelectorAll('.workflow-node-orb')];
let orbs = [];
let desktop = false;
let frame = 0;
let lastFrame = 0;
let playhead = 0;
let hoveredNode = '';
let focusedNode = '';
let inView = false;
let pausedByUser = false;
try { pausedByUser = localStorage.getItem(playbackKey) === 'off'; }
catch { /* Keep playback usable when browser storage is unavailable. */ }

function paintOrbs(time) {
  for (const { ctx, color, index } of orbs) {
    const angle = time * (.68 + index * .035) + index * 1.14;
    const tilt = .52 + Math.sin(time * .84 + index) * .38;
    const cos = Math.cos(angle);
    const sin = Math.sin(angle);
    const cosTilt = Math.cos(tilt);
    const sinTilt = Math.sin(tilt);
    ctx.clearRect(0, 0, 80, 80);
    ctx.fillStyle = color;
    for (const { point: [x, y, z], size, phase, edge } of dots) {
      const depth = (z * cos - x * sin) * cosTilt + y * sinTilt;
      const scale = 15.2 / (1 - depth * .12);
      const px = 40 + (x * cos + z * sin) * scale;
      const py = 40 + (y * cosTilt - (z * cos - x * sin) * sinTilt) * scale;
      const wave = .5 + .5 * Math.cos(phase * Math.PI * 2 - time * 3.5);
      const depthAlpha = .72 + depth * .12;
      ctx.globalAlpha = depthAlpha * (edge ? .20 + .76 * wave ** 3 : .15 + .35 * wave);
      ctx.beginPath();
      ctx.arc(px, py, size * (1 + .18 * wave), 0, Math.PI * 2);
      ctx.fill();
    }
  }
}

function animateOrbs(now) {
  if (!lastFrame || now - lastFrame >= 30) {
    if (lastFrame) playhead += Math.min(now - lastFrame, 60) / 1000;
    lastFrame = now;
    paintOrbs(playhead);
  }
  frame = requestAnimationFrame(animateOrbs);
}

function syncOrbs(playing) {
  if (desktop && !orbs.length) {
    orbs = canvases.flatMap((canvas, index) => {
      const ctx = canvas.getContext('2d');
      if (!ctx) return [];
      ctx.scale(2, 2);
      return [{ ctx, index, color: getComputedStyle(canvas).color }];
    });
    paintOrbs(playhead);
  }
  if (desktop && playing && orbs.length) {
    if (!frame) frame = requestAnimationFrame(animateOrbs);
  } else if (frame) {
    cancelAnimationFrame(frame);
    frame = lastFrame = 0;
  }
}

function highlightNode() {
  graph.dataset.highlight = hoveredNode || focusedNode;
}

for (const link of graph.querySelectorAll('[data-workflow-node]')) {
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

function canPlay() {
  return inView && !document.hidden && !document.querySelector('dialog[open]');
}

export function syncPlayback() {
  const playing = !reducedMotion.matches && !pausedByUser && canPlay();
  const usePoster = reducedMotion.matches || Boolean(video.error);
  graph.toggleAttribute('data-flowing', playing);
  syncOrbs(playing);
  playback.disabled = reducedMotion.matches;
  playback.checked = !pausedByUser && !reducedMotion.matches;
  document.getElementById('motion-preference').hidden = !reducedMotion.matches;
  if (usePoster || !playing) {
    video.pause();
    if (usePoster) delete graph.dataset.videoReady;
    return;
  }
  if (!video.hasAttribute('src')) video.src = video.dataset.src;
  video.play().catch(() => {
    if (pausedByUser || !canPlay() || reducedMotion.matches || video.error) return;
    pausedByUser = true;
    syncPlayback();
  });
}

video.addEventListener('playing', () => { graph.dataset.videoReady = 'true'; });
video.addEventListener('error', syncPlayback);
playback.addEventListener('change', () => {
  pausedByUser = !playback.checked;
  try { localStorage.setItem(playbackKey, pausedByUser ? 'off' : 'on'); }
  catch { /* The preference still applies during the current visit. */ }
  syncPlayback();
});
reducedMotion.addEventListener('change', syncPlayback);
document.addEventListener('visibilitychange', syncPlayback);
new ResizeObserver(([entry]) => {
  desktop = entry.contentRect.width > 740;
  syncOrbs(graph.hasAttribute('data-flowing'));
}).observe(graph);

const observer = new IntersectionObserver(([entry]) => {
  inView = entry.isIntersecting;
  if (!inView) {
    hoveredNode = '';
    focusedNode = '';
    highlightNode();
  }
  syncPlayback();
}, { threshold: 0.12 });

const poster = graph.querySelector('.workflow-poster img');
// Give the static frame the first paint before requesting the video.
const observeAfterPoster = () => requestAnimationFrame(() => requestAnimationFrame(() => observer.observe(graph)));
if (poster.complete) observeAfterPoster();
else {
  poster.addEventListener('load', observeAfterPoster, { once: true });
  poster.addEventListener('error', observeAfterPoster, { once: true });
}
