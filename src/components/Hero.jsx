import React from 'react';
import { useTypingEffect } from '../hooks/useTypingEffect';

const Hero = () => {
  const handleSmoothScroll = (e, targetId) => {
    e.preventDefault();
    const target = document.querySelector(targetId);
    if (target) {
      target.scrollIntoView({ behavior: 'smooth' });
    }
  };

  const typingText = useTypingEffect([
    "I build software solutions.",
    "I create AI-based tools.",
    "I develop web applications.",
    "I design database-driven systems."
  ]);

  return (
    <section id="home" className="hero">
      <div className="hero-bg-shapes">
        <div className="bg-shape shape-1"></div>
        <div className="bg-shape shape-2"></div>
        <div className="bg-shape shape-3"></div>
      </div>
      <div className="hero-container">
        <div className="hero-content">
          <div className="hero-badge">Web Developer & AI Enthusiast</div>
          <h1>Hello, I'm <span>Ananya Y A</span></h1>
          <p className="hero-title">{typingText}</p>
          <div className="hero-buttons">
            <div className="btn-group">
              <a href="#projects" className="btn" onClick={(e) => handleSmoothScroll(e, '#projects')}>
                View My Work
              </a>
              <a href="#contact" className="btn outline" onClick={(e) => handleSmoothScroll(e, '#contact')}>
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
              <div className="avatar-circle">
                <i className="fas fa-laptop-code"></i>
              </div>
              <div className="avatar-glow"></div>
              <div className="floating-element element-1">
                <i className="fab fa-react"></i>
              </div>
              <div className="floating-element element-2">
                <i className="fab fa-js"></i>
              </div>
              <div className="floating-element element-3">
                <i className="fab fa-python"></i>
              </div>
              <div className="floating-element element-4">
                <i className="fas fa-database"></i>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
};

export default Hero;
