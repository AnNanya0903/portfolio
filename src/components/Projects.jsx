import React from 'react';
import { useTypingEffect } from '../hooks/useTypingEffect';
import { useScrollAnimation } from '../hooks/useScrollAnimation';

const Projects = () => {
  const headingText = useTypingEffect(['Featured ']);
  const [ref, isVisible] = useScrollAnimation(0.1);

  const projects = [
    {
      title: 'CogniFlow Enterprise Intelligence',
      description: 'An advanced AI-driven productivity suite featuring a cognitive voice interface, intelligent document Q&A (RAG), and real-time strategic analytics. Built for high-performance teams to automate complex professional workflows.',
      github: 'https://github.com/AnNanya0903/CogniFlow-Enterprise-Intelligence',
      icon: '🧠',
    },
    {
      title: 'Secure Vote Bot',
      description: 'A secure, transparent, and decentralized e-voting system built using blockchain technology to ensure tamper-proof elections, real-time result verification, and trustless vote counting.',
      github: 'https://github.com/AnNanya0903/secure-vote-bot',
      icon: '🗳️',
    },
    {
      title: 'Music Recommendation System',
      description: 'An AI-powered music recommendation system that detects a user’s facial emotion in real time using a webcam and automatically plays a Spotify playlist that matches the detected mood.',
      github: 'https://github.com/AnNanya0903/music_recommendation',
      icon: '🎵',
    },
    {
      title: 'Blog Platform',
      description: 'A full-stack web-based application designed to help users create and manage blog posts efficiently, with user authentication, post creation with rich text formatting, and content management features.',
      github: 'https://github.com/AnNanya0903/Blog_Platform',
      icon: '📝',
    },
    {
      title: 'Maya Therapy',
      description: 'A beautiful, responsive therapist website built with Next.js, featuring an elegant UI for Dr. Maya Reynolds therapy practice.',
      github: 'https://github.com/AnNanya0903/maya-therapy',
      icon: '❤️',
    },
    {
      title: 'Wumpus World AI',
      description: 'A Python-powered AI adventure where an agent navigates a perilous 4x4 grid, avoids deadly pits and the Wumpus, and uses logical reasoning to find the gold with visualization and step-by-step simulation.',
      github: 'https://github.com/AnNanya0903/wumpus_proj_py0002',
      icon: '🤖',
    },
  ];

  return (
    <section 
      id="projects" 
      className={`section projects animated-section ${isVisible ? 'scroll-visible' : 'scroll-hidden'}`}
      ref={ref}
    >
      <h2>{headingText}<span>Projects</span></h2>
      <p>Showcasing my technical skills and creative solutions</p>
      <div className="project-grid">
        {projects.map((project, index) => (
          <div key={index} className="project-card" style={{ animationDelay: `${index * 0.1}s` }}>
            <div className="project-icon">{project.icon}</div>
            <h3>{project.title}</h3>
            <p>{project.description}</p>
            <a href={project.github} target="_blank" rel="noopener noreferrer" className="project-link">
              <i className="fab fa-github"></i> View on GitHub <i className="fas fa-arrow-right"></i>
            </a>
          </div>
        ))}
      </div>
    </section>
  );
};

export default Projects;
