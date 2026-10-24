const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '..', 'remote_client_low_latency.html'), 'utf8');
function source(from, until) { return html.slice(html.indexOf(from), html.indexOf(until)); }

test('receiver requests immediate video and a small separate audio jitter buffer', () => {
  const context = vm.createContext({receiverHint: 'unsupported'});
  vm.runInContext(source('function tuneReceiver(', 'function bitrateBits('), context);
  const video = {track:{kind:'video'}, jitterBufferTarget:null, playoutDelayHint:null};
  const audio = {track:{kind:'audio'}, jitterBufferTarget:null, playoutDelayHint:null};
  context.tuneReceiver(video);
  context.tuneReceiver(audio);
  assert.equal(video.jitterBufferTarget, 0);
  assert.equal(video.playoutDelayHint, 0);
  assert.equal(audio.jitterBufferTarget, 20);
  assert.equal(audio.playoutDelayHint, .02);
  assert.match(context.receiverHint, /target 0 ms/);
  assert.doesNotThrow(() => context.tuneReceiver({track:{kind:'video'}}));
  assert.equal(context.receiverHint, 'unsupported');
});

test('rejected modern buffer hints still allow the legacy hint', () => {
  const context = vm.createContext({receiverHint:'unsupported'});
  vm.runInContext(source('function tuneReceiver(', 'function bitrateBits('), context);
  const receiver = {track:{kind:'video'}, playoutDelayHint:null};
  Object.defineProperty(receiver, 'jitterBufferTarget', {set() {throw new Error('unsupported');}});
  context.tuneReceiver(receiver);
  assert.equal(receiver.playoutDelayHint, 0);
  assert.equal(context.receiverHint, 'legacy 0 ms');
});

test('fast video attaches before asynchronous offer creation can bypass encoded frames', () => {
  const order = [], receiver = {track:{kind:'video'}};
  class Peer {
    addTransceiver(kind) {order.push(kind); return {receiver};}
    createDataChannel() {return {};}
    createOffer() {
      assert.deepEqual(order, ['video', 'audio', 'transform']);
      return new Promise(()=>{});
    }
  }
  const context = vm.createContext({performance,RTCPeerConnection:Peer,RTCRtpScriptTransform:class{},
    ui:{invite:{value:'test'},network:{value:'stable'},host:{},secret:{}},
    decodeConnectionLink:()=>({host:'https://example.com',hostname:'example.com',proof:'test',link:'test'}),
    controllerScreen:()=>({width:1280,height:720}),prepareAudio(){},cleanup(){},save(){},put(){},
    setTimeout:()=>1,gameMouse:false,soundEnabled:true,fastPreferred:true,fastBlocked:false,
    fastSupported:()=>true,startFastPlayback:(connection,value)=>{
      assert.ok(connection instanceof Peer);assert.equal(value,receiver);order.push('transform');return true;
    }});
  vm.runInContext(source('function connect(', 'function scheduleDisconnectedRecovery('), context);
  context.connect(true);
});

test('fast-renderer stalls recover, while hidden tabs and retired peers do not trigger fallback', () => {
  const peer = {}, failures = [];
  const context = vm.createContext({pc:peer,fastActive:true,lastFastFrameAt:1000,document:{hidden:false},
    failFastPlayback:(connection, reason) => failures.push(reason)});
  vm.runInContext(source('function checkFastPlayback(', 'function fastPlaybackWorker('), context);
  assert.equal(context.checkFastPlayback(peer, 4000), false);
  context.document.hidden = true;
  assert.equal(context.checkFastPlayback(peer, 6000), false);
  context.document.hidden = false;
  assert.equal(context.checkFastPlayback({}, 6000), false);
  assert.equal(context.checkFastPlayback(peer, 6000), true);
  assert.deepEqual(failures, ['decoded video stopped progressing']);
});

test('fast-renderer errors switch to standard playback once, ignoring retired peers', () => {
  const peer = {}, reconnects = [];
  const context = vm.createContext({pc:peer,fastBlocked:false,fastFailureReason:'',
    playbackUi:()=>{},notifyInput:()=>{},scheduleReconnect:reason=>reconnects.push(reason)});
  vm.runInContext(source('function failFastPlayback(', 'function checkFastPlayback('), context);
  context.failFastPlayback({}, 'retired peer');
  assert.equal(context.fastBlocked, false);
  context.failFastPlayback(peer, 'decoder unavailable');
  context.failFastPlayback(peer, 'duplicate error');
  assert.equal(context.fastBlocked, true);
  assert.equal(context.fastFailureReason, 'decoder unavailable');
  assert.deepEqual(reconnects, ['Switching to standard playback: decoder unavailable.']);
});

