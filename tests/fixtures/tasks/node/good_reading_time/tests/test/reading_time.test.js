'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { readingTime } = require('../lib/stats');

test('readingTime rounds up', () => {
  assert.equal(readingTime('w '.repeat(201)), 2);
  assert.equal(readingTime('one'), 1);
});
test('readingTime empty is zero', () => assert.equal(readingTime('   '), 0));
test('readingTime custom wpm', () => {
  assert.equal(readingTime('a b c d e', 2), 3);
  assert.equal(readingTime('a b c', 1), 3);       // 1 wpm is positive, so it is allowed
  assert.equal(readingTime('a b c', 0.5), 6);     // so is a fraction
});
test('readingTime rejects bad wpm', () => {
  assert.throws(() => readingTime('a', 0), RangeError);
  assert.throws(() => readingTime('a', -5), RangeError);
});
