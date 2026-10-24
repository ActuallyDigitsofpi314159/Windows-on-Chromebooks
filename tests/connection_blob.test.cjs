const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const zlib = require('node:zlib');

// Exercise the actual browser decoder with Node's browser-compatible streams.
const html = fs.readFileSync(path.join(__dirname, '..', 'remote_client_low_latency.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script);
const decoder = script.slice(script.indexOf('function decodeConnectionLink('), script.indexOf('function save('));
function client(overrides = {}) {
  const context = vm.createContext({URL, atob, Blob, DecompressionStream, TextDecoder, ...overrides});
  vm.runInContext(decoder, context);
  return context;
}
const {decodeConnection, decodeConnectionLink} = client();
const proof = 'dk1.AAAAAAAAAAAAAAAAAAAAAA';
const link = `https://sample-host.trycloudflare.com/#${proof}`;
const blob = value => 'dk2.' + zlib.deflateSync(Buffer.from(value)).toString('base64url');

test('compressed connection carries the same host and proof as a legacy link', async () => {
  const result = await decodeConnection(blob(link));
  assert.equal(result.host, 'https://sample-host.trycloudflare.com');
  assert.equal(result.hostname, 'sample-host.trycloudflare.com');
  assert.equal(result.proof, proof);
  assert.equal(result.link, link);
});

test('copied blobs tolerate surrounding whitespace and wrapped lines', async () => {
  const encoded = blob(link);
  const result = await decodeConnection(`  ${encoded.slice(0, 20)}\n${encoded.slice(20)}\r\n `);
  assert.equal(result.link, link);
});

test('legacy HTTPS and direct LAN links remain supported', async () => {
  assert.equal((await decodeConnection(link)).link, link);
  assert.equal(decodeConnectionLink(`http://192.168.1.2:8765/#${proof}`).host, 'http://192.168.1.2:8765');
});

test('invalid URLs and incomplete access proofs are rejected', async () => {
  for (const value of [
    `javascript:alert(1)`,
    `https://user:password@example.com/#${proof}`,
    `https://example.com/extra/#${proof}`,
    `https://example.com/?secret=x#${proof}`,
    'https://example.com/#dk1.short',
    'https://example.com/',
  ]) {
    await assert.rejects(decodeConnection(value));
    await assert.rejects(decodeConnection(blob(value)));
  }
});

test('damaged, invalid and oversized compressed input fails clearly', async () => {
  for (const value of ['dk2.invalid', 'dk2.a!', 'dk2.', 'dk2.' + 'a'.repeat(8192)]) {
    await assert.rejects(decodeConnection(value), /blob/);
  }
});

test('decompressed payload size is bounded', async () => {
  await assert.rejects(decodeConnection(blob('x'.repeat(1024 * 1024))), /damaged or incomplete/);
});

test('browsers without decompression get an actionable fallback', async () => {
  const oldBrowser = client({DecompressionStream: undefined});
  await assert.rejects(oldBrowser.decodeConnection(blob(link)), /current browser or a legacy connection link/);
  assert.equal((await oldBrowser.decodeConnection(link)).link, link);
});
