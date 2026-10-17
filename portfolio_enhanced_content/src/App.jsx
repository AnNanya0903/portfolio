import React, { useEffect } from 'react';
import Navbar from './components/Navbar';
import Hero from './components/Hero';
import Skills from './components/Skills';
import About from './components/About';
import Experience from './components/Experience';
import Projects from './components/Projects';
import Certificates from './components/Certificates';
import Contact from './components/Contact';
import Footer from './components/Footer';
import BackToTop from './components/BackToTop';
import AIBackground from './components/background/AIBackground';
import CursorSpotlight from './components/CursorSpotlight';
import TechMarquee from './components/TechMarquee';
import Preloader from './components/Preloader';
import CommandPalette from './components/CommandPalette';

function App() {
  // Always dark mode
  useEffect(() => {
    document.body.classList.add('dark');
  }, []);

  // Scroll parallax for the decorative section blobs: each section's blobs
  // drift based on how far the section is from the viewport centre.
  useEffect(() => {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

    const sections = Array.from(document.querySelectorAll('.animated-section'));
    if (!sections.length) return;

    let raf = 0;
    const update = () => {
      raf = 0;
      const viewportCentre = window.innerHeight / 2;
      sections.forEach((section) => {
        const rect = section.getBoundingClientRect();
        const offset = (rect.top + rect.height / 2 - viewportCentre) / window.innerHeight;
        section.style.setProperty('--blob-shift', (-offset * 26).toFixed(1));
      });
    };

    const onScroll = () => {
      if (!raf) raf = requestAnimationFrame(update);
    };

    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll);
    update();

    return () => {
      window.removeEventListener('scroll', onScroll);
      window.removeEventListener('resize', onScroll);
      if (raf) cancelAnimationFrame(raf);
    };
  }, []);

  return (
    <div className="App">
      <Preloader />
      <AIBackground />
      <CursorSpotlight />
      <Navbar />
      <Hero />
      <TechMarquee />
      <About />
      <Experience />
      <Skills />
      <Projects />
      <Certificates />
      <Contact />
      <Footer />
      <BackToTop />
      <CommandPalette />
    </div>
  );
}

export default App;
