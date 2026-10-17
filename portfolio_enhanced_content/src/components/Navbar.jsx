import React, { useState, useEffect } from 'react';
import { SECTIONS } from '../data/site';

const Navbar = () => {
  const [menuOpen, setMenuOpen] = useState(false);
  const [isMobile, setIsMobile] = useState(false);
  const [scrolled, setScrolled] = useState(false);
  const [progress, setProgress] = useState(0);
  const [activeId, setActiveId] = useState('home');

  useEffect(() => {
    const checkMobile = () => setIsMobile(window.innerWidth < 900);
    checkMobile();
    window.addEventListener('resize', checkMobile);
    return () => window.removeEventListener('resize', checkMobile);
  }, []);

  useEffect(() => {
    const onScroll = () => {
      const doc = document.documentElement;
      const max = doc.scrollHeight - doc.clientHeight;
      setProgress(max > 0 ? (doc.scrollTop / max) * 100 : 0);
      setScrolled(doc.scrollTop > 40);
    };
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => window.removeEventListener('scroll', onScroll);
  }, []);

  useEffect(() => {
    const targets = SECTIONS
      .map((section) => document.getElementById(section.id))
      .filter(Boolean);
    if (!targets.length) return undefined;

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) setActiveId(entry.target.id);
        });
      },
      { rootMargin: '-45% 0px -50% 0px', threshold: 0 }
    );

    targets.forEach((target) => observer.observe(target));
    return () => observer.disconnect();
  }, []);

  const handleSmoothScroll = (e, targetId) => {
    e.preventDefault();
    setMenuOpen(false);
    const target = document.querySelector(targetId);
    if (target) {
      target.scrollIntoView({ behavior: 'smooth' });
    }
  };

  return (
    <header className={scrolled ? 'scrolled' : ''}>
      <nav className="navbar">
        <div className="logo">Ananya<span>Gowda</span></div>

        {isMobile && (
          <button
            className="mobile-menu-btn"
            onClick={() => setMenuOpen(!menuOpen)}
            aria-label="Toggle navigation menu"
          >
            <i className={`fas ${menuOpen ? 'fa-times' : 'fa-bars'}`}></i>
          </button>
        )}

        <ul className={`nav-links ${menuOpen ? 'active' : ''}`}>
          {SECTIONS.map((section) => (
            <li key={section.id}>
              <a
                href={`#${section.id}`}
                className={activeId === section.id ? 'active' : ''}
                onClick={(e) => handleSmoothScroll(e, `#${section.id}`)}
              >
                {section.label}
              </a>
            </li>
          ))}
          <li className="nav-cmdk-item">
            <button
              type="button"
              className="nav-cmdk"
              onClick={() => window.dispatchEvent(new Event('toggle-command-palette'))}
              aria-label="Open command palette (Ctrl or Cmd + K)"
            >
              <i className="fas fa-magnifying-glass"></i>
              <kbd>⌘K</kbd>
            </button>
          </li>
        </ul>
      </nav>
      <div className="scroll-progress" style={{ width: `${progress}%` }}></div>
    </header>
  );
};

export default Navbar;
