import React, { useEffect, useState } from 'react';

/**
 * Short branded intro overlay on first load. Fades out automatically and
 * unmounts itself; skipped entirely for reduced-motion users.
 */
const Preloader = () => {
  const [fading, setFading] = useState(false);
  const [removed, setRemoved] = useState(false);

  useEffect(() => {
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduceMotion) {
      setRemoved(true);
      return;
    }

    document.body.style.overflow = 'hidden';

    const fadeTimer = setTimeout(() => setFading(true), 1150);
    const removeTimer = setTimeout(() => {
      setRemoved(true);
      document.body.style.overflow = '';
    }, 1800);

    return () => {
      clearTimeout(fadeTimer);
      clearTimeout(removeTimer);
      document.body.style.overflow = '';
    };
  }, []);

  if (removed) return null;

  return (
    <div className={`preloader ${fading ? 'is-done' : ''}`} aria-hidden="true">
      <div className="preloader__inner">
        <div className="preloader__mark">AYA</div>
        <div className="preloader__bar"><span /></div>
        <p className="preloader__text">Loading portfolio</p>
      </div>
    </div>
  );
};

export default Preloader;
