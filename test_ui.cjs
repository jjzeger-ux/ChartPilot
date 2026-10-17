const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('every inline script parses', () => {
  for (const file of fs.readdirSync('templates')) {
    const html = fs.readFileSync(`templates/${file}`, 'utf8');
    for (const [, script] of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) {
      new vm.Script(script, { filename: file });
    }
  }
});

test('patient queue escapes user-provided labels', () => {
  const source = fs.readFileSync('templates/index.html', 'utf8');
  const start = source.indexOf('function escapeHtml(');
  const end = source.indexOf('\nrefreshQueue();', start);
  const queue = {};
  const context = vm.createContext({
    activeEid: 'ABC', STATUS_LABELS: { waiting: 'Waiting' },
    document: { getElementById: () => queue },
  });
  vm.runInContext(source.slice(start, end), context);
  context.renderQueue([{ id: 'ABC', mrn: '<b>Test</b>', initials: '<i>',
    room: '<img src=x>', provider: 'A&B', status: 'waiting' }]);
  assert.ok(queue.innerHTML.includes('&lt;b&gt;Test&lt;/b&gt;'));
  assert.ok(queue.innerHTML.includes('&lt;img src=x&gt;'));
  assert.ok(queue.innerHTML.includes('A&amp;B'));
  assert.ok(!queue.innerHTML.includes('<img src=x>'));
});
