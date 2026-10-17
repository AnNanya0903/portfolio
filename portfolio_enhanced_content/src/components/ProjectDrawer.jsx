import React, { useEffect, useRef } from 'react';
import './ProjectDrawer.css';

const ProjectDrawer = ({ project, onClose }) => {
  const closeRef = useRef(null);

  useEffect(() => {
    if (!project) return undefined;

    const onKeyDown = (event) => {
      if (event.key === 'Escape') onClose();
    };

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    window.addEventListener('keydown', onKeyDown);
    closeRef.current?.focus();

    return () => {
      window.removeEventListener('keydown', onKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [project, onClose]);

  if (!project) return null;

  const links = project.links || [
    { label: project.demoLabel, href: project.demo, icon: project.demoIcon, primary: true },
    { label: 'GitHub', href: project.github, icon: 'fab fa-github' },
  ];

  return (
    <div className="drawer" role="dialog" aria-modal="true" aria-label={`${project.title} details`}>
      <div className="drawer__backdrop" onClick={onClose} />

      <aside className="drawer__panel">
        <button
          ref={closeRef}
          type="button"
          className="drawer__close"
          onClick={onClose}
          aria-label="Close project details"
        >
          <i className="fas fa-times"></i>
        </button>

        {project.image ? (
          <img className="drawer__image" src={project.image} alt="" />
        ) : (
          <div className="drawer__placeholder" aria-hidden="true">{project.icon}</div>
        )}

        <div className="drawer__body">
          <div className="drawer__meta">
            <span className="drawer__category">{project.category}</span>
            {project.featured && (
              <span className="drawer__featured"><i className="fas fa-star"></i> Featured</span>
            )}
          </div>

          <h3 className="drawer__title">{project.title}</h3>
          <p className="drawer__description">{project.description}</p>

          {project.highlights && project.highlights.length > 0 && (
            <>
              <h4 className="drawer__subheading">Key Features</h4>
              <ul className="drawer__highlights">
                {project.highlights.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </>
          )}

          <h4 className="drawer__subheading">Tech Stack</h4>
          <div className="drawer__tech">
            {project.tech.map((tech) => (
              <span key={tech} className="drawer__chip">{tech}</span>
            ))}
          </div>

          <h4 className="drawer__subheading">Links</h4>
          <div className="drawer__links">
            {links.map((link) => (
              <a
                key={link.label}
                href={link.href}
                target="_blank"
                rel="noopener noreferrer"
                className={`project-link ${link.primary ? 'drawer__link-primary' : ''}`}
              >
                <i className={link.icon}></i> {link.label} <i className="fas fa-arrow-right"></i>
              </a>
            ))}
          </div>
        </div>
      </aside>
    </div>
  );
};

export default ProjectDrawer;
