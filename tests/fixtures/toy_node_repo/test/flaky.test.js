'use strict';
// Flaky on purpose when TOY_FLAKY=1: fails on every second run in the same
// environment (counter file in the temp folder).
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

test('sometimes flaky', () => {
  if (process.env.TOY_FLAKY !== '1') return;
  const counter = path.join(os.tmpdir(), 'textkit_flaky_counter');
  const n = fs.existsSync(counter) ? Number(fs.readFileSync(counter, 'utf8')) : 0;
  fs.writeFileSync(counter, String(n + 1));
  assert.equal(n % 2, 0, 'flaky failure on purpose');
});
