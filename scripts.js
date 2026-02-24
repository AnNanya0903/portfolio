// DARK/LIGHT MODE TOGGLE
const toggleBtn = document.getElementById('theme-toggle');
const body = document.body;
const toggleIcon = toggleBtn.querySelector('i');

// Load saved mode
if (localStorage.getItem('theme') === 'dark') {
  body.classList.add('dark');
  toggleIcon.classList.remove('fa-moon');
  toggleIcon.classList.add('fa-sun');
}

// Toggle dark/light mode
toggleBtn.addEventListener('click', () => {
  body.classList.toggle('dark');
  const isDark = body.classList.contains('dark');
  
  if (isDark) {
    toggleIcon.classList.remove('fa-moon');
    toggleIcon.classList.add('fa-sun');
  } else {
    toggleIcon.classList.remove('fa-sun');
    toggleIcon.classList.add('fa-moon');
  }
  
  localStorage.setItem('theme', isDark ? 'dark' : 'light');
});

// Smooth scroll for navigation
document.querySelectorAll('a[href^="#"]').forEach(anchor => {
  anchor.addEventListener('click', function (e) {
    e.preventDefault();
    const target = document.querySelector(this.getAttribute('href'));
    if (target) {
      target.scrollIntoView({
        behavior: 'smooth'
      });
    }
  });
});
