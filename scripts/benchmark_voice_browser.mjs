/**
 * Actual WebRTC/browser diagnostic, with WAV fixtures replacing getUserMedia.
 * Usage: node scripts/benchmark_voice_browser.mjs --manifest artifacts/voice-fixtures.json
 * Manifest: {fixtures:[{id:'faq',kind:'faq',text:'Какой ваш адрес?',wav:'question.wav'},
 *                     {id:'conversation',kind:'llm',text:'...',wav:'conversation.wav'}]}
 * WAV paths are relative to the manifest. Optional transcript_pattern/reply_pattern are regex strings.
 * No raw microphone, external staff actions, or booking confirmations are used.
 */
import {chromium} from '@playwright/test';
import fs from 'node:fs/promises';
import path from 'node:path';
import {parseArgs} from 'node:util';

const {values} = parseArgs({options: {
  manifest: {type: 'string', default: 'artifacts/voice-fixtures.json'},
  output: {type: 'string', default: 'artifacts/voice-browser-latency.json'},
  base: {type: 'string', default: process.env.VOICE_BASE_URL || 'http://127.0.0.1:7860/'},
  repeats: {type: 'string', default: '5'},
  warmups: {type: 'string', default: '1'},
  timeout: {type: 'string', default: '120000'},
  threshold: {type: 'string', default: '0.003'},
  profiles: {type: 'string', default: 'male,female'},
  headed: {type: 'boolean', default: false},
}});
const repeats = Number(values.repeats), warmups = Number(values.warmups), timeout = Number(values.timeout);
const threshold = Number(values.threshold);
if (!Number.isInteger(repeats) || repeats < 5 || !Number.isInteger(warmups) || warmups < 1 ||
    !Number.isFinite(timeout) || timeout < 1000 || !Number.isFinite(threshold) || threshold <= 0 || threshold >= 1) {
  throw new Error('Require repeats >= 5, warmups >= 1, timeout >= 1000 and 0 < threshold < 1');
}
const profiles = values.profiles.split(',');
if (!profiles.length || profiles.some(profile => !['male', 'female'].includes(profile))) {
  throw new Error('--profiles must contain male and/or female');
}
const manifestPath = path.resolve(values.manifest);
const manifest = JSON.parse(await fs.readFile(manifestPath, 'utf8'));
const fixtures = Array.isArray(manifest) ? manifest : manifest.fixtures;
if (!Array.isArray(fixtures) || !fixtures.some(f => f.kind === 'faq') || !fixtures.some(f => f.kind === 'llm')) {
  throw new Error('The fixture manifest must contain FAQ and conversational LLM questions');
}
for (const fixture of fixtures) {
  if (!fixture.id || !fixture.text || !fixture.wav || !['faq', 'llm'].includes(fixture.kind)) {
    throw new Error('Each fixture requires id, text, wav and kind (faq|llm)');
  }
  fixture.wav = path.resolve(path.dirname(manifestPath), fixture.wav);
  fixture.base64 = (await fs.readFile(fixture.wav)).toString('base64');
  for (const key of ['transcript_pattern', 'reply_pattern']) {
    if (fixture[key]) new RegExp(fixture[key], 'iu');
  }
}

const report = {
  started_at: new Date().toISOString(), base_url: values.base, threshold,
  observation: 'Decoded remote MediaStream PCM while the HTML audio element is playing; no physical speaker measurement.',
  response_measurement: 'Useful response only; waiting acknowledgement is reported separately as first sound. Turns without a quiet boundary between acknowledgement and response are invalid for decoded response timing.',
  clock: 'Shared browser AudioContext; question end is the last WAV sample exceeding the threshold.',
  output_latency: 'baseLatency/outputLatency belong to the analysis AudioContext. The HTML audio element uses its native playback path, so these values cannot estimate its speaker latency.',
  limitations: 'Synthetic speech and local Chromium; at least five warm observations are diagnostic, not sufficient for a p95 acceptance claim.',
  configuration: {repeats, warmups, profiles, timeout_ms: timeout, welcome_quiet_seconds: 3, browser_isolation: 'new_process_per_profile'}, fixtures: fixtures.map(({base64, ...fixture}) => fixture),
  runs: [], summaries: [], errors: [],
};
const outputPath = path.resolve(values.output);
await fs.mkdir(path.dirname(outputPath), {recursive: true});
async function save() { await fs.writeFile(outputPath, JSON.stringify(report, null, 2) + '\n'); }