test('worker requests low-latency decoding, renders and closes decoded frames', async () => {
  const messages = [], configurations = [], frames = [];
  class Decoder {
    constructor(callbacks) { this.callbacks=callbacks; this.decodeQueueSize=0; this.state='unconfigured'; }
    static async isConfigSupported(config) { return {supported:true,config}; }
    configure(config) { configurations.push(config); this.state='configured'; }
    decode(chunk) {
      const frame={timestamp:chunk.timestamp,displayWidth:1280,displayHeight:720,closed:false,close(){this.closed=true;}};
      frames.push(frame); this.callbacks.output(frame);
    }
  }
  const context = vm.createContext({self:{},performance,VideoDecoder:Decoder,
    EncodedVideoChunk:class {constructor(options){Object.assign(this, options);}},postMessage:message=>messages.push(message)});
  vm.runInContext(source('function fastPlaybackWorker(', 'function startFastPlayback('), context);
  context.fastPlaybackWorker();
  const encoded={type:'key',timestamp:0,data:Uint8Array.from([0,0,0,1,0x67,0x42,0xe0,0x1f,0,0,1,0x65]).buffer};
  const inputs=[{done:false,value:encoded},{done:true}];
  let draws=0;
  const canvas = {getContext:()=>({drawImage(){draws++;}})};
  const readable = {getReader:()=>({read:async()=>inputs.shift(),cancel:async()=>{}})};
  context.self.onmessage({data:{type:'start',canvas,readable}});
  await new Promise(setImmediate);
  assert.equal(configurations[0].codec, 'avc1.42e01f');
  assert.equal(configurations[0].optimizeForLatency, true);
  assert.equal(configurations[0].hardwareAcceleration, 'prefer-hardware');
  assert.equal(draws, 1);
  assert.equal(frames[0].closed, true);
  assert.equal(messages.some(message=>message.type==='error'), false);
  assert.equal(messages.some(message=>message.type==='ready'), true);
});

test('autoplay suspension is explained immediately even if resume stays pending', async () => {
  let resolveResume;
  const notifications = [];
  const audio = {state:'suspended',resume:()=>new Promise(resolve=>{resolveResume=resolve;})};
  const context = vm.createContext({window:{AudioContext:class{}},document:{addEventListener(){}},soundEnabled:true,
    audioContext:audio,audioReceived:true,audioPlaybackError:'',updateAudioHint(){},
    notifyInput:message=>notifications.push(message),ui:{notice:{textContent:''}}});
  vm.runInContext(source('function prepareAudio(', 'function playRemoteAudio('), context);
  const preparing = context.prepareAudio();
  assert.match(context.audioPlaybackError, /Click the stream/);
  assert.match(notifications[0], /enable audio/);
  audio.state='running'; resolveResume();
  assert.equal(await preparing, true);
  assert.equal(context.audioPlaybackError, '');
});

test('a stream gesture retries suspended audio without waiting for an autoplay rejection', async () => {
  let resumes=0,attached=0;
  const track={},audio={state:'suspended',resume:async()=>{resumes++;audio.state='running';}};
  const context = vm.createContext({window:{AudioContext:class{}},document:{addEventListener(){}},soundEnabled:true,
    audioContext:audio,audioReceived:true,audioSource:null,audioPlaybackError:'',updateAudioHint(){},notifyInput(){},
    ui:{notice:{textContent:''},audio:{srcObject:{getAudioTracks:()=>[track]}}},playRemoteAudio:value=>{assert.equal(value,track);attached++;}});
  vm.runInContext(source('function prepareAudio(', 'function playRemoteAudio('), context);
  context.unlockAudioFromGesture();
  await new Promise(setImmediate);
  assert.equal(resumes, 1);
  assert.equal(attached, 1);
  context.unlockAudioFromGesture();
  assert.equal(resumes, 1);
});
