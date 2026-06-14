import React from 'react';
import { useTypingEffect } from '../hooks/useTypingEffect';
import { useScrollAnimation } from '../hooks/useScrollAnimation';

const Internship = () => {
  const headingText = useTypingEffect(['Internship ']);
  const [ref, isVisible] = useScrollAnimation(0.1);
  
  const internships = [
    {
      title: 'AI Engineering Intern',
      company: 'Edutainer',
      period: 'Present',
      skills: ['Python', 'Machine Learning', 'NLP', 'Data Analysis'],
      description: [
        'Worked on developing and optimizing AI/ML models for real-world applications, focusing on delivering practical, scalable solutions that solve business problems',
        'Applied data preprocessing techniques such as handling missing values, feature scaling, and outlier detection to clean and prepare datasets for model training; improved data quality and model reliability',
        'Implemented model training and evaluation pipelines using Python and ML libraries; performed cross-validation, hyperparameter tuning, and performance metrics analysis to enhance model accuracy and generalization',
        'Utilized Natural Language Processing (NLP) concepts to build text-processing applications, demonstrating practical application of NLP techniques in solving real-world tasks'
      ],
      gradient: 'linear-gradient(135deg, #667eea 0%, #764ba2 50%, #f093fb 100%)',
      icon: 'fa-brain'
    },
    {
      title: 'Full Stack Development Intern',
      company: 'Nayoda',
      period: 'Mar 2026 – Apr 2026',
      skills: ['React.js', 'Node.js', 'JavaScript', 'REST APIs', 'Full Stack Development'],
      description: [
        'Completed an intensive full stack development internship, building and deploying end-to-end web applications using modern frontend and backend technologies',
        'Demonstrated strong proficiency in full stack architecture, RESTful API integration, and delivering production-ready features with good skills and professionalism'
      ],
      gradient: 'linear-gradient(135deg, #4facfe 0%, #00f2fe 100%)',
      icon: 'fa-code'
    },
    {
      title: 'Web Development Intern',
      company: 'SCOSS Services, Tumkur',
      period: 'Sep 2025 – Nov 2025',
      skills: ['HTML', 'CSS', 'JavaScript', 'UI/UX', 'Responsive Design'],
      description: [
        'Developed responsive web pages using HTML, CSS, and JavaScript; ensured layouts adapt seamlessly across desktop, tablet, and mobile devices for better user accessibility',
        'Performed frontend development and debugging tasks, including DOM manipulation to create interactive elements and real-time updates on web pages',
        'Improved UI/UX design by implementing intuitive navigation, clean visual hierarchies, and smooth animations, enhancing overall user satisfaction and engagement',
        'Implemented real-time case study solutions, applying theoretical frontend concepts to solve practical problems and deliver functional, user-friendly web interfaces'
      ],
      gradient: 'linear-gradient(135deg, #f093fb 0%, #f5576c 50%, #ff9a9e 100%)',
      icon: 'fa-laptop-code'
    }
  ];

  return (
    <section id="internship" className={`section skills animated-section ${isVisible ? 'scroll-visible' : 'scroll-hidden'}`} ref={ref}>
      <h2>{headingText}<span>Experience</span></h2>
      <div className="project-grid">
        {internships.map((internship, index) => (
          <div key={index} className="project-card" style={{ animationDelay: `${index * 0.1}s` }}>
            <div className="project-icon" style={{ background: internship.gradient }}>
              <i className={`fas ${internship.icon}`}></i>
            </div>
            <h3>{internship.title}</h3>
            <p className="project-meta">{internship.company} • {internship.period}</p>
            <div className="internship-skills">
              {internship.skills.map((skill, i) => (
                <span key={i} className="internship-skill">
                  {skill}
                </span>
              ))}
            </div>
            <ul className="internship-description">
              {internship.description.map((item, i) => (
                <li key={i}>{item}</li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </section>
  );
};

export default Internship;
