import React from 'react';
import { useScrollAnimation } from '../hooks/useScrollAnimation';

const Certificates = () => {
  const [ref, isVisible] = useScrollAnimation(0.1);
  
  const certificates = [
    {
      title: 'Web Development Certificate',
      issuer: 'Online Learning Platform',
      date: '2025',
      description: 'Comprehensive course covering HTML, CSS, JavaScript, and modern web frameworks.',
      gradient: 'linear-gradient(135deg, #a18cd1 0%, #fbc2eb 100%)',
      icon: 'fa-certificate'
    },
    {
      title: 'Python Programming',
      issuer: 'Coding Academy',
      date: '2025',
      description: 'Certification in Python fundamentals, data structures, and object-oriented programming.',
      gradient: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
      icon: 'fa-award'
    },
    {
      title: 'Git & GitHub Mastery',
      issuer: 'DevSchool',
      date: '2025',
      description: 'Advanced Git workflows, branching strategies, and collaboration on GitHub.',
      gradient: 'linear-gradient(135deg, #f093fb 0%, #f5576c 100%)',
      icon: 'fa-code-branch'
    }
  ];

  return (
    <section id="certificates" className={`section skills ${isVisible ? 'scroll-visible' : 'scroll-hidden'}`} ref={ref}>
      <h2>Certificates & <span>Achievements</span></h2>
      <div className="project-grid">
        {certificates.map((cert, index) => (
          <div key={index} className="project-card">
            <div className="project-icon" style={{ background: cert.gradient }}>
              <i className={`fas ${cert.icon}`}></i>
            </div>
            <div className="project-scene">
              <div className="scene-shape shape-1"></div>
              <div className="scene-shape shape-2"></div>
            </div>
            <h3>{cert.title}</h3>
            <p className="project-meta">{cert.issuer} • {cert.date}</p>
            <p>{cert.description}</p>
          </div>
        ))}
      </div>
    </section>
  );
};

export default Certificates;
