import React from 'react';
import { useTypingEffect } from '../hooks/useTypingEffect';
import { useScrollAnimation } from '../hooks/useScrollAnimation';

const Certificates = () => {
  const headingText = useTypingEffect(['Certificates & ']);
  const [ref, isVisible] = useScrollAnimation(0.1);
  
  const certificates = [
    {
      title: 'Data Analytics with Python',
      issuer: 'Visvesvaraya Technological University (VTU)',
      date: 'Feb 2026',
      description: 'Elite Gold course completion certificate for data analytics with Python.',
      gradient: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
      icon: 'fa-chart-line'
    },
    {
      title: 'Artificial Intelligence Internship',
      issuer: 'Edutainer (PAT Technologies)',
      date: 'Feb – May 2026',
      description: 'Completed an AI internship with hands-on projects and practical tasks.',
      gradient: 'linear-gradient(135deg, #f093fb 0%, #f5576c 100%)',
      icon: 'fa-brain'
    },
    {
      title: 'Get Started with AI on Azure',
      issuer: 'Microsoft',
      date: 'Jan 2023',
      description: 'Microsoft certification covering the fundamentals of AI on Azure.',
      gradient: 'linear-gradient(135deg, #0078d4 0%, #00bcf2 100%)',
      icon: 'fa-cloud'
    },
    {
      title: 'Work with AI Visuals in Power BI',
      issuer: 'Microsoft',
      date: 'Feb 2023',
      description: 'Microsoft certification on building AI-powered visuals in Power BI.',
      gradient: 'linear-gradient(135deg, #f2c811 0%, #002050 100%)',
      icon: 'fa-chart-bar'
    },
    {
      title: 'Full Stack Development Internship',
      issuer: 'Nayoda',
      date: 'Mar – Apr 2026',
      description: 'Completed a four-week full-stack development internship.',
      gradient: 'linear-gradient(135deg, #0f3460 0%, #533483 100%)',
      icon: 'fa-code'
    },
    {
      title: 'SCOSS Services Training',
      issuer: 'SCOSS',
      date: '2026',
      description: 'Certificate for completing SCOSS Services training and certification.',
      gradient: 'linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%)',
      icon: 'fa-certificate'
    }
  ];

  return (
    <section id="certificates" className={`section skills animated-section ${isVisible ? 'scroll-visible' : 'scroll-hidden'}`} ref={ref}>
      <h2>{headingText}<span>Achievements</span></h2>
      <p>Credentials in AI, data analytics, and full-stack development</p>
      <div className="project-grid">
        {certificates.map((cert, index) => (
          <div key={index} className="project-card" style={{ animationDelay: `${index * 0.1}s` }}>
            <div className="project-icon" style={{ background: cert.gradient }}>
              <i className={`fas ${cert.icon}`}></i>
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
