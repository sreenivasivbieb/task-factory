'use strict';

function words(text) {
  return text.split(/\s+/).filter(Boolean);
}

/** Number of words in text. */
function wordCount(text) {
  return words(text).length;
}

/** Map of lower-cased word -> count, ignoring punctuation at word edges. */
function wordFrequencies(text) {
  const freq = new Map();
  for (const raw of words(text)) {
    const w = raw.toLowerCase().replace(/^[^a-z0-9]+|[^a-z0-9]+$/g, '');
    if (!w) continue;
    freq.set(w, (freq.get(w) || 0) + 1);
  }
  return freq;
}

/** The n most frequent words as [word, count], ties broken alphabetically. */
function topWords(text, n = 3) {
  return [...wordFrequencies(text).entries()]
    .sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1))
    .slice(0, n);
}

/** Whole minutes needed to read text at wpm words per minute (rounded up). */
function readingTime(text, wpm = 200) {
  if (!Number.isFinite(wpm) || wpm <= 0) throw new RangeError('wpm must be a positive number');
  const count = wordCount(text);
  if (count === 0) return 0;
  return Math.floor(count / wpm);
}

module.exports = { words, wordCount, wordFrequencies, topWords, readingTime };
