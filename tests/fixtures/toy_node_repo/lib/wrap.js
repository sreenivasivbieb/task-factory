'use strict';
const { words } = require('./stats');

/** Greedy word wrap to lines of at most width characters. */
function wrap(text, width = 80) {
  if (!Number.isInteger(width) || width < 1) throw new RangeError('width must be a positive integer');
  const lines = [];
  let line = '';
  for (const w of words(text)) {
    if (!line) line = w;
    else if (line.length + 1 + w.length <= width) line += ' ' + w;
    else { lines.push(line); line = w; }
  }
  if (line) lines.push(line);
  return lines;
}

/** Shorten text to at most max characters, ending with an ellipsis when cut. */
function truncate(text, max) {
  if (text.length <= max) return text;
  return text.slice(0, Math.max(0, max - 1)) + '…';
}

module.exports = { wrap, truncate };
