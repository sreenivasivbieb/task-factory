'use strict';

/** Replace {{name}} placeholders with values. Unknown names throw. */
function render(template, values) {
  return template.replace(/\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}/g, (_, name) => {
    if (!(name in values)) throw new Error(`missing value for ${name}`);
    return String(values[name]);
  });
}

/** Names of all placeholders, in order of first appearance. */
function placeholders(template) {
  const seen = [];
  for (const m of template.matchAll(/\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}/g)) {
    if (!seen.includes(m[1])) seen.push(m[1]);
  }
  return seen;
}

module.exports = { render, placeholders };
