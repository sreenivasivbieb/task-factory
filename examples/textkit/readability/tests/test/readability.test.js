'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { sentences, averageSentenceLength } = require('../lib/readability');

test('sentence ends', () => {
  assert.deepEqual(sentences('Wait... what?! Fine.'), ['Wait...', 'what?!', 'Fine.']);
  assert.deepEqual(sentences('One.Two. Three'), ['One.Two.', 'Three']);
  assert.deepEqual(sentences('  Hello world  '), ['Hello world']);
  assert.deepEqual(sentences('Line one.\nLine two!\tLine three?'), ['Line one.', 'Line two!', 'Line three?']);
});

test('numbers and domains stay whole', () => {
  assert.deepEqual(sentences('Pi is 3.14 today. See example.com now'), ['Pi is 3.14 today.', 'See example.com now']);
});

test('pieces without letters or digits are not sentences', () => {
  assert.deepEqual(sentences('... !!! Hi.'), ['Hi.']);
  assert.deepEqual(sentences('Ça va? Ünïcode!'), ['Ça va?', 'Ünïcode!']);
  assert.deepEqual(sentences('   '), []);
  assert.deepEqual(sentences(''), []);
  assert.deepEqual(sentences('?!'), []);
});

test('average sentence length', () => {
  assert.equal(averageSentenceLength('One two three. Four five!'), 2.5);
  assert.equal(averageSentenceLength('a b. c d. e f g.'), 2.3);
  assert.equal(averageSentenceLength('a b c d e f g h i j k l m n o p q r s t'), 20);
  assert.equal(averageSentenceLength('a. b c d e.'), 2.5);
  assert.equal(averageSentenceLength('... ?'), 0);
  assert.equal(averageSentenceLength(''), 0);
});

test('exported from the package entry point', () => {
  const pkg = require('../index');
  assert.equal(pkg.sentences, sentences);
  assert.equal(pkg.averageSentenceLength, averageSentenceLength);
  assert.equal(typeof pkg.slugify, 'function');
});
