# Sentence splitting and average sentence length

Editors want a readability hint. Add a new module `lib/readability.js` that exports two functions,
and export both from the package entry point `index.js` as well.

## `sentences(text)`

Returns the sentences of `text` as an array of trimmed strings, each including its ending
punctuation.

- A sentence ends at one or more of `.`, `!` or `?` that are followed by whitespace or by the end
  of the text: `'Wait... what?! Fine.'` → `['Wait...', 'what?!', 'Fine.']`.
- Punctuation directly followed by another character does not end a sentence, so numbers and
  domains stay whole: `'Pi is 3.14 today. See example.com now'` →
  `['Pi is 3.14 today.', 'See example.com now']`.
- Text after the last sentence end is a sentence too (see the last example).
- A piece without any letter or digit is not a sentence: `'... !!! Hi.'` → `['Hi.']`.
- Empty or whitespace-only text gives `[]`.

## `averageSentenceLength(text)`

The number of words (counted with the existing `wordCount` from `lib/stats.js`) divided by the
number of sentences, **rounded to one decimal place**. Text without sentences gives `0`.
Example: `'One two three. Four five!'` → `2.5`.
