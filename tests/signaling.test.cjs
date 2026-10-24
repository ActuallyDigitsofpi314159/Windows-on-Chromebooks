const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '..', 'remote_client_low_latency.html'), 'utf8');
const source = html.slice(html.indexOf('function postOffer('), html.indexOf('function cleanup('));

function signaling(fetch, mode = 'stable') {
  const context = vm.createContext({
    fetch, URL, AbortController, performance, setTimeout, clearTimeout,
    ui: {network: {value: mode}, clientToken: {value: 'test-client'}},
    CLIENT_BUILD: 'test-build', signalingMs: null, signalingAbort: null,
  });
  vm.runInContext(source, context);
  return {context, offer: () => context.postOffer('https://example.com', 'test-proof',
    {localDescription: {sdp: 'test-sdp', type: 'offer'}}, {width: 1280, height: 720})};
}

test('offers preserve default stream settings and authentication', async () => {
  const client = signaling(async (url, options) => {
    assert.equal(url, 'https://example.com/offer');
    assert.equal(options.headers['X-Remote-Secret'], 'test-proof');
    assert.equal(options.headers['X-Remote-Host'], 'example.com');
    const body = JSON.parse(options.body);
    assert.equal(body.streamMode, 'stable');
    assert.equal(body.networkMode, 'auto');
    assert.deepEqual(body.screen, {width:1280, height:720});
    return Response.json({sdp:'answer-sdp',type:'answer'});
  });
  assert.equal((await client.offer()).sdp, 'answer-sdp');
  assert.equal(client.context.signalingAbort, null);
});

test('rejected proofs and invalid offers give actionable errors', async () => {
  const rejected = signaling(async () => Response.json({error:'Invalid connection code'}, {status:403}));
  await assert.rejects(rejected.offer(), /Copy a fresh connection blob/);
  const invalid = signaling(async () => Response.json({error:'Invalid offer: bad screen'}, {status:400}));
  await assert.rejects(invalid.offer(), /Host rejected.*bad screen/);
});

test('host capture failures preserve the actual host error', async () => {
  for (const status of [502, 503]) {
    const client = signaling(async () => Response.json({error:'Capture startup failed: encoder unavailable'}, {status}));
    await assert.rejects(client.offer(), new RegExp(`Host capture is unavailable.*${status}.*encoder unavailable`));
  }
});

test('network and gateway failures explain host/tunnel checks', async () => {
  const offline = signaling(async () => {throw new TypeError('Failed to fetch');});
  await assert.rejects(offline.offer(), /Tunnel unavailable or host is down/);
  const gateway = signaling(async () => new Response('<html>Gateway failure</html>', {status:502}));
  await assert.rejects(gateway.offer(), /HTTP 502.*Check the host window/);
});
