import React, { useEffect, useRef } from 'react';

/**
 * Soft radial glow that trails the pointer across the page.
 * Sits above the AI background but below all content (z-index: 0), and is
 * disabled on touch devices and for users who prefer reduced motion.
 */
const CursorSpotlight = () => {
  const ref = useRef(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    const canHover = window.matchMedia('(hover: hover) and (pointer: fine)').matches;
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (!canHover || reduceMotion) return;

    let raf = 0;
    let targetX = window.innerWidth / 2;
    let targetY = window.innerHeight / 2;
    let currentX = targetX;
    let currentY = targetY;

    const render = () => {
      currentX += (targetX - currentX) * 0.16;
      currentY += (targetY - currentY) * 0.16;
      el.style.transform = `translate3d(${currentX}px, ${currentY}px, 0)`;
      const moving = Math.abs(targetX - currentX) > 0.4 || Math.abs(targetY - currentY) > 0.4;
      raf = moving ? requestAnimationFrame(render) : 0;
    };

    const onMove = (e) => {
      targetX = e.clientX;
      targetY = e.clientY;
      el.classList.add('is-active');
      if (!raf) raf = requestAnimationFrame(render);
    };

    const onLeave = () => el.classList.remove('is-active');

    window.addEventListener('mousemove', onMove, { passive: true });
    document.addEventListener('mouseleave', onLeave);
    raf = requestAnimationFrame(render);

    return () => {
      window.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseleave', onLeave);
      if (raf) cancelAnimationFrame(raf);
    };
  }, []);

  return <div className="cursor-spotlight" ref={ref} aria-hidden="true" />;
};

export default CursorSpotlight;
