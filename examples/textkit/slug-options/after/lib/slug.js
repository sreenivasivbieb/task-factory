'use strict';

const SEPARATORS = ['-', '_', '.'];

/**
 * Lower-case slug. Throws TypeError for non-strings.
 * options: a number (maxLength) or { maxLength = 64, separator = '-' }.
 */
function slugify(text, options = {}) {
  if (typeof text !== 'string') throw new TypeError('slugify expects a string');
  const { maxLength = 64, separator = '-' } = typeof options === 'number' ? { maxLength: options } : options;
  if (!Number.isInteger(maxLength) || maxLength < 1) throw new RangeError('maxLength must be a positive integer');
  if (!SEPARATORS.includes(separator)) throw new RangeError(`separator must be one of ${SEPARATORS.join(' ')}`);
  const edges = new RegExp(`^\\${separator}+|\\${separator}+$`, 'g');
  const slug = text
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .replace(/[^a-z0-9]+/g, separator)
    .replace(edges, '');
  return slug.slice(0, maxLength).replace(edges, '');
}

module.exports = { slugify };
