'use strict';
const { wordCount } = require('./stats');

const SENTENCE_END = /[.!?]+(?=\s|$)/g;
const HAS_CONTENT = /[\p{L}\p{N}]/u;

/** Sentences of text, trimmed, each with its ending punctuation. */
function sentences(text) {
  const out = [];
  let start = 0;
  for (const m of text.matchAll(SENTENCE_END)) {
    const stop = m.index + m[0].length;
    const sentence = text.slice(start, stop).trim();
    if (HAS_CONTENT.test(sentence)) out.push(sentence);
    start = stop;
  }
  const rest = text.slice(start).trim();
  if (HAS_CONTENT.test(rest)) out.push(rest);
  return out;
}

/** Words per sentence, rounded to one decimal; 0 without sentences. */
function averageSentenceLength(text) {
  const count = sentences(text).length;
  if (count === 0) return 0;
  return Math.round((wordCount(text) / count) * 10) / 10;
}

module.exports = { sentences, averageSentenceLength };
