import React from 'react';
import { useScrollAnimation } from '../hooks/useScrollAnimation';

const Skills = () => {
  const [ref, isVisible] = useScrollAnimation(0.1);

  return (
    <section 
      id="skills" 
      className={`section skills ${isVisible ? 'scroll-visible' : 'scroll-hidden'}`}
      ref={ref}
    >
      <h2>My <span>Skills</span></h2>
      <p>I combine technical expertise with creative problem-solving</p>
      <div className="skills-container split-skills">
        <div className="skills-column">
          <h3 className="skills-subheading">Frontend</h3>
          <div className="skills-grid">
            <span className="skill-tag"><i className="fab fa-html5"></i>HTML5 & CSS3</span>
            <span className="skill-tag"><i className="fab fa-js"></i>JavaScript</span>
            <span className="skill-tag"><i className="fab fa-react"></i>React.js</span>
            <span className="skill-tag"><i className="fas fa-mobile-alt"></i>Responsive Design</span>
            <span className="skill-tag"><i className="fab fa-git-alt"></i>Git & GitHub</span>
            <span className="skill-tag"><i className="fas fa-pen-ruler"></i>UI/UX Design</span>
            <span className="skill-tag"><i className="fas fa-code"></i>DSA</span>
            <span className="skill-tag"><i className="fas fa-puzzle-piece"></i>OOPS</span>
            <span className="skill-tag"><i className="fas fa-bug"></i>Error Handling</span>
            <span className="skill-tag"><i className="fas fa-cubes"></i>Modular Design</span>
          </div>
        </div>
        <div className="skills-column">
          <h3 className="skills-subheading">Backend</h3>
          <div className="skills-grid">
            <span className="skill-tag"><i className="fab fa-node-js"></i>Node.js</span>
            <span className="skill-tag"><i className="fas fa-server"></i>Express.js</span>
            <span className="skill-tag"><i className="fas fa-database"></i>MongoDB</span>
            <span className="skill-tag"><i className="fab fa-python"></i>Python</span>
            <span className="skill-tag"><i className="fas fa-cubes"></i>Blockchain</span>
            <span className="skill-tag"><i className="fas fa-brain"></i>AI</span>
            <span className="skill-tag"><i className="fas fa-shield-alt"></i>Secure Coding</span>
          </div>
        </div>
        <div className="skills-column">
          <h3 className="skills-subheading">Soft Skills</h3>
          <div className="skills-grid soft-grid">
            <span className="skill-tag soft"><i className="fas fa-comments"></i>Communication</span>
            <span className="skill-tag soft"><i className="fas fa-users"></i>Teamwork</span>
            <span className="skill-tag soft"><i className="fas fa-puzzle-piece"></i>Problem Solving</span>
            <span className="skill-tag soft"><i className="fas fa-clock"></i>Time Management</span>
            <span className="skill-tag soft"><i className="fas fa-sync-alt"></i>Adaptability</span>
            <span className="skill-tag soft"><i className="fas fa-crown"></i>Leadership</span>
          </div>
        </div>
      </div>
    </section>
  );
};

export default Skills;
