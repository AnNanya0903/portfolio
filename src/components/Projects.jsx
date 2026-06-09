import React from 'react';
import { useScrollAnimation } from '../hooks/useScrollAnimation';

const Projects = () => {
  const [ref, isVisible] = useScrollAnimation(0.1);

  const projects = [
    {
      title: 'CogniFlow Enterprise Intelligence',
      description: 'An advanced AI-driven productivity suite featuring a cognitive voice interface, intelligent document Q&A (RAG), and real-time strategic analytics. Built for high-performance teams to automate complex professional workflows.',
      github: 'https://github.com/AnNanya0903/CogniFlow-Enterprise-Intelligence',
      gradient: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
      icon: 'fa-brain'
    },
    {
      title: 'Secure Vote Bot',
      description: 'A secure, transparent, and decentralized e-voting system built using blockchain technology to ensure tamper-proof elections, real-time result verification, and trustless vote counting.',
      github: 'https://github.com/AnNanya0903/secure-vote-bot',
      gradient: 'linear-gradient(135deg, #11998e 0%, #38ef7d 100%)',
      icon: 'fa-vote-yea'
    },
    {
      title: 'Music Recommendation System',
      description: 'An AI-powered music recommendation system that detects a user’s facial emotion in real time using a webcam and automatically plays a Spotify playlist that matches the detected mood.',
      github: 'https://github.com/AnNanya0903/music_recommendation',
      gradient: 'linear-gradient(135deg, #ff9a9e 0%, #fad0c4 99%, #fad0c4 100%)',
      icon: 'fa-music'
    },
    {
      title: 'Blog Platform',
      description: 'A full-stack web-based application designed to help users create and manage blog posts efficiently, with user authentication, post creation with rich text formatting, and content management features.',
      github: 'https://github.com/AnNanya0903/Blog_Platform',
      gradient: 'linear-gradient(135deg, #f093fb 0%, #f5576c 100%)',
      icon: 'fa-blog'
    },
    {
      title: 'Maya Therapy',
      description: 'A beautiful, responsive therapist website built with Next.js, featuring an elegant UI for Dr. Maya Reynolds therapy practice.',
      github: 'https://github.com/AnNanya0903/maya-therapy',
      gradient: 'linear-gradient(135deg, #4facfe 0%, #00f2fe 100%)',
      icon: 'fa-heartbeat'
    },
    {
      title: 'Wumpus World AI',
      description: 'A Python-powered AI adventure where an agent navigates a perilous 4x4 grid, avoids deadly pits and the Wumpus, and uses logical reasoning to find the gold with visualization and step-by-step simulation.',
      github: 'https://github.com/AnNanya0903/wumpus_proj_py0002',
      gradient: 'linear-gradient(135deg, #a18cd1 0%, #fbc2eb 100%)',
      icon: 'fa-robot'
    }
  ];

  return (
    <section 
      id="projects" 
      className={`section projects ${isVisible ? 'scroll-visible' : 'scroll-hidden'}`}
      ref={ref}
    >
      <h2>Featured <span>Projects</span></h2>
      <p>Showcasing my technical skills and creative solutions</p>
      <div className="project-grid">
        {projects.map((project, index) => (
          <div key={index} className="project-card">
            <div className="project-icon" style={{ background: project.gradient }}>
              <i className={`fas ${project.icon}`}></i>
            </div>
            <div className="project-scene">
              <div className="scene-shape shape-1"></div>
              <div className="scene-shape shape-2"></div>
            </div>
            <h3>{project.title}</h3>
            <p>{project.description}</p>
            <a href={project.github} target="_blank" rel="noopener noreferrer" className="project-link">
              View on GitHub <i className="fas fa-arrow-right"></i>
            </a>
          </div>
        ))}
      </div>
    </section>
  );
};

export default Projects;
