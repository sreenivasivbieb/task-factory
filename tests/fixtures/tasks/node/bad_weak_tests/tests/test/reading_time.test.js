'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { readingTime } = require('../lib/stats');

test('readingTime basic', () => assert.equal(readingTime('w '.repeat(401)), 3));
