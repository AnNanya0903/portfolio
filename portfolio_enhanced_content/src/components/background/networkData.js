// Deterministic layout for the AI-network layer of the background.
// Everything here is plain data so you can tweak counts/regions without touching JSX.

// Tunables ------------------------------------------------------------
// Desktop canvas (wide) and mobile canvas (portrait) are generated separately
// so neither gets cropped to an empty middle.
export const DESKTOP = {
  w: 1440, h: 900,
  count: 34,              // total network nodes
  safe: [0.3, 0.7],       // x-range (fraction of width) kept mostly empty for content
  maxLen: 230,            // connect nodes closer than this (viewBox units)
  spacing: 95,
  flow: 9,                // edges with a travelling light pulse
  seed: 20260,
};
export const MOBILE = {
  w: 390, h: 844,
  count: 22,
  safe: null,             // full width; cards are opaque enough to keep text readable
  maxLen: 190,
  spacing: 80,
  flow: 5,
  seed: 777,
};
const MAX_LINKS_PER_NODE = 3;
// ---------------------------------------------------------------------

function mulberry32(a) {
  return function () {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function buildNodes(cfg, rand) {
  const nodes = [];
  let guard = 0;
  while (nodes.length < cfg.count && guard++ < 4000) {
    const x = 12 + rand() * (cfg.w - 24);
    const y = 40 + rand() * (cfg.h - 80);
    const fx = x / cfg.w;
    // keep the middle band sparse (a few nodes allowed so it doesn't feel cut in half)
    if (cfg.safe && fx > cfg.safe[0] && fx < cfg.safe[1] && rand() > 0.12) continue;
    // minimum spacing
    if (nodes.some((n) => Math.hypot(n.x - x, n.y - y) < cfg.spacing)) continue;
    const r = rand();
    nodes.push({
      id: nodes.length,
      x: Math.round(x),
      y: Math.round(y),
      r: r > 0.82 ? 4 : r > 0.4 ? 2.8 : 2,
      hub: r > 0.82,
      color: ['blue', 'cyan', 'violet'][Math.floor(rand() * 3)],
      pulseDelay: +(rand() * 8).toFixed(1),
      pulse: rand() > 0.55,
    });
  }
  return nodes;
}

function buildEdges(nodes, cfg) {
  const edges = [];
  const degree = new Map(nodes.map((n) => [n.id, 0]));
  const key = (a, b) => (a < b ? `${a}-${b}` : `${b}-${a}`);
  const seen = new Set();

  nodes.forEach((a) => {
    const near = nodes
      .filter((b) => b.id !== a.id)
      .map((b) => ({ b, d: Math.hypot(a.x - b.x, a.y - b.y) }))
      .filter((o) => o.d < cfg.maxLen)
      .sort((p, q) => p.d - q.d);
    for (const { b } of near) {
      if (degree.get(a.id) >= MAX_LINKS_PER_NODE) break;
      if (degree.get(b.id) >= MAX_LINKS_PER_NODE) continue;
      const k = key(a.id, b.id);
      if (seen.has(k)) continue;
      seen.add(k);
      degree.set(a.id, degree.get(a.id) + 1);
      degree.set(b.id, degree.get(b.id) + 1);
      edges.push({ a: a.id, b: b.id, flow: false });
    }
  });

  // Choose the longest edges for flowing pulses so the motion is readable but sparse.
  const byLen = edges
    .map((e, i) => ({ i, d: Math.hypot(nodes[e.a].x - nodes[e.b].x, nodes[e.a].y - nodes[e.b].y) }))
    .sort((p, q) => q.d - p.d)
    .slice(0, cfg.flow);
  byLen.forEach(({ i }, n) => {
    edges[i].flow = true;
    edges[i].dur = 11 + (n % 5) * 2.5;      // seconds (scaled by --bg-speed in CSS)
    edges[i].delay = -(n * 2.3);
  });

  return edges;
}

export function createNetwork(cfg) {
  const rand = mulberry32(cfg.seed);
  const nodes = buildNodes(cfg, rand);
  return { nodes, edges: buildEdges(nodes, cfg), w: cfg.w, h: cfg.h };
}

export const desktopNet = createNetwork(DESKTOP);
export const mobileNet = createNetwork(MOBILE);

// Workflow pipelines: a main trunk with branching paths and small "stage" blocks.
// Hand-drawn so they read as automation flows rather than random lines.
export const pipelines = [
  {
    id: 'left',
    // trunk + two branches that rejoin
    d: 'M -20 640 H 120 Q 150 640 150 610 V 560 Q 150 530 180 530 H 300 M 150 610 V 700 Q 150 730 180 730 H 330 M 300 530 H 380 Q 410 530 410 560 V 600 Q 410 630 380 630 H 330 Q 300 630 300 660 V 700',
    stages: [
      { x: 120, y: 640 },
      { x: 300, y: 530 },
      { x: 330, y: 730 },
    ],
    dur: 18,
    delay: 0,
  },
  {
    id: 'right',
    d: 'M 1460 250 H 1330 Q 1300 250 1300 280 V 330 Q 1300 360 1270 360 H 1150 M 1300 280 V 190 Q 1300 160 1270 160 H 1110 M 1150 360 H 1070 Q 1040 360 1040 330 V 300',
    stages: [
      { x: 1330, y: 250 },
      { x: 1150, y: 360 },
      { x: 1110, y: 160 },
    ],
    dur: 21,
    delay: -7,
  },
];

// Tiny syntax-ish glyphs: abstract, small, low contrast. Placed near edges only.
export const glyphs = [
  { t: '{ }', x: 70, y: 120, s: 15 },
  { t: '</>', x: 250, y: 330, s: 14 },
  { t: '=>', x: 90, y: 820, s: 14 },
  { t: 'λ', x: 410, y: 800, s: 16 },
  { t: '[ ]', x: 1360, y: 90, s: 15 },
  { t: '()', x: 1230, y: 480, s: 14 },
  { t: '::', x: 1380, y: 700, s: 14 },
  { t: '01', x: 1110, y: 840, s: 13 },
  { t: '&&', x: 40, y: 400, s: 13 },
];
