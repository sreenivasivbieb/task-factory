'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { wrap, truncate } = require('../lib/wrap');

test('wrap short text', () => assert.deepEqual(wrap('one two', 20), ['one two']));
test('wrap splits lines', () => assert.deepEqual(wrap('aaa bbb ccc', 7), ['aaa bbb', 'ccc']));
test('wrap long word stays whole', () => assert.deepEqual(wrap('abcdefgh', 3), ['abcdefgh']));
test('wrap rejects bad width', () => assert.throws(() => wrap('x', 0), RangeError));
test('truncate keeps short text', () => assert.equal(truncate('hello', 10), 'hello'));
test('truncate adds ellipsis', () => assert.equal(truncate('hello world', 6), 'hello…'));
