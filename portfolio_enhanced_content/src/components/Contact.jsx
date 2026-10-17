import React, { useState } from 'react';
import { useTypingEffect } from '../hooks/useTypingEffect';
import { useScrollAnimation } from '../hooks/useScrollAnimation';

const EMAIL = 'ananyagowda0903@gmail.com';

// ── Formspree ─────────────────────────────────────────────────────────────
// TODO: replace the placeholder below with your real Formspree form ID
// (create one at https://formspree.io → New Form → copy the 8-character ID).
// Until it is replaced, the form falls back to opening a pre-filled mail draft.
const FORMSPREE_ID = 'REPLACE_WITH_FORMSPREE_ID';
const HAS_FORMSPREE = FORMSPREE_ID !== 'REPLACE_WITH_FORMSPREE_ID';
const FORMSPREE_ENDPOINT = `https://formspree.io/f/${FORMSPREE_ID}`;

const Contact = () => {
  const headingText = useTypingEffect(["Let's "]);
  const [ref, isVisible] = useScrollAnimation(0.1);
  const [form, setForm] = useState({ name: '', email: '', message: '' });
  const [errors, setErrors] = useState({});
  const [status, setStatus] = useState('idle');

  const handleChange = (e) => {
    const { name, value } = e.target;
    setForm((prev) => ({ ...prev, [name]: value }));
    setErrors((prev) => ({ ...prev, [name]: '' }));
  };

  const validate = () => {
    const next = {};
    if (!form.name.trim()) next.name = 'Please enter your name';
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(form.email.trim())) {
      next.email = 'Enter a valid email address';
    }
    if (form.message.trim().length < 10) {
      next.message = 'Please write at least 10 characters';
    }
    return next;
  };

  const reset = () => {
    setForm({ name: '', email: '', message: '' });
    setStatus('sent');
    setTimeout(() => setStatus('idle'), 6000);
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    const nextErrors = validate();
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) return;

    if (!HAS_FORMSPREE) {
      const subject = encodeURIComponent(`Portfolio enquiry from ${form.name}`);
      const body = encodeURIComponent(`${form.message}\n\n— ${form.name} (${form.email})`);
      window.location.href = `mailto:${EMAIL}?subject=${subject}&body=${body}`;
      reset();
      return;
    }

    setStatus('sending');
    try {
      const response = await fetch(FORMSPREE_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({
          name: form.name,
          email: form.email,
          message: form.message,
        }),
      });
      if (!response.ok) throw new Error('Formspree request failed');
      reset();
    } catch {
      setStatus('error');
      setTimeout(() => setStatus('idle'), 6000);
    }
  };

  return (
    <section 
      id="contact" 
      className={`section contact animated-section ${isVisible ? 'scroll-visible' : 'scroll-hidden'}`}
      ref={ref}
    >
      <h2>{headingText}<span>Connect</span></h2>
      <p style={{ marginBottom: '2rem' }}>Open to full-stack and AI engineering roles, internships, and freelance projects. Have an opportunity or an idea? My inbox is open.</p>
      
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

      <form className="contact-form" onSubmit={handleSubmit} noValidate>
        <div className="form-group float-field">
          <input
            id="name"
            name="name"
            type="text"
            placeholder=" "
            value={form.name}
            onChange={handleChange}
            className={errors.name ? 'has-error' : ''}
          />
          <label htmlFor="name">Name</label>
          {errors.name && <span className="form-error">{errors.name}</span>}
        </div>

        <div className="form-group float-field">
          <input
            id="email"
            name="email"
            type="email"
            placeholder=" "
            value={form.email}
            onChange={handleChange}
            className={errors.email ? 'has-error' : ''}
          />
          <label htmlFor="email">Email</label>
          {errors.email && <span className="form-error">{errors.email}</span>}
        </div>

        <div className="form-group float-field">
          <textarea
            id="message"
            name="message"
            rows="5"
            placeholder=" "
            value={form.message}
            onChange={handleChange}
            className={errors.message ? 'has-error' : ''}
          ></textarea>
          <label htmlFor="message">Message</label>
          {errors.message && <span className="form-error">{errors.message}</span>}
        </div>

        <button type="submit" className="btn" disabled={status === 'sending'}>
          <i className={`fas ${status === 'sending' ? 'fa-spinner fa-spin' : 'fa-paper-plane'}`} style={{ marginRight: '0.5rem' }}></i>
          {status === 'sending' ? 'Sending…' : 'Send Message'}
        </button>

        {status === 'sent' && (
          <div className="form-success" role="status">
            <svg className="success-check" viewBox="0 0 52 52" aria-hidden="true">
              <circle className="success-check__circle" cx="26" cy="26" r="24" />
              <path className="success-check__tick" d="M14 27l8 8 16-16" />
            </svg>
            {HAS_FORMSPREE
              ? "Thanks! Your message is on its way — I'll get back to you soon."
              : "Thanks! Your mail app should open with the message ready to send."}
          </div>
        )}

        {status === 'error' && (
          <div className="form-error-block" role="alert">
            <i className="fas fa-triangle-exclamation"></i> Something went wrong. Please email me directly at {EMAIL}.
          </div>
        )}
      </form>
    </section>
  );
};

export default Contact;
