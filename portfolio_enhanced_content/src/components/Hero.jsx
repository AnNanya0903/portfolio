import React, { useEffect, useRef } from 'react';
import { useTypingEffect } from '../hooks/useTypingEffect';

const Hero = () => {
  const headingText = useTypingEffect(["Hello, I'm "]);
  const heroRef = useRef(null);

  // Pointer parallax + scroll fade for the hero layer.
  useEffect(() => {
    const el = heroRef.current;
    if (!el) return;

    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const canHover = window.matchMedia('(hover: hover) and (pointer: fine)').matches;

    let pointerRaf = 0;
    let targetX = 0;
    let targetY = 0;
    let currentX = 0;
    let currentY = 0;

    const renderPointer = () => {
      currentX += (targetX - currentX) * 0.09;
      currentY += (targetY - currentY) * 0.09;
      el.style.setProperty('--mx', currentX.toFixed(3));
      el.style.setProperty('--my', currentY.toFixed(3));
      const moving = Math.abs(targetX - currentX) > 0.003 || Math.abs(targetY - currentY) > 0.003;
      pointerRaf = moving ? requestAnimationFrame(renderPointer) : 0;
    };

    const onPointerMove = (e) => {
      const rect = el.getBoundingClientRect();
      targetX = ((e.clientX - rect.left) / rect.width - 0.5) * 2;
      targetY = ((e.clientY - rect.top) / rect.height - 0.5) * 2;
      if (!pointerRaf) pointerRaf = requestAnimationFrame(renderPointer);
    };

    const onPointerLeave = () => {
      targetX = 0;
      targetY = 0;
      if (!pointerRaf) pointerRaf = requestAnimationFrame(renderPointer);
    };

    let scrollRaf = 0;
    const updateScroll = () => {
      scrollRaf = 0;
      const height = el.offsetHeight || 1;
      const progress = Math.min(1, Math.max(0, window.scrollY / height));
      el.style.setProperty('--hero-fade', progress.toFixed(3));
    };
    const onScroll = () => {
      if (!scrollRaf) scrollRaf = requestAnimationFrame(updateScroll);
    };

    if (!reduceMotion) {
      window.addEventListener('scroll', onScroll, { passive: true });
      updateScroll();
    }

    if (canHover && !reduceMotion) {
      el.addEventListener('mousemove', onPointerMove);
      el.addEventListener('mouseleave', onPointerLeave);
    }

    return () => {
      window.removeEventListener('scroll', onScroll);
      el.removeEventListener('mousemove', onPointerMove);
      el.removeEventListener('mouseleave', onPointerLeave);
      if (pointerRaf) cancelAnimationFrame(pointerRaf);
      if (scrollRaf) cancelAnimationFrame(scrollRaf);
    };
  }, []);

  const handleSmoothScroll = (e, targetId) => {
    e.preventDefault();
    const target = document.querySelector(targetId);
    if (target) {
      target.scrollIntoView({ behavior: 'smooth' });
    }
  };

  const typingText = useTypingEffect([
    "Building AI-powered products, from backend to interface.",
    "LLM agents, RAG pipelines, and full-stack web apps.",
    "Clean code, practical AI, and software that ships.",
    "Turning complex problems into reliable, scalable software."
  ]);

  const orbitNodes = [
    { icon: 'fab fa-react', cls: 'fn-1', color: '#61dafb' },
    { icon: 'fab fa-js', cls: 'fn-2', color: '#f7df1e' },
    { icon: 'fab fa-python', cls: 'fn-3', color: '#6fb3e0' },
    { icon: 'fas fa-database', cls: 'fn-4', color: '#a78bfa' },
    { icon: 'fas fa-brain', cls: 'fn-5', color: '#f472b6' },
    { icon: 'fab fa-node-js', cls: 'fn-6', color: '#68a063' },
  ];

  const particles = Array.from({ length: 10 }, (_, i) => ({
    left: `${6 + i * 9}%`,
    delay: `${i * 0.9}s`,
    duration: `${9 + (i % 4) * 2}s`,
    size: `${3 + (i % 3)}px`,
  }));

  return (
    <section id="home" className="hero" ref={heroRef}>
      <div className="hero-decor" aria-hidden="true">
        <div className="hero-orb hero-orb-1"></div>
        <div className="hero-orb hero-orb-2"></div>
        <div className="hero-orb hero-orb-3"></div>
        {particles.map((p, i) => (
          <span
            key={i}
            className="hero-dot"
            style={{
              left: p.left,
              width: p.size,
              height: p.size,
              animationDelay: p.delay,
              animationDuration: p.duration,
            }}
          />
        ))}
      </div>

      <div className="hero-container">
        <div className="hero-content">
          <div className="hero-badge">
            <span className="badge-dot"></span>Full-Stack Developer &amp; AI Engineer
          </div>
          <h1>{headingText}<span>Ananya Y A</span></h1>
          <p className="hero-title">{typingText}</p>

          <div className="hero-buttons">
            <div className="btn-group">
              <a href="#projects" className="btn" onClick={(e) => handleSmoothScroll(e, '#projects')}>
                View My Work
              </a>
              <a href="#contact" className="btn" style={{ background: 'linear-gradient(135deg, #10b981, #059669)', border: 'none' }} onClick={(e) => handleSmoothScroll(e, '#contact')}>
                Get In Touch
              </a>
            </div>
            <div className="social-icons" style={{ marginTop: '1.5rem', display: 'flex', justifyContent: 'center', gap: '1.5rem', animation: 'fadeInUp 0.8s ease-out 0.9s forwards', opacity: 0 }}>
              <a href="https://github.com/AnNanya0903" target="_blank" rel="noopener noreferrer" className="social-link">
                <i className="fab fa-github"></i>
              </a>
              <a href="https://linkedin.com/in/ananyagowda" target="_blank" rel="noopener noreferrer" className="social-link">
                <i className="fab fa-linkedin"></i>
              </a>
              <a href="mailto:ananyagowda0903@gmail.com" className="social-link">
                <i className="fas fa-envelope"></i>
              </a>
            </div>
          </div>
        </div>

        <div className="hero-visual">
          <div className="hero-3d">
            <div className="avatar-container">
              <div className="avatar-glow"></div>
              <div className="avatar-circle">
                <i className="fas fa-robot"></i>
              </div>
              {orbitNodes.map((node, i) => (
                <div key={i} className={`floating-element ${node.cls}`}>
                  <i className={node.icon} style={{ color: node.color }}></i>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      <a href="#about" className="scroll-cue" onClick={(e) => handleSmoothScroll(e, '#about')} aria-label="Scroll to about">
        <span></span>
      </a>
    </section>
  );
};

export default Hero;
