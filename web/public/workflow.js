const graph = document.getElementById('workflow-graph');
const video = document.getElementById('workflow-video');
const playback = document.getElementById('workflow-playback');
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
const playbackKey = 'edge-fabric:home-motion';
const weatherKey = 'edge-fabric:background-weather';
const stage = graph.querySelector('.workflow-stage');
const atmosphere = graph.querySelector('.workflow-atmosphere');
const weather = document.getElementById('scene-weather');
const finePointer = matchMedia('(hover: hover) and (pointer: fine)');
let pointerBounds;
let mistFrame = 0;
let mistPointer;
const tetrahedron = {
  vertices: [[1, 1, 1], [-1, -1, 1], [-1, 1, -1], [1, -1, -1]],
  faces: [[0, 1, 2], [0, 3, 1], [0, 2, 3], [1, 3, 2]],
};

function prism(sides) {
  const ring = Array.from({ length: sides }, (_, index) => {
    const angle = index * 2 * Math.PI / sides;
    return [1.34 * Math.cos(angle), 1.34 * Math.sin(angle)];
  });
  const top = ring.map((_, index) => index);
  const bottom = top.map((index) => index + sides);
  const faces = [top, bottom];
  for (const index of top) {
    const next = (index + 1) % sides;
    faces.push([index, next, bottom[next], bottom[index]]);
  }
  const vertices = [0.96, -0.96].flatMap((height) => ring.map(([x, z]) => [x, height, z]));
  return { vertices, faces };
}

const nodeShapes = {
  upload: tetrahedron,
  progress: prism(3),
  results: prism(4),
  resources: prism(5),
};

function edgeParticles({ vertices, faces }) {
  const edges = new Map();
  for (const face of faces) {
    face.forEach((start, index) => {
      const end = face[(index + 1) % face.length];
      const pair = [Math.min(start, end), Math.max(start, end)];
      edges.set(pair.join(':'), pair);
    });
  }
  return [...edges.values()].flatMap(([i, j]) => {
    const start = vertices[i];
    const end = vertices[j];
    const steps = Math.max(7, Math.round(Math.hypot(...start.map((value, axis) => value - end[axis])) * 5.2));
    return Array.from({ length: steps - 1 }, (_, index) => {
      const position = (index + 1) / steps;
      return {
        point: start.map((value, axis) => value + (end[axis] - value) * position),
        phase: position + i * .13 + j * .23,
        size: .82,
        edge: true,
      };
    });
  }).concat(vertices.map((point, index) => ({ point, phase: index * .31, size: 1.52, edge: true })));
}

