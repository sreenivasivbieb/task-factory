'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { slugify } = require('../lib/slug');

test('slugify basic', () => assert.equal(slugify('Hello World'), 'hello-world'));
test('slugify strips accents', () => assert.equal(slugify('Crème Brûlée'), 'creme-brulee'));
test('slugify trims dashes', () => assert.equal(slugify('  --Hi!--  '), 'hi'));
test('slugify max length', () => assert.equal(slugify('a b c d e f', 5), 'a-b-c'));
test('slugify rejects non-string', () => assert.throws(() => slugify(42), TypeError));
