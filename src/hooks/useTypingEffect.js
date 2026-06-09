import { useState, useEffect } from 'react';

export const useTypingEffect = (texts, typingSpeed = 100, deletingSpeed = 50, pauseTime = 2000) => {
  const [index, setIndex] = useState(0);
  const [subIndex, setSubIndex] = useState(0);
  const [reverse, setReverse] = useState(false);
  const [blink, setBlink] = useState(true);

  useEffect(() => {
    if (index === texts.length) {
      setIndex(0);
      return;
    }

    if (reverse) {
      if (subIndex === 0) {
        setReverse(false);
        setIndex((prev) => (prev + 1) % texts.length);
      } else {
        const timeout = setTimeout(() => setSubIndex((prev) => prev - 1), deletingSpeed);
        return () => clearTimeout(timeout);
      }
    } else {
      if (subIndex === texts[index].length) {
        const timeout = setTimeout(() => setReverse(true), pauseTime);
        return () => clearTimeout(timeout);
      } else {
        const timeout = setTimeout(() => setSubIndex((prev) => prev + 1), typingSpeed);
        return () => clearTimeout(timeout);
      }
    }
  }, [texts, index, subIndex, reverse, typingSpeed, deletingSpeed, pauseTime]);

  useEffect(() => {
    const timeout = setTimeout(() => setBlink((prev) => !prev), 500);
    return () => clearTimeout(timeout);
  }, [blink]);

  return `${texts[index].substring(0, subIndex)}${blink ? '|' : ' '}`;
};