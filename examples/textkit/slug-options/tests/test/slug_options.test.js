'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { slugify } = require('../lib/slug');

test('separator option', () => {
  assert.equal(slugify('Hello, World!', { separator: '_' }), 'hello_world');
  assert.equal(slugify('  Crème -- Brûlée  ', { separator: '.' }), 'creme.brulee');
  assert.equal(slugify('a-b c', { separator: '_' }), 'a_b_c');
  assert.equal(slugify('Hello World', {}), 'hello-world');
});

test('maxLength option and the number shorthand', () => {
  assert.equal(slugify('a b c d e f', { maxLength: 5 }), 'a-b-c');
  assert.equal(slugify('a b c d e f', 5), 'a-b-c');
  assert.equal(slugify('ab cd', { maxLength: 3, separator: '.' }), 'ab');
  assert.equal(slugify('ab cd', { maxLength: 1 }), 'a');
  assert.equal(slugify('x'.repeat(70), { separator: '_' }).length, 64);
});

test('bad separators are rejected', () => {
  for (const separator of ['', ' ', '+', '--', '/', 7]) {
    assert.throws(() => slugify('a b', { separator }), RangeError, String(separator));
  }
});

test('maxLength must be a positive integer', () => {
  for (const maxLength of [0, -1, 2.5, NaN, '5']) {
    assert.throws(() => slugify('a b', { maxLength }), RangeError, String(maxLength));
  }
  assert.throws(() => slugify('a b', 0), RangeError);
  assert.throws(() => slugify(5, { separator: '_' }), TypeError);
});
