import '@testing-library/jest-dom/vitest';

// jsdom does not implement scrollIntoView (used by the chat autoscroll).
if (typeof window !== 'undefined'
    && !window.HTMLElement.prototype.scrollIntoView) {
  window.HTMLElement.prototype.scrollIntoView = () => {};
}
