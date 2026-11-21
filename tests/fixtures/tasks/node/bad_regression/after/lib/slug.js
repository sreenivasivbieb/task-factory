'use strict';

/** Lower-case, dash-separated slug. Throws TypeError for non-strings. */
function slugify(text, maxLength = 10) {
  if (typeof text !== 'string') throw new TypeError('slugify expects a string');
  const slug = text
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return slug.slice(0, maxLength).replace(/-+$/, '');
}

module.exports = { slugify };
