'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { readingTime } = require('../lib/stats');

test('readingTime rounds up', () => {
  assert.equal(readingTime('w '.repeat(201)), 2);
  assert.equal(readingTime('one'), 1);
});
test('readingTime empty is zero', () => assert.equal(readingTime('   '), 0));
test('readingTime custom wpm', () => assert.equal(readingTime('a b c d e', 2), 3));
test('readingTime rejects bad wpm', () => {
  assert.throws(() => readingTime('a', 0), RangeError);
  assert.throws(() => readingTime('a', -5), RangeError);
});
