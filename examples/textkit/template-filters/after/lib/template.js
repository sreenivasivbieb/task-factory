'use strict';

const FILTERS = {
  upper: (s) => s.toUpperCase(),
  lower: (s) => s.toLowerCase(),
  trim: (s) => s.trim(),
  title: (s) => s.toLowerCase().replace(/(^|\s)(\S)/g, (_, space, c) => space + c.toUpperCase()),
};

const PLACEHOLDER = /\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)((?:\s*\|\s*[a-z]+)*)\s*\}\}/g;

function applyFilters(value, chain) {
  let out = String(value);
  for (const name of chain.split('|').map((f) => f.trim()).filter(Boolean)) {
    if (!Object.hasOwn(FILTERS, name)) throw new Error(`unknown filter "${name}"`);
    out = FILTERS[name](out);
  }
  return out;
}

/** Replace {{name}} or {{name | filter | ...}} placeholders with values. Unknown names throw. */
function render(template, values) {
  return template.replace(PLACEHOLDER, (_, name, chain) => {
    if (!(name in values)) throw new Error(`missing value for ${name}`);
    return applyFilters(values[name], chain);
  });
}

/** Names of all placeholders, in order of first appearance. */
function placeholders(template) {
  const seen = [];
  for (const m of template.matchAll(PLACEHOLDER)) {
    if (!seen.includes(m[1])) seen.push(m[1]);
  }
  return seen;
}

module.exports = { render, placeholders };
