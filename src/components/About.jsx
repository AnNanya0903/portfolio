import React from 'react';
import { useTypingEffect } from '../hooks/useTypingEffect';
import { useScrollAnimation } from '../hooks/useScrollAnimation';

const About = () => {
  const headingText = useTypingEffect(['About ']);
  const [ref, isVisible] = useScrollAnimation(0.1);

  return (
    <section 
      id="about" 
      className={`section about animated-section ${isVisible ? 'scroll-visible' : 'scroll-hidden'}`}
      ref={ref}
    >
      <h2>{headingText}<span>Me</span></h2>
      <div className="about-container">
        <div className="about-image">
          <img src="/profile-pic.jpg" alt="Profile" className="about-illustration" />
        </div>
        <div className="about-content">
          <p className="about-text">I am a passionate developer focused on creating efficient, practical, and user-centered software solutions. With hands-on experience in programming, databases, and automation tools, I enjoy transforming ideas into real applications.</p>
          <p className="about-text">I am especially interested in artificial intelligence, modern web technologies, and building scalable systems, and I continuously work to improve my skills and contribute to meaningful digital solutions.</p>
          <div className="about-stats">
            <div className="stat-item">
              <span className="stat-number">5+</span>
              <span className="stat-label">Projects Completed</span>
            </div>
            <div className="stat-item">
              <span className="stat-number">0</span>
              <span className="stat-label">Years Experience</span>
            </div>
            <div className="stat-item">
              <span className="stat-number">10+</span>
              <span className="stat-label">Technologies</span>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
};

export default About;
