import React from 'react';
import { useTypingEffect } from '../hooks/useTypingEffect';
import { useScrollAnimation } from '../hooks/useScrollAnimation';

const Contact = () => {
  const headingText = useTypingEffect(['Establish ']);
  const [ref, isVisible] = useScrollAnimation(0.1);

  return (
    <section 
      id="contact" 
      className={`section contact animated-section ${isVisible ? 'scroll-visible' : 'scroll-hidden'}`}
      ref={ref}
    >
      <h2>{headingText}<span>Connection</span></h2>
      <p style={{ marginBottom: '2rem' }}>Ready to collaborate or just say hello!</p>
      
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '1rem', justifyContent: 'center', maxWidth: '800px', margin: '0 auto' }}>
        <a 
          href="mailto:ananyagowda0903@gmail.com" 
          className="contact-card"
          style={{ 
            display: 'flex', 
            alignItems: 'center', 
            gap: '0.5rem', 
            textDecoration: 'none', 
            color: 'var(--text-color)', 
            padding: '0.8rem 1.5rem', 
            border: '2px solid var(--accent-color)', 
            borderRadius: '50px', 
            transition: 'all 0.3s',
            boxShadow: '0 10px 30px rgba(99,102,241,0.2)'
          }} 
          onMouseEnter={(e) => { 
            e.target.style.background = 'linear-gradient(135deg, var(--accent-color), #8b5cf6)'; 
            e.target.style.color = 'white'; 
          }} 
          onMouseLeave={(e) => { 
            e.target.style.background = 'transparent'; 
            e.target.style.color = 'var(--text-color)'; 
          }}
        >
          <i className="fas fa-envelope"></i> ananyagowda0903@gmail.com
        </a>
        
        <a 
          href="https://linkedin.com/in/ananyagowda" 
          target="_blank" 
          rel="noopener noreferrer"
          className="contact-card"
          style={{ 
            display: 'flex', 
            alignItems: 'center', 
            gap: '0.5rem', 
            textDecoration: 'none', 
            color: 'var(--text-color)', 
            padding: '0.8rem 1.5rem', 
            border: '2px solid var(--accent-color)', 
            borderRadius: '50px', 
            transition: 'all 0.3s',
            boxShadow: '0 10px 30px rgba(99,102,241,0.2)'
          }} 
          onMouseEnter={(e) => { 
            e.target.style.background = 'linear-gradient(135deg, var(--accent-color), #8b5cf6)'; 
            e.target.style.color = 'white'; 
          }} 
          onMouseLeave={(e) => { 
            e.target.style.background = 'transparent'; 
            e.target.style.color = 'var(--text-color)'; 
          }}
        >
          <i className="fab fa-linkedin"></i> LinkedIn Profile
        </a>
        
        <a 
          href="https://github.com/AnNanya0903" 
          target="_blank" 
          rel="noopener noreferrer"
          className="contact-card"
          style={{ 
            display: 'flex', 
            alignItems: 'center', 
            gap: '0.5rem', 
            textDecoration: 'none', 
            color: 'var(--text-color)', 
            padding: '0.8rem 1.5rem', 
            border: '2px solid var(--accent-color)', 
            borderRadius: '50px', 
            transition: 'all 0.3s',
            boxShadow: '0 10px 30px rgba(99,102,241,0.2)'
          }} 
          onMouseEnter={(e) => { 
            e.target.style.background = 'linear-gradient(135deg, var(--accent-color), #8b5cf6)'; 
            e.target.style.color = 'white'; 
          }} 
          onMouseLeave={(e) => { 
            e.target.style.background = 'transparent'; 
            e.target.style.color = 'var(--text-color)'; 
          }}
        >
          <i className="fab fa-github"></i> GitHub Profile
        </a>
        
        <div 
          className="contact-card"
          style={{ 
            display: 'flex', 
            alignItems: 'center', 
            gap: '0.5rem', 
            color: 'var(--text-color)', 
            padding: '0.8rem 1.5rem'
          }}
        >
          <i className="fas fa-map-marker-alt"></i> Tumkur, Karnataka, India
        </div>
      </div>
    </section>
  );
};

export default Contact;
