import React from 'react';
import { useTypingEffect } from '../hooks/useTypingEffect';
import { useScrollAnimation } from '../hooks/useScrollAnimation';
import { useCountUp } from '../hooks/useCountUp';

const About = () => {
  const headingText = useTypingEffect(['About ']);
  const [ref, isVisible] = useScrollAnimation(0.1);

  const projectsCount = useCountUp(14, isVisible);
  const internshipsCount = useCountUp(3, isVisible);
  const techCount = useCountUp(40, isVisible);

  const bento = [
    {
      icon: 'fa-brain',
      title: 'AI & LLM Engineering',
      text: 'RAG pipelines, LangGraph agents, NLP and LLM-powered tools that turn messy real-world input into structured results.',
      span: 'wide',
      accent: 'ai',
    },
    {
      icon: 'fa-layer-group',
      title: 'Full-Stack Delivery',
      text: 'FastAPI and Node backends, React frontends, REST APIs, JWT/RBAC auth and real-time Socket.IO features.',
      accent: 'stack',
    },
    {
      icon: 'fa-gears',
      title: 'Workflow Automation',
      text: 'Make scenarios wired to Google Forms, Sheets and Supabase.',
      accent: 'automation',
    },
    {
      icon: 'fa-graduation-cap',
      title: 'Education',
      text: 'MCA in Full Stack Development, Bangalore Institute of Technology — CGPA 9.4.',
      accent: 'education',
    },
    {
      icon: 'fa-terminal',
      title: 'Tech I Reach For',
      chips: ['Python', 'FastAPI', 'React', 'Node.js', 'TypeScript', 'PostgreSQL', 'MongoDB', 'LangGraph', 'Supabase', 'Make'],
      span: 'wide',
      accent: 'tech',
    },
    {
      icon: 'fa-location-dot',
      title: 'Open To Work',
      text: 'Full-stack and AI engineering roles, plus select freelance projects. Based in Tumkur, Karnataka, India.',
      accent: 'available',
    },
  ];

  return (
    <section
      id="about"
      className={`section about animated-section ${isVisible ? 'scroll-visible' : 'scroll-hidden'}`}
      ref={ref}
    >
      <h2>{headingText}<span>Me</span></h2>
      <div className="about-container">
        <div className="about-image">
          <img src="/profile_pic.png" alt="Profile" className="about-illustration" />
        </div>
        <div className="about-content">
          <p className="about-text">I'm a full-stack developer and AI engineer pursuing my MCA in Full Stack Development at Bangalore Institute of Technology (CGPA 9.4). I build end-to-end products: Python and FastAPI backends, React frontends, and LLM-powered agents that turn messy, real-world input into structured, useful results.</p>
          <p className="about-text">My work spans an AI-first CRM, RAG-based document Q&amp;A, a transparent candidate-ranking engine, and civic-tech platforms. Through three internships in AI engineering and web development, I've learned to value clean architecture, readable code, and shipping features people actually use.</p>
          <p className="about-text">I'm open to full-stack and AI engineering roles, and I take on select freelance projects.</p>
          <div className="about-stats">
            <div className="stat-item">
              <span className="stat-number">{Math.round(projectsCount)}</span>
              <span className="stat-label">Projects Built</span>
            </div>
            <div className="stat-item">
              <span className="stat-number">{Math.round(internshipsCount)}</span>
              <span className="stat-label">Internships Completed</span>
            </div>
            <div className="stat-item">
              <span className="stat-number">{Math.round(techCount)}+</span>
              <span className="stat-label">Technologies</span>
            </div>
          </div>
        </div>
      </div>

      <div className="about-bento">
        {bento.map((tile, index) => (
          <article
            key={tile.title}
            className={`bento-tile bento-tile--${tile.accent}${tile.span ? ` bento-tile--${tile.span}` : ''}`}
            style={{ animationDelay: `${index * 0.08}s` }}
          >
            <span className="bento-tile__icon" aria-hidden="true">
              <i className={`fas ${tile.icon}`}></i>
            </span>
            <h3 className="bento-tile__title">{tile.title}</h3>
            {tile.text && <p className="bento-tile__text">{tile.text}</p>}
            {tile.chips && (
              <div className="bento-tile__chips">
                {tile.chips.map((chip) => (
                  <span key={chip} className="bento-chip">{chip}</span>
                ))}
              </div>
            )}
          </article>
        ))}
      </div>
    </section>
  );
};

export default About;
