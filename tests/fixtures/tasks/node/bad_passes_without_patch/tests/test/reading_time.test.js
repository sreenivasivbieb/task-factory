'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { wordCount, readingTime } = require('../lib/stats');

test('word count still works', () => assert.equal(wordCount('a b c'), 3));
test('readingTime rounds up', () => assert.equal(readingTime('w '.repeat(201)), 2));
