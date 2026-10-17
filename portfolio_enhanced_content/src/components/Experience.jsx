import React from 'react';
import { useTypingEffect } from '../hooks/useTypingEffect';
import { useScrollAnimation } from '../hooks/useScrollAnimation';

const Experience = () => {
  const headingText = useTypingEffect(['Professional ']);
  const [ref, isVisible] = useScrollAnimation(0.1);

  const internships = [
    {
      title: 'Full Stack Development Intern',
      company: 'Nayoda',
      period: 'March 2026 – April 2026',
      project: 'SkillSphere — Intelligent Hyperlocal Freelance Ecosystem',
      tech: ['MongoDB', 'Express.js', 'React.js', 'Node.js (MERN)'],
      overview: 'Worked on developing a full-stack freelance marketplace connecting clients and freelancers, with features for gig management, proposals, messaging, payments, and administration.',
      responsibilities: [
        'Developed a multi-role authentication system with JWT, role-based access control (RBAC), Google OAuth, email verification, password reset, and two-factor authentication.',
        'Built 9 backend REST APIs for marketplace operations.',
        'Created 9 frontend components using React.',
        'Worked on real-time chat using Socket.IO, reviews, notifications, payment interfaces, and the admin dashboard.'
      ],
      achievements: [
        'Delivered an end-to-end MERN-stack platform during a two-month internship.',
        'Implemented milestone payments and a reputation-scoring system.',
        'Developed secure access for clients, freelancers, and administrators.'
      ],
      skills: [
        'MERN Stack', 'REST API development', 'React.js', 'Authentication',
        'Database integration', 'Socket.IO', 'Full-stack architecture', 'Application security'
      ],
      gradient: 'linear-gradient(135deg, #11998e 0%, #38ef7d 100%)',
      icon: 'fa-code'
    },
    {
      title: 'Full Stack Web Development Intern',
      company: 'SCOSS Services',
      period: 'September 2025 – November 2025',
      location: 'Tumkur',
      tech: ['HTML5', 'CSS3', 'JavaScript', 'REST APIs'],
      overview: 'Worked on client-facing web development, focusing on responsive interfaces, browser compatibility, and frontend-to-backend API integration.',
      responsibilities: [
        'Built responsive web interfaces using HTML5, CSS3, and JavaScript.',
        'Developed mobile-friendly layouts for different screen sizes.',
        'Ensured cross-browser compatibility.',
        'Integrated REST APIs to exchange dynamic data between frontend interfaces and backend servers.'
      ],
      achievements: [
        'Developed responsive, user-friendly web interfaces.',
        'Connected frontend applications with backend services through REST APIs.',
        'Gained practical experience in web application development.'
      ],
      skills: [
        'HTML5', 'CSS3', 'JavaScript', 'Responsive web design',
        'Cross-browser testing', 'REST API integration', 'Frontend development'
      ],
      gradient: 'linear-gradient(135deg, #4facfe 0%, #00f2fe 100%)',
      icon: 'fa-laptop-code'
    },
    {
      title: 'Artificial Intelligence Intern',
      company: 'Edutainer',
      period: 'February 2026 – May 2026',
      project: 'CogniFlow — Enterprise AI Intelligence Suite',
      tech: ['Python', 'Scikit-learn', 'NLP', 'OpenAI API', 'Groq', 'RAG'],
      overview: 'Worked on an AI-powered enterprise intelligence system designed to retrieve relevant information from documents and generate context-aware responses, alongside machine-learning classification models.',
      responsibilities: [
        'Designed a Retrieval-Augmented Generation (RAG) pipeline for document ingestion, vector embeddings, and semantic retrieval.',
        'Integrated OpenAI and Groq as interchangeable language-model providers within a Python service layer.',
        'Developed 3 supervised machine-learning classification models using Scikit-learn.',
        'Applied feature engineering and cross-validation to improve model performance.'
      ],
      achievements: [
        'Reported a 15% accuracy improvement over the baseline for the machine-learning models.',
        'Built a document-based AI question-answering pipeline.',
        'Integrated two LLM providers into one Python service layer.'
      ],
      skills: [
        'Python', 'Machine learning', 'Scikit-learn', 'Natural language processing',
        'RAG', 'Vector embeddings', 'Semantic search', 'LLM integration', 'Model evaluation'
      ],
      gradient: 'linear-gradient(135deg, #f093fb 0%, #f5576c 50%, #ff9a9e 100%)',
      icon: 'fa-brain'
    }
  ];

  const education = [
    {
      degree: 'MCA — Full Stack Development',
      institution: 'Bangalore Institute of Technology, VTU University',
      period: '2024 – 2026',
      score: 'CGPA: 9.4',
      note: 'Specialisation: Full Stack Development',
      gradient: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
      icon: 'fa-graduation-cap'
    },
    {
      degree: 'Bachelor of Computer Applications (BCA)',
      institution: 'Seshadripuram College, Tumkur University, Karnataka',
      period: 'Graduated Aug 2024',
      score: 'Graduated with 90%',
      note: 'Foundation in programming, databases, and software development',
      gradient: 'linear-gradient(135deg, #4facfe 0%, #00f2fe 100%)',
      icon: 'fa-user-graduate'
    }
  ];

  return (
    <section
      id="experience"
      className={`section experience animated-section ${isVisible ? 'scroll-visible' : 'scroll-hidden'}`}
      ref={ref}
    >
      <h2>{headingText}<span>Experience</span></h2>
      <p>Hands-on experience across AI engineering and full-stack development, backed by a strong academic foundation</p>

      <div className="timeline">
        {internships.map((internship, index) => (
          <article
            key={internship.company}
            className="timeline-item"
            style={{ animationDelay: `${index * 0.12}s` }}
          >
            <span className="timeline-node" aria-hidden="true">
              <i className={`fas ${internship.icon}`}></i>
            </span>

            <div className="timeline-card project-card">
              <span className="timeline-tag timeline-tag--work">Internship</span>
              <h3>{internship.title}</h3>
              <p className="project-meta">
                {internship.company} • {internship.period}{internship.location ? ` • ${internship.location}` : ''}
              </p>

              {internship.project && (
                <p className="exp-project">
                  <i className="fas fa-diagram-project"></i> {internship.project}
                </p>
              )}

              <div className="exp-skills">
                {internship.tech.map((tech, i) => (
                  <span key={i} className="exp-skill">{tech}</span>
                ))}
              </div>

              <h4 className="exp-subheading">Overview</h4>
              <p className="exp-overview">{internship.overview}</p>

              <h4 className="exp-subheading">Key Responsibilities</h4>
              <ul className="exp-list">
                {internship.responsibilities.map((item, i) => (
                  <li key={i}>{item}</li>
                ))}
              </ul>

              <h4 className="exp-subheading">Key Achievements</h4>
              <ul className="exp-list">
                {internship.achievements.map((item, i) => (
                  <li key={i}>{item}</li>
                ))}
              </ul>

              <h4 className="exp-subheading">Skills Gained</h4>
              <div className="exp-skills">
                {internship.skills.map((skill, i) => (
                  <span key={i} className="exp-skill">{skill}</span>
                ))}
              </div>
            </div>
          </article>
        ))}
      </div>

      <h3 className="timeline-group-title">Education</h3>

      <div className="timeline timeline--education">
        {education.map((edu, index) => (
          <article
            key={edu.degree}
            className="timeline-item"
            style={{ animationDelay: `${index * 0.12}s` }}
          >
            <span className="timeline-node" aria-hidden="true">
              <i className={`fas ${edu.icon}`}></i>
            </span>

            <div className="timeline-card project-card">
              <span className="timeline-tag timeline-tag--education">Education</span>
              <h3>{edu.degree}</h3>
              <p className="project-meta">{edu.institution} • {edu.period}</p>
              <p className="exp-score">{edu.score}</p>
              <p className="exp-overview">{edu.note}</p>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
};

export default Experience;
