// JUnit reporter for node:test, written into the sandbox by the factory.
// Unlike Node's built-in junit reporter (before v23) it always records the
// test file, so test IDs are stable: <file>::<name>.
function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

export default async function* reporter(source) {
  const cases = [];
  for await (const ev of source) {
    if (ev.type !== 'test:pass' && ev.type !== 'test:fail') continue;
    const d = ev.data;
    if (d.details && d.details.type === 'suite') continue;
    let status = ev.type === 'test:pass' ? 'passed' : 'failed';
    if (d.skip || d.todo) status = 'skipped';
    const err = d.details && d.details.error ? String(d.details.error.message || d.details.error) : '';
    cases.push({ name: d.name, file: d.file || '', status, time: ((d.details && d.details.duration_ms) || 0) / 1000, err });
  }
  let out = '<?xml version="1.0" encoding="utf-8"?>\n<testsuites>\n';
  for (const c of cases) {
    out += `<testcase name="${esc(c.name)}" file="${esc(c.file)}" time="${c.time}">`;
    if (c.status === 'failed') out += `<failure message="${esc(c.err.slice(0, 500))}"/>`;
    if (c.status === 'skipped') out += '<skipped/>';
    out += '</testcase>\n';
  }
  yield out + '</testsuites>\n';
}
