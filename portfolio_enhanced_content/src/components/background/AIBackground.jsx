import React from 'react';
import { desktopNet, mobileNet, pipelines, glyphs } from './networkData';
import './background.css';

/**
 * Fixed, non-interactive background layer.
 *   1. gradient + ambient glows   (CSS)
 *   2. technical grid             (CSS)
 *   3. AI network + pipelines     (inline SVG, CSS animations)
 *   4. vignette                   (CSS)
 * Colours, speed and strength are CSS variables in background.css.
 */
const NetworkSvg = ({ net, variant, extras }) => {
  const { nodes, edges, w, h } = net;
  const id = `ai-${variant}`;
  return (
    <svg
      className={`ai-bg__svg ai-bg__svg--${variant}`}
      viewBox={`0 0 ${w} ${h}`}
      preserveAspectRatio="xMidYMid slice"
      focusable="false"
    >
      <defs>
        {/* fades the network toward the centre where content lives */}
        <radialGradient id={`${id}-fade`} cx="50%" cy="50%" r="62%">
          <stop offset="0" stopColor="#fff" stopOpacity={extras ? 0.15 : 0.5} />
          <stop offset="0.55" stopColor="#fff" stopOpacity={extras ? 0.55 : 0.8} />
          <stop offset="1" stopColor="#fff" stopOpacity="1" />
        </radialGradient>
        <mask id={`${id}-mask`}>
          <rect width={w} height={h} fill={`url(#${id}-fade)`} />
        </mask>
      </defs>

      <g mask={`url(#${id}-mask)`}>
        <g className="ai-bg__edges">
          {edges.map((e, i) => (
            <line key={`e${i}`} x1={nodes[e.a].x} y1={nodes[e.a].y} x2={nodes[e.b].x} y2={nodes[e.b].y} />
          ))}
        </g>

        {/* travelling light along a few edges */}
        <g className="ai-bg__flows">
          {edges
            .filter((e) => e.flow)
            .map((e, i) => (
              <line
                key={`f${i}`}
                x1={nodes[e.a].x} y1={nodes[e.a].y} x2={nodes[e.b].x} y2={nodes[e.b].y}
                pathLength="100"
                style={{ '--dur': `${e.dur}s`, animationDelay: `${e.delay}s` }}
                className={i % 3 === 0 ? 'is-violet' : i % 3 === 1 ? 'is-cyan' : 'is-blue'}
              />
            ))}
        </g>

        <g className="ai-bg__nodes">
          {nodes.map((n) => (
            <g key={`n${n.id}`} className={`node is-${n.color}${n.pulse ? ' is-pulsing' : ''}`}>
              {n.hub && <circle className="node__halo" cx={n.x} cy={n.y} r={n.r * 4} />}
              <circle className="node__core" cx={n.x} cy={n.y} r={n.r} style={{ animationDelay: `${n.pulseDelay}s` }} />
            </g>
          ))}
        </g>

        {extras && (
          <>
            {/* workflow pipelines */}
            <g className="ai-bg__pipes">
              {pipelines.map((p) => (
                <g key={p.id}>
                  <path className="pipe" d={p.d} />
                  <path className="pipe-flow" d={p.d} pathLength="100" style={{ '--dur': `${p.dur}s`, animationDelay: `${p.delay}s` }} />
                  {p.stages.map((s, i) => (
                    <rect key={i} className="stage" x={s.x - 6} y={s.y - 6} width="12" height="12" rx="3" />
                  ))}
                </g>
              ))}
            </g>
            {/* abstract code glyphs */}
            <g className="ai-bg__glyphs">
              {glyphs.map((g, i) => (
                <text key={i} x={g.x} y={g.y} fontSize={g.s}>{g.t}</text>
              ))}
            </g>
          </>
        )}
      </g>
    </svg>
  );
};

const AIBackground = () => (
  <div className="ai-bg" aria-hidden="true">
    <div className="ai-bg__glow ai-bg__glow--blue" />
    <div className="ai-bg__glow ai-bg__glow--violet" />
    <div className="ai-bg__glow ai-bg__glow--cyan" />
    <div className="ai-bg__grid" />

    <NetworkSvg net={desktopNet} variant="desktop" extras />
    <NetworkSvg net={mobileNet} variant="mobile" />

    <div className="ai-bg__vignette" />
  </div>
);

export default AIBackground;
