'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { render, placeholders } = require('../lib/template');

test('render replaces values', () => assert.equal(render('Hi {{ name }}!', { name: 'Ana' }), 'Hi Ana!'));
test('render repeated', () => assert.equal(render('{{a}}{{a}}', { a: 1 }), '11'));
test('render missing throws', () => assert.throws(() => render('{{x}}', {}), /missing value for x/));
test('placeholders in order', () => assert.deepEqual(placeholders('{{b}} {{a}} {{b}}'), ['b', 'a']));
