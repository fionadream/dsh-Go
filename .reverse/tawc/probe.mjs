// TEMP (reverse/tawcroot-proto, never merge): startup segment probe for host_bench.py.
// Loaded with NODE_OPTIONS=--import. Only the main `dsh web` process writes; it records
// Node's process time origin (epoch ms), the HTTP listen time and the address-line time.
import fs from 'node:fs';
import http from 'node:http';
import { performance } from 'node:perf_hooks';
import { isMainThread } from 'node:worker_threads';

const OUT = '/tmp/bench/probe.jsonl';
if (isMainThread && process.argv.includes('web') && process.argv.some((v) => v.includes('@deepseek-ai/dsh'))) {
  const emit = (event) => {
    try {
      fs.appendFileSync(OUT, JSON.stringify({ event, epochMs: performance.timeOrigin + performance.now(),
        timeOrigin: performance.timeOrigin, pid: process.pid }) + '\n');
    } catch { /* never block startup */ }
  };
  emit('node-start');
  const listen = http.Server.prototype.listen;
  http.Server.prototype.listen = function (...args) {
    this.once('listening', () => emit('listen'));
    return Reflect.apply(listen, this, args);
  };
  const write = process.stdout.write;
  let seen = false;
  process.stdout.write = function (chunk, ...rest) {
    if (!seen && String(chunk).includes('dsh web: http')) { seen = true; emit('token-line'); }
    return Reflect.apply(write, this, [chunk, ...rest]);
  };
}
