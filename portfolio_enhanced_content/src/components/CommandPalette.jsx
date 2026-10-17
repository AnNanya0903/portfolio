import React, { useEffect, useMemo, useRef, useState } from 'react';
import { SECTIONS, PROJECTS, SOCIAL_LINKS } from '../data/site';
import './CommandPalette.css';

const CommandPalette = () => {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef(null);
  const activeRef = useRef(null);

  const items = useMemo(() => {
    const sectionItems = SECTIONS.map((section) => ({
      id: `section-${section.id}`,
      group: 'Sections',
      label: section.label,
      icon: 'fas fa-hashtag',
      keywords: `${section.label} ${section.id}`,
      run: () => {
        document.getElementById(section.id)?.scrollIntoView({ behavior: 'smooth' });
      },
    }));

    const projectItems = PROJECTS.map((project) => ({
      id: `project-${project.slug}`,
      group: 'Projects',
      label: project.title,
      hint: project.category,
      icon: 'fas fa-cube',
      keywords: `${project.title} ${project.category} ${project.tech.join(' ')}`,
      run: () => {
        document.getElementById('projects')?.scrollIntoView({ behavior: 'smooth' });
        window.dispatchEvent(new CustomEvent('open-project', { detail: project.slug }));
      },
    }));

    const linkItems = SOCIAL_LINKS.map((link) => ({
      id: `link-${link.label}`,
      group: 'Links',
      label: link.label,
      hint: link.href.replace(/^mailto:/, ''),
      icon: link.icon,
      keywords: `${link.label} contact social`,
      run: () => {
        if (link.href.startsWith('mailto:')) window.location.href = link.href;
        else window.open(link.href, '_blank', 'noopener,noreferrer');
      },
    }));

    return [...sectionItems, ...projectItems, ...linkItems];
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((item) => item.keywords.toLowerCase().includes(q));
  }, [items, query]);

  // Open / close shortcuts
  useEffect(() => {
    const onKeyDown = (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setOpen((prev) => !prev);
      }
    };
    const onToggle = () => setOpen((prev) => !prev);
    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('toggle-command-palette', onToggle);
    return () => {
      window.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('toggle-command-palette', onToggle);
    };
  }, []);

  // Reset state and lock scrolling while the palette is open
  useEffect(() => {
    if (!open) return undefined;
    setQuery('');
    setActiveIndex(0);

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    inputRef.current?.focus();

    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [open]);

  // Keep the highlighted row in view
  useEffect(() => {
    if (open) activeRef.current?.scrollIntoView({ block: 'nearest' });
  }, [activeIndex, open, query]);

  if (!open) return null;

  const runItem = (item) => {
    setOpen(false);
    item.run();
  };

  const onInputKeyDown = (event) => {
    if (event.key === 'Escape') {
      setOpen(false);
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      setActiveIndex((prev) => (filtered.length ? (prev + 1) % filtered.length : 0));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setActiveIndex((prev) => (filtered.length ? (prev - 1 + filtered.length) % filtered.length : 0));
    } else if (event.key === 'Enter' && filtered[activeIndex]) {
      event.preventDefault();
      runItem(filtered[activeIndex]);
    }
  };

  return (
    <div className="cmdk" role="dialog" aria-modal="true" aria-label="Command palette">
      <div className="cmdk__backdrop" onClick={() => setOpen(false)} />

      <div className="cmdk__panel">
        <div className="cmdk__search">
          <i className="fas fa-magnifying-glass" aria-hidden="true"></i>
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setActiveIndex(0);
            }}
            onKeyDown={onInputKeyDown}
            placeholder="Jump to a section, project or link…"
            aria-label="Search sections, projects and links"
          />
          <kbd>Esc</kbd>
        </div>

        {filtered.length > 0 ? (
          <ul className="cmdk__list">
            {filtered.map((item, index) => {
              const showGroup = index === 0 || filtered[index - 1].group !== item.group;
              return (
                <React.Fragment key={item.id}>
                  {showGroup && (
                    <li className="cmdk__group" aria-hidden="true">{item.group}</li>
                  )}
                  <li>
                    <button
                      type="button"
                      ref={index === activeIndex ? activeRef : null}
                      className={`cmdk__item ${index === activeIndex ? 'is-active' : ''}`}
                      onMouseEnter={() => setActiveIndex(index)}
                      onClick={() => runItem(item)}
                    >
                      <i className={item.icon} aria-hidden="true"></i>
                      <span className="cmdk__label">{item.label}</span>
                      {item.hint && <span className="cmdk__hint">{item.hint}</span>}
                    </button>
                  </li>
                </React.Fragment>
              );
            })}
          </ul>
        ) : (
          <p className="cmdk__empty">No matches for “{query}”</p>
        )}

        <div className="cmdk__footer">
          <span><kbd>↑</kbd><kbd>↓</kbd> navigate</span>
          <span><kbd>↵</kbd> select</span>
          <span><kbd>Esc</kbd> close</span>
        </div>
      </div>
    </div>
  );
};

export default CommandPalette;
