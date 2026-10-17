import { useEffect, useRef, useState } from 'react';

/**
 * Animates a number from 0 to `target` the first time `isActive` becomes true.
 * Eases out with a cubic curve and runs on requestAnimationFrame.
 */
export const useCountUp = (target, isActive = true, duration = 1600) => {
  const [value, setValue] = useState(0);
  const frame = useRef(null);
  const started = useRef(false);

  useEffect(() => {
    if (!isActive || started.current) return undefined;
    started.current = true;

    const start = performance.now();
    const tick = (now) => {
      const progress = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      setValue(target * eased);
      if (progress < 1) {
        frame.current = requestAnimationFrame(tick);
      }
    };

    frame.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame.current);
  }, [isActive, target, duration]);

  return value;
};
