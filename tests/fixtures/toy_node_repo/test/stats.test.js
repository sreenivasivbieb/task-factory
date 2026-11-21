'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { wordCount, wordFrequencies, topWords } = require('../lib/stats');

test('wordCount', () => assert.equal(wordCount('  one two\tthree\n'), 3));
test('wordCount empty', () => assert.equal(wordCount(''), 0));
test('wordFrequencies ignores case and punctuation', () => {
  const f = wordFrequencies('The cat. the CAT, a dog!');
  assert.equal(f.get('the'), 2);
  assert.equal(f.get('cat'), 2);
  assert.equal(f.get('dog'), 1);
});
test('topWords ties alphabetical', () => {
  assert.deepEqual(topWords('b a c a b', 2), [['a', 2], ['b', 2]]);
});
test('topWords default n', () => assert.equal(topWords('a b c d e').length, 3));
