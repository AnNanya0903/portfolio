import React from 'react';

const TechMarquee = () => {
  const items = [
    { icon: 'fab fa-react', name: 'React' },
    { icon: 'fab fa-node-js', name: 'Node.js' },
    { icon: 'fab fa-python', name: 'Python' },
    { icon: 'fab fa-js', name: 'JavaScript' },
    { icon: 'fas fa-database', name: 'MongoDB' },
    { icon: 'fas fa-fire', name: 'FastAPI' },
    { icon: 'fas fa-brain', name: 'LangChain' },
    { icon: 'fas fa-robot', name: 'LLM Agents' },
    { icon: 'fab fa-docker', name: 'Docker' },
    { icon: 'fab fa-aws', name: 'AWS' },
    { icon: 'fab fa-git-alt', name: 'Git' },
    { icon: 'fas fa-cloud', name: 'Vercel' },
  ];

  return (
    <div className="tech-marquee" aria-label="Technologies I work with">
      <div className="tech-marquee__track">
        {[0, 1].map((group) => (
          <ul className="tech-marquee__group" key={group} aria-hidden={group === 1}>
            {items.map((item) => (
              <li className="tech-marquee__item" key={item.name}>
                <i className={item.icon}></i>
                <span>{item.name}</span>
              </li>
            ))}
          </ul>
        ))}
      </div>
    </div>
  );
};

export default TechMarquee;
