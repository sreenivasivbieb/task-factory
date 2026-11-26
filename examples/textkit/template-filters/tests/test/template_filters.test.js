'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { render, placeholders } = require('../lib/template');

test('single filters', () => {
  assert.equal(render('{{ name | upper }}', { name: 'Ana' }), 'ANA');
  assert.equal(render('{{name|lower}}', { name: 'ANA' }), 'ana');
  assert.equal(render('[{{ x | trim }}]', { x: '  hi  ' }), '[hi]');
  assert.equal(render('{{ t | title }}', { t: 'hELLO wORLD  again' }), 'Hello World  Again');
});

test('filters chain left to right and stringify values', () => {
  assert.equal(render('{{ n | trim | upper }}!', { n: '  bob ' }), 'BOB!');
  assert.equal(render('{{ n|upper|lower }}', { n: 'MiXeD' }), 'mixed');
  assert.equal(render('{{ v | upper }}', { v: 42 }), '42');
  assert.equal(render('{{a}}-{{ a | upper }}', { a: 'x' }), 'x-X');
});

test('unknown filters throw, including object properties', () => {
  assert.throws(() => render('{{ a | shout }}', { a: 'x' }), { message: 'unknown filter "shout"' });
  assert.throws(() => render('{{ a | constructor }}', { a: 'x' }), { message: 'unknown filter "constructor"' });
  assert.throws(() => render('{{ a | upper | tostring }}', { a: 'x' }), /unknown filter "tostring"/);
});

test('missing values still throw with filters', () => {
  assert.throws(() => render('{{ x | upper }}', {}), /missing value for x/);
});

test('placeholders ignore filters', () => {
  assert.deepEqual(placeholders('{{a | upper}} {{b}} {{ a|trim }}'), ['a', 'b']);
  assert.deepEqual(placeholders('Hi {{ name | title }}'), ['name']);
});
