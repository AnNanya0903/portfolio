import React from 'react';
import { useTypingEffect } from '../hooks/useTypingEffect';
import { useScrollAnimation } from '../hooks/useScrollAnimation';

const Skills = () => {
  const headingText = useTypingEffect(['Technical ']);
  const [ref, isVisible] = useScrollAnimation(0.1);

  const skillCategories = [
    {
      title: 'Languages',
      icon: 'fa-code',
      gradient: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
      skills: ['Python', 'JavaScript', 'TypeScript', 'C']
    },
    {
      title: 'Frontend',
      icon: 'fa-laptop-code',
      gradient: 'linear-gradient(135deg, #f093fb 0%, #f5576c 100%)',
      skills: ['React.js', 'HTML5', 'CSS3', 'Tailwind', 'DOM']
    },
    {
      title: 'Backend & APIs',
      icon: 'fa-server',
      gradient: 'linear-gradient(135deg, #4facfe 0%, #00f2fe 100%)',
      skills: ['Node.js', 'REST APIs', 'Supabase']
    },
    {
      title: 'AI / ML',
      icon: 'fa-brain',
      gradient: 'linear-gradient(135deg, #f093fb 0%, #f5576c 50%, #ff9a9e 100%)',
      skills: ['Scikit-learn', 'TensorFlow', 'NLP', 'NumPy', 'Pandas', 'LLMs', 'RAG']
    },
    {
      title: 'Blockchain',
      icon: 'fa-cubes',
      gradient: 'linear-gradient(135deg, #43e97b 0%, #38f9d7 100%)',
      skills: ['Solidity', 'Ethereum', 'Hardhat', 'MetaMask']
    },
    {
      title: 'Databases',
      icon: 'fa-database',
      gradient: 'linear-gradient(135deg, #a18cd1 0%, #fbc2eb 100%)',
      skills: ['MongoDB', 'SQL', 'PostgreSQL']
    },
    {
      title: 'Tools',
      icon: 'fa-tools',
      gradient: 'linear-gradient(135deg, #ff9a9e 0%, #fecfef 100%)',
      skills: ['Git', 'GitHub', 'VS Code']
    },
    {
      title: 'Core CS',
      icon: 'fa-graduation-cap',
      gradient: 'linear-gradient(135deg, #f093fb 0%, #f5576c 100%)',
      skills: ['DSA', 'OOP', 'Agile']
    }
  ];

  return (
    <section 
      id="skills" 
      className={`section skills animated-section ${isVisible ? 'scroll-visible' : 'scroll-hidden'}`}
      ref={ref}
    >
      <h2>{headingText}<span>Proficiency</span></h2>
      <p>A curated showcase of my skills and expertise</p>
      <div 
        className="skills-panel"
        style={{ 
          background: 'var(--card-bg)',
          borderRadius: '20px',
          padding: '1.25rem',
          boxShadow: 'var(--card-shadow)',
          border: '1px solid rgba(255,255,255,0.15)',
          maxWidth: '1000px',
          margin: '0 auto',
          marginTop: '1.25rem'
        }}
      >
        {skillCategories.map((category, index) => (
          <div 
            key={index}
            className="skill-category"
            style={{ 
              marginBottom: 0,
              paddingBottom: 0,
              borderBottom: 'none',
              animationDelay: `${index * 0.06}s`
            }}
          >
            <div             style={{ 
              display: 'flex', 
              alignItems: 'center', 
              gap: '0.75rem', 
              marginBottom: '0.5rem'
            }}>
              <div style={{
                width: '34px',
                height: '34px',
                borderRadius: '10px',
                background: category.gradient,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: 'white',
                fontSize: '0.95rem',
                boxShadow: '0 6px 20px rgba(99,102,241,0.3)'
              }}>
                <i className={`fas ${category.icon}`}></i>
              </div>
              <h3 style={{ 
                color: 'var(--text-color)', 
                margin: 0, 
                fontSize: '1rem',
                fontWeight: 700
              }}>{category.title}</h3>
            </div>
            <div style={{ 
              display: 'flex', 
              flexWrap: 'wrap', 
              gap: '0.35rem'
            }}>
              {category.skills.map((skill, skillIndex) => (
                <span 
                  key={skillIndex}
                  className="skill-chip"
                  style={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: '0.4rem',
                    padding: '0.38rem 0.75rem',
                    borderRadius: '50px',
                    fontWeight: 600,
                    fontSize: '0.78rem',
                    color: 'var(--text-color)',
                    transition: 'all 0.3s cubic-bezier(0.4,0,0.2,1)',
                    cursor: 'default',
                    background: 'linear-gradient(135deg, rgba(99,102,241,0.1) 0%, rgba(139,92,246,0.1) 100%)',
                    border: '1px solid rgba(99,102,241,0.2)',
                    animationDelay: `${skillIndex * 0.045}s`
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.transform = 'translateY(-3px) scale(1.05)';
                    e.currentTarget.style.color = 'white';
                    e.currentTarget.style.background = category.gradient;
                    e.currentTarget.style.borderColor = 'transparent';
                    e.currentTarget.style.boxShadow = '0 10px 30px rgba(99,102,241,0.4)';
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.transform = 'translateY(0) scale(1)';
                    e.currentTarget.style.color = 'var(--text-color)';
                    e.currentTarget.style.background = 'linear-gradient(135deg, rgba(99,102,241,0.1) 0%, rgba(139,92,246,0.1) 100%)';
                    e.currentTarget.style.borderColor = 'rgba(99,102,241,0.2)';
                    e.currentTarget.style.boxShadow = 'none';
                  }}
                >
                  {skill}
                </span>
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
};

export default Skills;
