'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { wrap } = require('../lib/wrap');

test('slow: wrap a large text', () => {
  const text = 'lorem ipsum dolor sit amet '.repeat(20000);
  const lines = wrap(text, 40);
  assert.ok(lines.every((l) => l.length <= 40));
});
