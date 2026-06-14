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
      date: '2026-02-25',
      description: 'Elite Gold Course Completion Certificate for Data Analytics with Python.',
      gradient: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
      icon: 'fa-chart-line'
    },
    {
      title: 'Artificial Intelligence Internship',
      issuer: 'Edutainer (PAT Technologies)',
      date: '2026-02-02 to 2026-05-31',
      description: 'Internship completion in Artificial Intelligence with hands-on projects and practical tasks.',
      gradient: 'linear-gradient(135deg, #f093fb 0%, #f5576c 100%)',
      icon: 'fa-brain'
    },
    {
      title: 'Get Started with AI on Azure',
      issuer: 'Microsoft',
      date: '2023-01-07',
      description: 'Microsoft Azure AI certification covering fundamentals of AI on Microsoft Azure.',
      gradient: 'linear-gradient(135deg, #0078d4 0%, #00bcf2 100%)',
      icon: 'fa-cloud'
    },
    {
      title: 'Work with AI Visuals in Power BI',
      issuer: 'Microsoft',
      date: '2023-02-14',
      description: 'Certification for working with AI-powered visualizations in Microsoft Power BI.',
      gradient: 'linear-gradient(135deg, #f2c811 0%, #002050 100%)',
      icon: 'fa-chart-bar'
    },
    {
      title: 'Full Stack Development Internship',
      issuer: 'Nayoda',
      date: '2026-03-11 to 2026-04-08',
      description: 'Full Stack Development internship demonstrating skills and professionalism.',
      gradient: 'linear-gradient(135deg, #0f3460 0%, #533483 100%)',
      icon: 'fa-code'
    },
    {
      title: 'SCOSS Services',
      issuer: 'SCOSS',
      date: '2026',
      description: 'Certificate for completion of SCOSS Services training and certification.',
      gradient: 'linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%)',
      icon: 'fa-certificate'
    }
  ];

  return (
    <section id="certificates" className={`section skills animated-section ${isVisible ? 'scroll-visible' : 'scroll-hidden'}`} ref={ref}>
      <h2>{headingText}<span>Achievements</span></h2>
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