const ambientDots = [];
// Keep particle positions stable across frames so the glow travels along a coherent surface.
for (let i = 1; i <= 108; i++) {
  const z = 1 - 2 * i / 109;
  const angle = i * 2.39996;
  const radius = Math.sqrt(1 - z * z) * (.76 + .52 * ((i * .61803) % 1));
  ambientDots.push({
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
const particleFields = new Map();
const rimGlowDuration = 420;
try { pausedByUser = localStorage.getItem(playbackKey) === 'off'; }
catch { /* Keep playback usable when browser storage is unavailable. */ }

function resetMist() {
  if (mistFrame) cancelAnimationFrame(mistFrame);
  mistFrame = 0;
  atmosphere.style.removeProperty('--mist-x');
  atmosphere.style.removeProperty('--mist-y');
}

function setWeather(value) {
  weather.value = ['clear', 'mist', 'overcast'].includes(value) ? value : 'mist';
  graph.dataset.weather = weather.value;
  resetMist();
}

let savedWeather;
try { savedWeather = localStorage.getItem(weatherKey); }
catch { /* Keep the default weather when storage is unavailable. */ }
setWeather(savedWeather);
weather.addEventListener('change', () => {
  setWeather(weather.value);
  try { localStorage.setItem(weatherKey, weather.value); }
  catch { /* The selected weather still applies in this tab. */ }
});

stage.addEventListener('pointerenter', () => { pointerBounds = stage.getBoundingClientRect(); });
stage.addEventListener('pointerleave', resetMist);
stage.addEventListener('pointermove', (event) => {
  if (event.pointerType !== 'mouse' || !finePointer.matches || !graph.hasAttribute('data-flowing') ||
      weather.value === 'clear') return;
  mistPointer = { x: event.clientX, y: event.clientY };
  if (mistFrame) return;
  mistFrame = requestAnimationFrame(() => {
    mistFrame = 0;
    pointerBounds ??= stage.getBoundingClientRect();
    const { left, top, width, height } = pointerBounds;
    if (!width || !height) return;
    const displacement = (offset, size, amplitude) => (Math.max(0, Math.min(1, offset / size)) - .5) * amplitude;
    atmosphere.style.setProperty('--mist-x', `${displacement(mistPointer.x - left, width, 34)}px`);
    atmosphere.style.setProperty('--mist-y', `${displacement(mistPointer.y - top, height, 24)}px`);
  });
}, { passive: true });

function paintOrbs(time) {
  for (const { ctx, color, index, dots } of orbs) {
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
    const delta = lastFrame ? Math.min(now - lastFrame, 60) : 0;
    playhead += delta / 1000;
    lastFrame = now;
    paintOrbs(playhead);
    if (particleFields.size) paintAttraction(delta);
  }
  frame = requestAnimationFrame(animateOrbs);
}

function syncOrbs(playing) {
  if (desktop && !orbs.length) {
    orbs = canvases.flatMap((canvas, index) => {
      const ctx = canvas.getContext('2d');
      if (!ctx) return [];
      ctx.scale(2, 2);
      const shape = nodeShapes[canvas.closest('[data-workflow-node]').dataset.workflowNode];
      return [{ ctx, index, color: getComputedStyle(canvas).color, dots: [...edgeParticles(shape), ...ambientDots] }];
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

// Each entrance has a small canvas behind its label, with no page-wide drawing or hit area.
function attractParticles(link) {
  link.toggleAttribute('data-attracting', true);
  if (!desktop || !finePointer.matches || !graph.hasAttribute('data-flowing')) return;
  if (!particleFields.has(link)) {
    const canvas = document.createElement('canvas');
    canvas.className = 'workflow-node-particles';
    canvas.width = 544;
    canvas.height = 304;
    canvas.setAttribute('aria-hidden', 'true');
    const context = canvas.getContext('2d');
    if (!context) return;
    const label = link.querySelector('.workflow-node-name');
    label.prepend(canvas);
    context.scale(2, 2);
    context.fillStyle = getComputedStyle(link).getPropertyValue('--node-color').trim();
    context.strokeStyle = context.fillStyle;
    context.lineCap = 'round';
    const particles = Array.from({ length: 24 }, (_, index) => ({
      edgePosition: (index + Math.random()) / 24,
      sweep: .18 + Math.random() * .16,
      distance: .64 + Math.random() * .3,
      radius: .55 + Math.random() * .7,
      duration: 750 + Math.random() * 400,
      age: Math.random() * 700,
      impactAge: rimGlowDuration,
    }));
    particleFields.set(link, {
      canvas, context, particles, label, burst: 0,
      width: label.offsetWidth, height: label.offsetHeight,
    });
  }
  const field = particleFields.get(link);
  field.burst = 0;
}

function measureParticleFields() {
  for (const field of particleFields.values()) {
    field.width = field.label.offsetWidth;
    field.height = field.label.offsetHeight;
  }
}

// Sample the straight sections and semicircular ends by distance along the glass rim.
function capsuleEdge(position, width, height) {
  const radius = height / 2;
  const straight = Math.max(0, width - height);
  const arc = Math.PI * radius;
  const distance = ((position % 1 + 1) % 1) * (2 * straight + 2 * arc);
  if (distance < arc || (distance >= arc + straight && distance < 2 * arc + straight)) {
    const right = distance < arc;
    const angle = right ? distance / radius - Math.PI / 2 : (distance - arc - straight) / radius + Math.PI / 2;
    const dx = Math.cos(angle);
    const dy = Math.sin(angle);
    return { x: (right ? straight / 2 : -straight / 2) + dx * radius, y: dy * radius };
  }
  if (distance < arc + straight) return { x: straight / 2 - (distance - arc), y: radius };
  return { x: -straight / 2 + distance - 2 * arc - straight, y: -radius };
}

function paintRimGlow(context, particle, width, height, fade) {
  const progress = particle.impactAge / rimGlowDuration;
  const { edgePosition } = particle;
  const extent = .012 + .043 * (1 - (1 - progress) ** 2);
  const strength = (1 - progress) ** 2 * fade;
  const contact = capsuleEdge(edgePosition, width, height);
  const x = 136 + contact.x;
  const y = 76 + contact.y;

  context.globalAlpha = strength * .16;
  context.lineWidth = 1.15;
  context.beginPath();
  for (let step = -5; step <= 5; step++) {
    const point = capsuleEdge(edgePosition + extent * step / 5, width, height);
    if (step === -5) context.moveTo(136 + point.x, 76 + point.y);
    else context.lineTo(136 + point.x, 76 + point.y);
  }
  context.stroke();

  const halo = context.createRadialGradient(x, y, 0, x, y, 5);
  halo.addColorStop(0, context.strokeStyle);
  halo.addColorStop(1, 'transparent');
  context.globalAlpha = strength * .24;
  context.fillStyle = halo;
  context.fillRect(x - 5, y - 5, 10, 10);
  context.fillStyle = context.strokeStyle;
}

function paintAttraction(delta) {
  for (const [link, field] of particleFields) {
    const { canvas, context, particles } = field;
    const attracting = link.hasAttribute('data-attracting');
    field.burst = attracting ? 0 : field.burst + delta;
    if (field.burst >= 490) {
      canvas.remove();
      particleFields.delete(link);
      continue;
    }
    const { width, height } = field;
    if (!width || !height) continue;
    context.clearRect(0, 0, 272, 152);
    context.save();
    context.beginPath();
    context.rect(0, 0, 272, 152);
    context.roundRect(136 - width / 2, 76 - height / 2, width, height, height / 2);
    context.clip('evenodd');
    for (const particle of particles) {
      particle.impactAge += delta;
      if (attracting) {
        particle.age += delta;
        if (particle.age >= particle.duration) {
          particle.age -= particle.duration;
          particle.impactAge = particle.age;
        }
      }
      const t = particle.age / particle.duration;
      const scatter = field.burst / 490;
      // Sweep along an expanded capsule first, then spiral in to the contact point.
      const position = particle.edgePosition - (1 - t) * particle.sweep + scatter * .09;
      const orbit = 1 - t ** 2.6;
      const offset = orbit * 43 * particle.distance + scatter * 23;
      const edge = capsuleEdge(position, width + 2 * offset, height + 2 * offset);
      const x = 136 + edge.x;
      const y = 76 + edge.y;
      context.globalAlpha = Math.min(t * 5, (1 - t) * 16, 1) * (1 - scatter) ** 2 * .86;
      context.beginPath();
      context.arc(x, y, particle.radius, 0, Math.PI * 2);
      context.fill();
    }
    context.restore();
    context.save();
    context.beginPath();
    context.rect(0, 0, 272, 152);
    context.roundRect(138 - width / 2, 78 - height / 2, width - 4, height - 4, height / 2 - 2);
    context.clip('evenodd');
    for (const particle of particles) {
      if (particle.impactAge < rimGlowDuration) paintRimGlow(context, particle, width, height, 1 - field.burst / 490);
    }
    context.restore();
  }
}

function clearAttraction() {
  for (const { canvas } of particleFields.values()) canvas.remove();
  particleFields.clear();
  for (const link of graph.querySelectorAll('[data-attracting]')) delete link.dataset.attracting;
}

for (const link of graph.querySelectorAll('[data-workflow-node]')) {
  const node = link.dataset.workflowNode;
  link.addEventListener('pointerenter', (event) => {
    if (event.pointerType === 'touch') return;
    hoveredNode = node;
    highlightNode();
    if (event.pointerType === 'mouse') attractParticles(link);
  });
  link.addEventListener('pointerleave', () => {
    hoveredNode = '';
    highlightNode();
    delete link.dataset.attracting;
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
  if (!playing) {
    resetMist();
    clearAttraction();
  }
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
  if (!desktop) clearAttraction();
  else measureParticleFields();
  pointerBounds = undefined;
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