let browser;
try {
  for (const profile of profiles) {
    // Chromium on macOS can stall a second MediaStream AudioContext after the
    // previous profile is disposed. A fresh process gives each profile the
    // same cold browser state and keeps the shared audio clock advancing.
    browser = await chromium.launch({headless: !values.headed, args: ['--autoplay-policy=no-user-gesture-required']});
    const context = await browser.newContext({ignoreHTTPSErrors: true, viewport: {width: 1440, height: 1000}});
    const page = await context.newPage();
    page.setDefaultTimeout(timeout);
    let syntheticSession;
    await page.addInitScript(({threshold}) => {
      const state = {
        events: [], botSpeaking: false, lastAudioTime: null, seenAudio: false,
        pollMaxGapMs: 0, previousPollMs: null, active: null, observerMode: null,
      };
      let audio, microphone, silentMicrophone, analyser, silentOutput, remoteSource, observedStream;
      const peers = [];
      const NativePeer = window.RTCPeerConnection;
      window.RTCPeerConnection = class extends NativePeer {
        constructor(...args) {super(...args); peers.push(this);}
      };
      const samples = new Float32Array(256);
      const record = (type, payload = {}) => {
        const entry = {type, performance_ms: performance.now(), audio_seconds: audio?.currentTime ?? null, ...payload};
        state.events.push(entry);
        return entry;
      };
      const NativeSocket = window.WebSocket;
      window.WebSocket = class extends NativeSocket {
        constructor(...args) {
          super(...args);
          this.addEventListener('message', message => {
            try {
              const payload = JSON.parse(message.data);
              if (payload.type === 'bot_speaking') state.botSpeaking = payload.speaking;
              // Intent also protects a clip cancelled after its first transport
              // write but before the downstream delivery event could arrive.
              if (payload.type === 'acknowledgement' && state.active) state.active.acknowledgement_seen = true;
              if (payload.type === 'voice_audio' && state.active) {
                if (payload.kind === 'acknowledgement') state.active.acknowledgement_seen = true;
                if (payload.kind === 'response' && state.active.response_signal_audio_seconds === null) {
                  state.active.response_signal_audio_seconds = audio?.currentTime ?? null;
                  state.active.response_has_quiet_boundary = state.lastAudioTime === null ||
                    audio.currentTime - state.lastAudioTime >= 0.04;
                }
              }
              if (['turn', 'reply', 'turn_latency', 'voice_stage', 'voice_audio', 'acknowledgement', 'speech_started', 'bot_speaking', 'playback_marker', 'error',
                'ended', 'consent_granted', 'queue_ready', 'queue_update'].includes(payload.type)) record(payload.type, payload);
            } catch {}
          });
        }
      };
      const nativeFetch = window.fetch.bind(window);
      window.fetch = async (...args) => {
        const result = await nativeFetch(...args);
        const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
        if (url.includes('/playbacks/') && args[1]?.method === 'PUT') {
          let body = {};
          try { body = JSON.parse(args[1].body); } catch {}
          record('browser_playback_receipt', {status: result.status, played_chars: body.played_chars,
            utterance_id: url.split('/playbacks/')[1]});
        }
        return result;
      };
      const ensureAudio = async () => {
        if (!audio) audio = new AudioContext({sampleRate: 48000, latencyHint: 'interactive'});
        await audio.resume();
        return audio;
      };
      Object.defineProperty(navigator.mediaDevices, 'getUserMedia', {configurable: true, value: async constraints => {
        if (!constraints?.audio || constraints.video) throw new Error('Benchmark supports synthetic audio only');
        await ensureAudio();
        if (!microphone) {
          microphone = audio.createMediaStreamDestination();
          // A real microphone continues sending quiet samples between utterances.
          // Keep the synthetic graph alive after each finite WAV source ends too.
          silentMicrophone = audio.createConstantSource();
          silentMicrophone.offset.value = 0;
          silentMicrophone.connect(microphone);
          silentMicrophone.start();
        }
        return microphone.stream;
      }});
      const observerTimer = setInterval(async () => {
        const element = document.querySelector('#remote-audio');
        if (!element?.srcObject || !audio || element.paused || element.muted || element.volume === 0) return;
        if (observedStream !== element.srcObject) {
          remoteSource?.disconnect();
          observedStream = element.srcObject;
          remoteSource = audio.createMediaStreamSource(observedStream);
          analyser ||= audio.createAnalyser();
          analyser.fftSize = samples.length;
          silentOutput ||= audio.createGain();
          silentOutput.gain.value = 0;
          remoteSource.connect(analyser);
          analyser.connect(silentOutput);
          silentOutput.connect(audio.destination); // Pull analysis without duplicating element playback.
          state.observerMode = 'playing_remote_audio_element_srcObject';
          record('remote_audio_attached', {audio_sample_rate: audio.sampleRate});
        }
        const pollMs = performance.now();
        if (state.previousPollMs !== null) state.pollMaxGapMs = Math.max(state.pollMaxGapMs, pollMs - state.previousPollMs);
        state.previousPollMs = pollMs;
        analyser.getFloatTimeDomainData(samples);
        let first = -1, last = -1;
        for (let index = 0; index < samples.length; index++) {
          if (Math.abs(samples[index]) >= threshold) { if (first < 0) first = index; last = index; }
        }
        if (first >= 0) {
          const blockStart = audio.currentTime - samples.length / audio.sampleRate;
          const firstTime = blockStart + first / audio.sampleRate;
          const precedingQuiet = state.lastAudioTime === null ? Infinity : firstTime - state.lastAudioTime;
          state.lastAudioTime = blockStart + last / audio.sampleRate;
          state.seenAudio = true;
          if (state.active && !state.active.first_audio && state.lastAudioTime >= state.active.input_end_audio_seconds) {
            const detected = Math.max(firstTime, state.active.input_end_audio_seconds);
            state.active.first_audio = record('browser_first_sound', {detected_audio_seconds: detected});
          }
          if (state.active?.acknowledgement_seen && !state.active.first_substantive_audio &&
              state.active.response_signal_audio_seconds !== null && firstTime >= state.active.response_signal_audio_seconds) {
            state.active.substantive_boundary_ambiguous = !(state.active.response_has_quiet_boundary || precedingQuiet >= 0.04);
            state.active.first_substantive_audio = record('browser_first_substantive_audio', {detected_audio_seconds: firstTime});
          }
        }
      }, 4);
      window.voiceBenchmark = {
        state,
        async networkStats() {
          const result = [];
          for (const peer of peers) {
            if (peer.connectionState === 'closed') continue;
            const stats = await peer.getStats();
            for (const entry of stats.values()) {
              if (['inbound-rtp', 'outbound-rtp', 'media-source'].includes(entry.type) &&
                  (entry.kind === 'audio' || entry.mediaType === 'audio')) {
                const {id, type, kind, timestamp, packetsSent, packetsReceived, packetsLost, jitter,
                  jitterBufferDelay, jitterBufferEmittedCount, totalSamplesReceived, concealedSamples,
                  silentConcealedSamples, totalAudioEnergy, totalSamplesDuration} = entry;
                result.push({id, type, kind, timestamp, packetsSent, packetsReceived, packetsLost, jitter,
                  jitterBufferDelay, jitterBufferEmittedCount, totalSamplesReceived, concealedSamples,
                  silentConcealedSamples, totalAudioEnergy, totalSamplesDuration});
              }
            }
          }
          return result;
        },
        snapshot() {
          return {performance_ms: performance.now(), events: state.events, botSpeaking: state.botSpeaking, seenAudio: state.seenAudio,
            currentAudioTime: audio?.currentTime ?? null, lastAudioTime: state.lastAudioTime,
            observerMode: state.observerMode, pollMaxGapMs: state.pollMaxGapMs, active: state.active,
            sampleRate: audio?.sampleRate, baseLatency: audio?.baseLatency, outputLatency: audio?.outputLatency ?? null,
            outputTimestamp: audio?.getOutputTimestamp?.() ?? null, audioContextState: audio?.state ?? null,
            remoteAudio: {paused: document.querySelector('#remote-audio')?.paused,
              currentTime: document.querySelector('#remote-audio')?.currentTime}};
        },
        isQuiet(minSeconds = 0.8) {
          return state.seenAudio && !state.botSpeaking && state.lastAudioTime !== null &&
            audio.currentTime - state.lastAudioTime >= minSeconds;
        },
        async inject(base64, metadata) {
          if (!this.isQuiet()) throw new Error('Previous remote speech has not finished');
          await ensureAudio();
          const bytes = Uint8Array.from(atob(base64), c => c.charCodeAt(0));
          const buffer = await audio.decodeAudioData(bytes.buffer);
          let lastAudible = -1;
          for (let channel = 0; channel < buffer.numberOfChannels; channel++) {
            const pcm = buffer.getChannelData(channel);
            for (let index = pcm.length - 1; index > lastAudible; index--) {
              if (Math.abs(pcm[index]) >= threshold) {lastAudible = index; break;}
            }
          }
          if (lastAudible < 0) throw new Error('WAV fixture contains no audible PCM samples');
          const source = audio.createBufferSource();
          source.buffer = buffer;
          source.connect(microphone);
          const start = audio.currentTime + 0.1;
          state.pollMaxGapMs = 0;
          state.active = {...metadata, performance_start_ms: performance.now(), audio_clock_start_seconds: audio.currentTime, event_start_index: state.events.length, wav_seconds: buffer.duration,
            input_start_audio_seconds: start, input_end_audio_seconds: start + (lastAudible + 1) / buffer.sampleRate,
            wav_trailing_silence_seconds: buffer.duration - (lastAudible + 1) / buffer.sampleRate,
            first_audio: null, first_substantive_audio: null, acknowledgement_seen: false,
            response_signal_audio_seconds: null, response_has_quiet_boundary: false,
            substantive_boundary_ambiguous: false};
          source.onended = () => { source.disconnect(); record('question_source_finished'); };
          source.start(start);
          record('question_source_scheduled', {scheduled_audio_seconds: start});
          return state.active;
        },
        async close() {clearInterval(observerTimer); silentMicrophone?.stop(); silentMicrophone?.disconnect(); remoteSource?.disconnect(); await audio?.close();},
      };
    }, {threshold});
    const pageErrors = [];
    page.on('pageerror', error => pageErrors.push(error.message));
    try {
      await page.goto(values.base);
      await page.waitForFunction(() => document.querySelector('#health-label')?.textContent.includes('на связи'), null, {timeout});
      await page.locator(`[data-voice="${profile}"]`).click();
      await page.locator('#consent').check();
      const created = page.waitForResponse(r => r.url().endsWith('/api/v1/sessions') && r.request().method() === 'POST');
      await page.locator('#start-call').click();
      const response = await created;
      if (!response.ok()) throw new Error(`Synthetic session creation returned ${response.status()}`);
      syntheticSession = await response.json();
      await page.locator('#call-controls').waitFor({state: 'visible', timeout});
      // The approved checkbox-consent greeting is emitted as four sentences.
      // Silence alone also occurs between synthesized sentences and is not completion.
      await page.waitForFunction(() =>
        window.voiceBenchmark.state.events.filter(event => event.type === 'bot_speaking' && !event.speaking).length >= 4 &&
        window.voiceBenchmark.isQuiet(3), null, {timeout});
      const welcome = await page.evaluate(() => window.voiceBenchmark.snapshot());
      report.runs.push({profile, phase: 'welcome', session_id: syntheticSession.id,
        observer_mode: welcome.observerMode, events: welcome.events,
        audio_sample_rate: welcome.sampleRate, base_latency_seconds: welcome.baseLatency,
        output_latency_seconds: welcome.outputLatency});
      for (const fixture of fixtures) {
        for (let iteration = 0; iteration < warmups + repeats; iteration++) {
          const phase = iteration < warmups ? 'warmup' : 'measured';
          const metadata = {profile, fixture_id: fixture.id, kind: fixture.kind, phase, iteration};
          await page.waitForFunction(() => window.voiceBenchmark.isQuiet(0.8), null, {timeout});
          await page.evaluate(({base64, metadata}) => window.voiceBenchmark.inject(base64, metadata),
            {base64: fixture.base64, metadata});
          await page.waitForFunction(() => {
            const {state} = window.voiceBenchmark;
            const events = state.events.slice(state.active.event_start_index);
            const reply = events.find(event => event.type === 'reply' && event.text);
            return state.active.first_audio && reply && events.some(event => event.type === 'turn') &&
              events.some(event => event.type === 'browser_playback_receipt' && event.status === 200 &&
                event.played_chars >= [...reply.text].length) && window.voiceBenchmark.isQuiet(0.8);
          }, null, {timeout});
          const snapshot = await page.evaluate(() => window.voiceBenchmark.snapshot());
          const network = await page.evaluate(() => window.voiceBenchmark.networkStats());
          const active = snapshot.active;
          const events = snapshot.events.slice(active.event_start_index);
          const turn = events.find(event => event.type === 'turn');
          const reply = events.find(event => event.type === 'reply');
          const stageEvents = events.filter(event => event.type === 'voice_stage');
          const finalSTT = stageEvents.find(event => event.stage === 'stt_final_ready');
          const stageGeneration = finalSTT?.generation ?? stageEvents.find(event => event.stage === 'dialogue_started')?.generation;
          const currentStages = stageEvents.filter(event => event.generation === stageGeneration);
          const stage = name => currentStages.find(event => event.stage === name);
          const stageSeconds = name => stage(name)?.server_perf_seconds;
          const interval = (start, end) => Number.isFinite(start) && Number.isFinite(end) && end >= start ? end - start : null;
          const speechEnd = stage('speech_endpoint')?.speech_end_perf_seconds;
          const stageDurations = {
            speech_end_to_endpoint: interval(speechEnd, stageSeconds('speech_endpoint')),
            endpoint_to_stt_final_ready: interval(stageSeconds('speech_endpoint'), stageSeconds('stt_final_ready')),
            prepare: interval(stageSeconds('prepare_started'), stageSeconds('prepare_ready')),
            dialogue: interval(stageSeconds('dialogue_started'), stageSeconds('dialogue_done')),
            dialogue_done_to_tts_request: interval(stageSeconds('dialogue_done'), stageSeconds('tts_requested')),
            tts_request_to_first_useful_pcm: interval(stageSeconds('tts_requested'), stageSeconds('first_useful_server_pcm')),
            first_pcm_to_transport: interval(stageSeconds('first_useful_server_pcm'), stageSeconds('first_response_transport_pcm')),
            speech_end_to_first_useful_pcm: interval(speechEnd, stageSeconds('first_useful_server_pcm')),
          };
          const substantive = active.acknowledgement_seen ? active.first_substantive_audio : active.first_audio;
          const seconds = substantive ? substantive.detected_audio_seconds - active.input_end_audio_seconds : null;
          const errors = events.filter(event => event.type === 'error');
          const clockDrift = (snapshot.performance_ms - active.performance_start_ms) / 1000 -
            (snapshot.currentAudioTime - active.audio_clock_start_seconds);
          const run = {...metadata, session_id: syntheticSession.id,
            question_text: fixture.text, transcript: turn?.text, reply_text: reply?.text,
            response_source: reply?.response_source ?? null,
            server_stage_clock: 'time.perf_counter; durations use only this server clock, never subtract browser timestamps',
            server_stage_generation: stageGeneration ?? null,
            server_stages: currentStages,
            server_stage_durations_seconds: stageDurations,
            prepared_forwarded_to_dialogue: finalSTT?.prepared_available ?? null,
            browser_decoded_response_seconds: seconds,
            browser_decoded_first_sound_seconds: active.first_audio.detected_audio_seconds - active.input_end_audio_seconds,
            acknowledgement_sent: active.acknowledgement_seen,
            server_acknowledgement_seconds: events.find(event => event.type === 'voice_audio' && event.kind === 'acknowledgement')?.seconds ?? null,
            substantive_boundary_ambiguous: active.substantive_boundary_ambiguous,
            server_turn_latency_seconds: events.find(event => event.type === 'turn_latency')?.seconds ?? null,
            input_start_audio_seconds: active.input_start_audio_seconds,
            input_end_audio_seconds: active.input_end_audio_seconds,
            first_response_audio_seconds: substantive?.detected_audio_seconds ?? null,
            first_sound_audio_seconds: active.first_audio.detected_audio_seconds,
            wav_seconds: active.wav_seconds, wav_trailing_silence_seconds: active.wav_trailing_silence_seconds,
            audio_sample_rate: snapshot.sampleRate, base_latency_seconds: snapshot.baseLatency,
            output_latency_seconds: snapshot.outputLatency, output_timestamp: snapshot.outputTimestamp,
            analyser_window_seconds: 256 / snapshot.sampleRate,
            maximum_analysis_poll_gap_ms: snapshot.pollMaxGapMs,
            observer_mode: snapshot.observerMode, analysis_audio_context_state: snapshot.audioContextState,
            remote_audio: snapshot.remoteAudio, audio_clock_drift_seconds: clockDrift, network_stats: network, events, errors};
          if (snapshot.audioContextState !== 'running') errors.push({type: 'validation', message: 'Analysis AudioContext is not running'});
          if (Math.abs(clockDrift) > 0.25) errors.push({type: 'validation', message: 'AudioContext clock stalled or drifted from wall clock'});
          if (fixture.kind === 'llm' && reply?.response_source) errors.push({type: 'validation', message: `Expected LLM conversation, received ${reply.response_source}`});
          if (!Number.isFinite(seconds) || seconds < 0) errors.push({type: 'validation', message: 'Invalid decoded response latency'});
          if (active.substantive_boundary_ambiguous) errors.push({type: 'validation', message: 'No quiet boundary separates acknowledgement and substantive PCM; useful-response timing is ambiguous'});
          for (const [pattern, actual, label] of [
            [fixture.transcript_pattern, turn?.text, 'transcript'], [fixture.reply_pattern, reply?.text, 'reply'],
          ]) if (pattern && !new RegExp(pattern, 'iu').test(actual || '')) errors.push({type: 'validation', message: `Unexpected ${label}`});
          report.runs.push(run);
          await save();
          process.stdout.write(`${profile} ${fixture.id} ${phase} ${iteration + 1}: useful=${seconds?.toFixed(3) ?? 'unknown'}s first-sound=${run.browser_decoded_first_sound_seconds.toFixed(3)}s server=${run.server_turn_latency_seconds ?? 'unknown'}s\n`);
          if (errors.length) {
            report.errors.push({...metadata, message: `Synthetic turn failed validation: ${errors.map(e => e.message).join('; ')}`});
            process.exitCode = 1;
          }
        }
      }
      if (pageErrors.length) throw new Error(`Browser page errors: ${pageErrors.join('; ')}`);
    } catch (error) {
      const diagnostics = await page.evaluate(() => window.voiceBenchmark?.snapshot()).catch(() => null);
      report.errors.push({profile, message: error.message, page_errors: pageErrors, diagnostics});
      const active = diagnostics?.active;
      if (active && !report.runs.some(run => run.profile === profile && run.fixture_id === active.fixture_id &&
          run.phase === active.phase && run.iteration === active.iteration)) {
        report.runs.push({profile, fixture_id: active.fixture_id, kind: active.kind, phase: active.phase,
          iteration: active.iteration, session_id: syntheticSession?.id, browser_decoded_response_seconds: null,
          events: diagnostics.events.slice(active.event_start_index), errors: [{type: 'transport', message: error.message}]});
      }
      await page.screenshot({path: outputPath.replace(/\.json$/, '') + `-${profile}-failure.png`, fullPage: true}).catch(() => {});
      process.exitCode = 1;
    } finally {
      if (syntheticSession) {
        // End only the synthetic session created by this harness; no operator actions.
        try {
          let result;
          if (await page.locator('#end-call').isVisible()) {
            const ended = page.waitForResponse(response => response.request().method() === 'DELETE' &&
              response.url().endsWith(`/api/v1/sessions/${syntheticSession.id}`), {timeout: 5000}).catch(() => null);
            await page.locator('#end-call').click();
            result = await ended;
          }
          result ||= await context.request.delete(new URL(`/api/v1/sessions/${syntheticSession.id}`, values.base).href,
            {headers: {Authorization: `Bearer ${syntheticSession.token}`}});
          report.runs.push({profile, phase: 'cleanup', session_id: syntheticSession.id, status: result.status()});
          if (![204, 404, 409].includes(result.status())) report.errors.push({profile, message: `Cleanup returned ${result.status()}`});
        } catch (error) {report.errors.push({profile, message: `Cleanup failed: ${error.message}`}); process.exitCode = 1;}
      }
      await page.evaluate(() => window.voiceBenchmark?.close()).catch(() => {});
      await context.close();
      await browser.close();
      browser = null;
      await save();
    }
  }
} finally {
  await browser?.close();
  for (const profile of profiles) for (const fixture of fixtures) {
    const runs = report.runs.filter(run => run.profile === profile && run.fixture_id === fixture.id && run.phase === 'measured');
    const validRuns = runs.filter(run => !run.errors?.length && Number.isFinite(run.browser_decoded_response_seconds));
    const sorted = validRuns.map(run => run.browser_decoded_response_seconds).sort((a, b) => a - b);
    const quantile = p => sorted.length ? sorted[Math.ceil(sorted.length * p) - 1] : null;
    report.summaries.push({profile, fixture_id: fixture.id, kind: fixture.kind, observations: sorted.length, attempted_observations: runs.length, failed_observations: runs.length - sorted.length,
      median_browser_decoded_seconds: quantile(0.5), diagnostic_p95_browser_decoded_seconds: quantile(0.95),
      min_browser_decoded_seconds: sorted[0] ?? null, max_browser_decoded_seconds: sorted.at(-1) ?? null,
      acceptance_claim: false});
  }
  report.finished_at = new Date().toISOString();
  await save();
  process.stdout.write(`Report: ${outputPath}\n`);
}
