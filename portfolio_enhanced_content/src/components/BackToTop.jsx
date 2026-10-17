import React, { useEffect, useState } from 'react';

const BackToTop = () => {
  const [visible, setVisible] = useState(false);
  const [progress, setProgress] = useState(0);

  useEffect(() => {
    const onScroll = () => {
      const doc = document.documentElement;
      const max = doc.scrollHeight - doc.clientHeight;
      setProgress(max > 0 ? doc.scrollTop / max : 0);
      setVisible(doc.scrollTop > 500);
    };
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => window.removeEventListener('scroll', onScroll);
  }, []);

  const radius = 22;
  const circumference = 2 * Math.PI * radius;

  return (
    <button
      type="button"
      className={`back-to-top ${visible ? 'is-visible' : ''}`}
      onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}
      aria-label="Back to top"
    >
      <svg className="back-to-top__ring" viewBox="0 0 52 52" aria-hidden="true">
        <circle className="back-to-top__track" cx="26" cy="26" r={radius} />
        <circle
          className="back-to-top__bar"
          cx="26"
          cy="26"
          r={radius}
          style={{
            strokeDasharray: circumference,
            strokeDashoffset: circumference * (1 - progress),
          }}
        />
      </svg>
      <i className="fas fa-arrow-up"></i>
    </button>
  );
};

export default BackToTop;
