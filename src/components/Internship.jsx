import React from 'react';
import { useScrollAnimation } from '../hooks/useScrollAnimation';

const Internship = () => {
  const [ref, isVisible] = useScrollAnimation(0.1);
  
  const internships = [
    {
      title: 'AI Engineering Intern',
      company: 'Tech Innovation Lab',
      period: 'Jan 2026 – Present',
      skills: ['Python', 'Scikit-learn', 'NLP', 'Machine Learning'],
      description: [
        'Built and deployed supervised ML classification models using Python and Scikit-learn; applied feature engineering and cross-validation to improve prediction accuracy by ~15% over baseline',
        'Designed end-to-end NLP preprocessing pipelines (TF-IDF vectorization, tokenization, stopword removal) for text classification tasks, reducing manual data processing effort significantly',
        'Contributed across full ML lifecycle — data collection, EDA, feature selection, model training, hyperparameter tuning, and performance evaluation — following industry workflows'
      ],
      gradient: 'linear-gradient(135deg, #667eea 0%, #764ba2 50%, #f093fb 100%)',
      icon: 'fa-robot'
    },
    {
      title: 'Frontend Web Development Intern',
      company: 'Digital Solutions Corp.',
      period: 'Sep – Nov 2025',
      skills: ['HTML5', 'CSS3', 'JavaScript', 'Responsive Design', 'UI/UX'],
      description: [
        'Developed 5+ responsive, accessible web interfaces using HTML5, CSS3, and JavaScript ensuring full cross-browser compatibility and WCAG accessibility standards',
        'Implemented event-driven JavaScript and DOM manipulation to build interactive real-time UI components, improving user interaction flow',
        'Diagnosed and resolved 10+ frontend rendering bugs, improving page load consistency and delivering polished UX for client deliverables on schedule'
      ],
      gradient: 'linear-gradient(135deg, #f093fb 0%, #f5576c 50%, #ff9a9e 100%)',
      icon: 'fa-code'
    }
  ];

  return (
    <section id="internship" className={`section skills ${isVisible ? 'scroll-visible' : 'scroll-hidden'}`} ref={ref}>
      <h2>Internship <span>Experience</span></h2>
      <div className="project-grid">
        {internships.map((internship, index) => (
          <div key={index} className="project-card">
            <div className="project-icon" style={{ background: internship.gradient }}>
              <i className={`fas ${internship.icon}`}></i>
            </div>
            <div className="project-scene">
              <div className="scene-shape shape-1"></div>
              <div className="scene-shape shape-2"></div>
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
