import React, { useState } from 'react';

const Navbar = ({ isDark, toggleTheme }) => {
  const [menuOpen, setMenuOpen] = useState(false);

  const handleSmoothScroll = (e, targetId) => {
    e.preventDefault();
    setMenuOpen(false);
    const target = document.querySelector(targetId);
    if (target) {
      target.scrollIntoView({ behavior: 'smooth' });
    }
  };

  return (
    <header>
      <nav className="navbar">
        <div className="logo">Ananya<span>Gowda</span></div>
        
        {/* Mobile Menu Button */}
        <button 
          className="mobile-menu-btn"
          onClick={() => setMenuOpen(!menuOpen)}
        >
          <i className={`fas ${menuOpen ? 'fa-times' : 'fa-bars'}`}></i>
        </button>

        {/* Nav Links */}
        <ul className={`nav-links ${menuOpen ? 'active' : ''}`}>
          <li>
            <a href="#home" onClick={(e) => handleSmoothScroll(e, '#home')}>Home</a>
          </li>
          <li>
            <a href="#about" onClick={(e) => handleSmoothScroll(e, '#about')}>About</a>
          </li>
          <li>
            <a href="#skills" onClick={(e) => handleSmoothScroll(e, '#skills')}>Skills</a>
          </li>
          <li>
            <a href="#internship" onClick={(e) => handleSmoothScroll(e, '#internship')}>Internship</a>
          </li>
          <li>
            <a href="#projects" onClick={(e) => handleSmoothScroll(e, '#projects')}>Projects</a>
          </li>
          <li>
            <a href="#certificates" onClick={(e) => handleSmoothScroll(e, '#certificates')}>Certificates</a>
          </li>
          <li>
            <a href="#contact" onClick={(e) => handleSmoothScroll(e, '#contact')}>Contact</a>
          </li>
        </ul>

        <button id="theme-toggle" onClick={toggleTheme}>
          <i className={`fas ${isDark ? 'fa-sun' : 'fa-moon'}`}></i>
        </button>
      </nav>
    </header>
  );
};

export default Navbar;
