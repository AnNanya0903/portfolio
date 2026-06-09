import React, { useState } from 'react';
import { useScrollAnimation } from '../hooks/useScrollAnimation';

const Contact = () => {
  const [formData, setFormData] = useState({ name: '', email: '', message: '' });
  const [ref, isVisible] = useScrollAnimation(0.1);

  const handleChange = (e) => {
    const { name, value } = e.target;
    setFormData(prev => ({ ...prev, [name]: value }));
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    // For now, just log the data. Later, you can add a backend or email service integration
    alert(`Thank you, ${formData.name}! Your message has been received!`);
    setFormData({ name: '', email: '', message: '' });
  };

  return (
    <section 
      id="contact" 
      className={`section contact ${isVisible ? 'scroll-visible' : 'scroll-hidden'}`}
      ref={ref}
    >
      <h2>Get in <span>Touch</span></h2>
      <p className="contact-subtitle">Feel free to reach out for collaborations or just a friendly hello</p>
      <form className="contact-form" onSubmit={handleSubmit}>
        <div className="form-group">
          <label htmlFor="name">Name</label>
          <input
            type="text"
            id="name"
            name="name"
            value={formData.name}
            onChange={handleChange}
            required
          />
        </div>
        <div className="form-group">
          <label htmlFor="email">Email</label>
          <input
            type="email"
            id="email"
            name="email"
            value={formData.email}
            onChange={handleChange}
            required
          />
        </div>
        <div className="form-group">
          <label htmlFor="message">Message</label>
          <textarea
            id="message"
            name="message"
            rows="5"
            value={formData.message}
            onChange={handleChange}
            required
          ></textarea>
        </div>
        <button type="submit" className="btn">Send Message</button>
      </form>
      <div className="social-icons" style={{ marginTop: '2rem', display: 'flex', justifyContent: 'center', gap: '1.5rem', animation: 'fadeInUp 0.8s ease-out 0.9s forwards', opacity: 0 }}>
        <a href="https://github.com/AnNanya0903" target="_blank" rel="noopener noreferrer" className="social-link">
          <i className="fab fa-github"></i>
        </a>
        <a href="https://linkedin.com/in/ananyagowda" target="_blank" rel="noopener noreferrer" className="social-link">
          <i className="fab fa-linkedin"></i>
        </a>
        <a href="mailto:ananyagowda0903@gmail.com" className="social-link">
          <i className="fas fa-envelope"></i>
        </a>
      </div>
    </section>
  );
};

export default Contact;
