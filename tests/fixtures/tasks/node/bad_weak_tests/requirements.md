# Reading time estimate

Add `readingTime(text, wpm = 200)` to `lib/stats.js` and export it (it is then
also exported from `index.js`).

- Returns the number of whole minutes needed to read `text` at `wpm` words per
  minute, rounded **up** (`Math.ceil(words / wpm)`). Words are counted with the
  existing `wordCount`.
- Empty or whitespace-only text returns `0`.
- `wpm` must be a positive finite number; otherwise throw a `RangeError`.
