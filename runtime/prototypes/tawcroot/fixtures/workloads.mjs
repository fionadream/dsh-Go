// Prototype-only fixture, explicitly copied by measure.py into one private unit.
// No user rootfs, credential, phone action, or package-manager state is accepted.
import fs from 'node:fs';
import path from 'node:path';
import cp from 'node:child_process';
import crypto from 'node:crypto';
import dns from 'node:dns/promises';
import net from 'node:net';
import { Worker } from 'node:worker_threads';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const adapter = globalThis[Symbol.for('dsh.go.direct')];
// Direct adapts commands/modules/DNS, not arbitrary fs calls or Node entry argv.
const physical = (value) => adapter ? adapter.toHost(value) : value;
const BASE = physical('/opt/dsh-prototype');
const DSH = physical('/opt/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js');
const PLUGIN = 'dsh-git-badge';
const PREFIX = 'DSH_TAWC_PROBE ';
const ns = () => process.hrtime.bigint();
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const sha = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex');
const requireGuest = createRequire(physical('/opt/dsh/package.json'));
const emit = (event) => process.stdout.write(PREFIX + JSON.stringify(event) + '\n');

function requireCheck(value, code) {
  if (!value) throw new Error(code);
}

/** One fixed short marker; a pipe write is not a promise about data chunks. */
export function waitForMarker(stream, marker, timeoutMs) {
  return new Promise((resolve) => {
    let tail = '', finished = false;
    const done = (found) => {
      if (finished) return;
      finished = true; clearTimeout(timer);
      stream.off('data', data); stream.off('end', ended); stream.off('close', ended); stream.off('error', ended);
      resolve(found);
    };
    const data = (chunk) => {
      tail += chunk.toString();
      if (tail.includes(marker)) done(true);
      else tail = tail.slice(-(marker.length - 1));
    };
    const ended = () => done(false);
    const timer = setTimeout(ended, timeoutMs);
    stream.on('data', data); stream.once('end', ended); stream.once('close', ended); stream.once('error', ended);
  });
}

/** Capture the main exit before any later check. Never include command output in reports. */
export function runCommand(label, file, args, { timeoutMs = 180000, input, ...options } = {}) {
  return new Promise((resolve) => {
    const started = ns();
    const cpu = process.cpuUsage();
    const hashes = [crypto.createHash('sha256'), crypto.createHash('sha256')];
    const counts = [0, 0], nuls = [0, 0], chunks = [[], []], captured = [0, 0];
    const child = cp.spawn(file, args, { ...options, stdio: ['pipe', 'pipe', 'pipe'] });
    let exitCode = null, signal = null, error = null, exitedAt = null;
    let timedOut = false, forced = false, finished = false, tailTimer = null, killTimer = null;
    function finish(incomplete = false) {
      if (finished) return;
      finished = true;
      clearTimeout(deadline); clearTimeout(tailTimer); clearTimeout(killTimer);
      child.stdout.destroy(); child.stderr.destroy();
      resolve({
        evidence: { label, exitCode, signal, error, timedOut, forced, streamIncomplete: incomplete,
          wallNs: Number((exitedAt ?? ns()) - started),
          fixtureParentCpuMicros: process.cpuUsage(cpu), cpuScope: 'fixture-parent-only-excludes-tracer-and-children',
          stdoutBytes: counts[0], stderrBytes: counts[1], stdoutNuls: nuls[0],
          stdoutSha256: hashes[0].digest('hex'), stderrSha256: hashes[1].digest('hex'),
          capturedTextTruncated: counts.some((count, at) => count > captured[at]) },
        stdout: Buffer.concat(chunks[0]).toString('utf8'), stderr: Buffer.concat(chunks[1]).toString('utf8'),
      });
    }
    for (const [at, stream] of [child.stdout, child.stderr].entries()) stream.on('data', (data) => {
      if (finished) return;
      hashes[at].update(data); counts[at] += data.length;
      for (const byte of data) if (byte === 0) nuls[at]++;
      const keep = data.subarray(0, Math.max(0, 262144 - captured[at]));
      if (keep.length) chunks[at].push(keep);
      captured[at] += keep.length;
    });
    const deadline = setTimeout(() => {
      timedOut = true; child.kill('SIGTERM');
      killTimer = setTimeout(() => {
        forced = true; child.kill('SIGKILL');
        tailTimer = setTimeout(() => finish(true), 2000);
      }, 1000);
    }, timeoutMs);
    child.on('error', (cause) => { error = cause.code ?? cause.name; finish(true); });
    child.on('exit', (code, reason) => {
      exitCode = code; signal = reason; exitedAt = ns();
      tailTimer = setTimeout(() => finish(true), 2000);
    });
    child.on('close', () => finish());
    child.stdin.on('error', () => {}); // EPIPE is reflected by the actual exit/output checks.
    child.stdin.end(input);
  });
}

function ok(command) {
  return command.evidence.exitCode === 0 && !command.evidence.timedOut && !command.evidence.streamIncomplete;
}

function filesDigest(directory, { skipGit = false } = {}) {
  directory = physical(directory);
  const hash = crypto.createHash('sha256');
  let files = 0, bytes = 0, links = 0;
  function visit(dir, relative = '') {
    for (const name of fs.readdirSync(dir).sort()) {
      if (skipGit && relative === '' && name === '.git') continue;
      const absolute = path.join(dir, name), rel = path.join(relative, name), stat = fs.lstatSync(absolute);
      hash.update(rel + '\0');
      if (stat.isSymbolicLink()) { links++; hash.update('L' + fs.readlinkSync(absolute) + '\0'); }
      else if (stat.isDirectory()) { hash.update('D\0'); visit(absolute, rel); }
      else if (stat.isFile()) {
        files++; bytes += stat.size; hash.update('F' + stat.size + '\0');
        const fd = fs.openSync(absolute, 'r'), buffer = Buffer.alloc(65536);
        try { let n; while ((n = fs.readSync(fd, buffer, 0, buffer.length, null)) > 0) hash.update(buffer.subarray(0, n)); }
        finally { fs.closeSync(fd); }
      }
    }
  }
  visit(directory);
  return { sha256: hash.digest('hex'), files, bytes, links };
}

export function pluginState(installed) {
  const profile = path.join(process.env.DSH_HOME, 'profiles/web');
  const manifest = JSON.parse(fs.readFileSync(path.join(profile, 'package.json'), 'utf8'));
  requireCheck(Boolean(manifest.dependencies?.[PLUGIN]) === installed, 'plugin-dependency');
  const bundles = manifest.dsh?.profile?.bundles;
  requireCheck(Array.isArray(bundles) && bundles.includes(PLUGIN) === installed, 'plugin-bundle');
  requireCheck(fs.existsSync(path.join(profile, 'node_modules', PLUGIN)) === installed, 'plugin-module');
  if (installed) requireCheck(manifest.dependencies[PLUGIN] === '0.20.1', 'plugin-version');
  return { installed, manifestReadable: true, moduleMatches: true };
}

const plugin = (verb) => runCommand('plugin-' + verb, 'node', [DSH, 'plugin', '--profile', 'web', verb,
  verb === 'add' ? PLUGIN + '@0.20.1' : PLUGIN]);

async function prepare(config, report) {
  if (config.suite === 'pnpm' && config.pnpmCache === 'warm') {
    for (const verb of ['add', 'remove']) {
      const result = await plugin(verb); report.checks.push(result.evidence);
      requireCheck(ok(result), 'pnpm-warm-' + verb); pluginState(verb === 'add');
    }
  }
  if (config.suite === 'apt') {
    const installed = await runCommand('apt-not-installed', 'dpkg-query', ['-W', '-f=${db:Status-Status}', 'hello']);
    report.checks.push(installed.evidence);
    requireCheck(installed.evidence.error === null && [0, 1].includes(installed.evidence.exitCode)
      && !installed.evidence.timedOut && installed.stdout.trim() !== 'installed', 'apt-hello-already-installed');
    const update = await runCommand('apt-update', 'apt-get', ['update']);
    report.checks.push(update.evidence); requireCheck(ok(update), 'apt-update');
    const policy = await runCommand('apt-resolve', 'apt-cache', ['policy', 'hello']);
    report.checks.push(policy.evidence); requireCheck(ok(policy), 'apt-resolve');
    const candidate = policy.stdout.match(/^\s*Candidate:\s*(\S+)$/m)?.[1];
    const version = config.aptVersion ?? candidate;
    requireCheck(typeof version === 'string' && /^[A-Za-z0-9.+:~_-]+$/.test(version) && version !== '(none)', 'apt-no-version');
    const download = await runCommand('apt-download', 'apt-get', ['--assume-yes', '--download-only', 'install', 'hello=' + version]);
    report.checks.push(download.evidence); requireCheck(ok(download), 'apt-download');
    const archive = filesDigest('/var/cache/apt/archives');
    requireCheck(fs.readdirSync(physical('/var/cache/apt/archives')).some((name) => name.endsWith('.deb')), 'apt-no-cached-archives');
    report.apt = { version, archivesSha256: archive.sha256, archiveFiles: archive.files, archiveBytes: archive.bytes,
      initialInstalled: false };
    fs.writeFileSync(BASE + '/apt-plan.json', JSON.stringify(report.apt));
  }
  if (config.suite === 'git') {
    const repo = BASE + '/git-input/repo';
    const commit = await runCommand('git-commit', 'git', ['-C', repo, 'rev-parse', 'HEAD']);
    const tracked = await runCommand('git-tracked', 'git', ['-C', repo, 'ls-files', '-z']);
    report.checks.push(commit.evidence, tracked.evidence);
    requireCheck(ok(commit) && commit.stdout.trim() === config.gitCommit && ok(tracked), 'git-fixture-identity');
    // ls-files already reads the index; the timed pass is the first status, not a cold index.
    report.git = { commit: config.gitCommit, trackedCount: tracked.evidence.stdoutNuls, trackedBytes: tracked.evidence.stdoutBytes,
      trackedListSha256: tracked.evidence.stdoutSha256, tree: filesDigest(repo, { skipGit: true }) };
    fs.writeFileSync(BASE + '/git-plan.json', JSON.stringify(report.git));
  }
}

export async function wasmProbe() {
  // (func (export "add") (param i32 i32) (result i32) local.get 0 local.get 1 i32.add)
  const bytes = [0,97,115,109,1,0,0,0,1,7,1,96,2,127,127,1,127,3,2,1,0,
    7,7,1,3,97,100,100,0,0,10,9,1,7,0,32,0,32,1,106,11];
  const { instance } = await WebAssembly.instantiate(Uint8Array.from(bytes));
  const result = instance.exports.add(37, 5);
  requireCheck(result === 42, 'wasm-execution');
  return { exportedFunction: 'i32-add', arguments: [37, 5], result, calls: 1 };
}

async function nodeProbe(report) {
  const root = BASE + '/node-files'; fs.mkdirSync(root);
  const data = Buffer.from('prototype-link-content');
  fs.writeFileSync(root + '/a', data); fs.linkSync(root + '/a', root + '/b');
  fs.symlinkSync('a', root + '/s'); fs.renameSync(root + '/b', root + '/c');
  requireCheck(fs.readFileSync(root + '/c').equals(data) && fs.readFileSync(root + '/s').equals(data), 'link-content');
  fs.appendFileSync(root + '/a', '!');
  report.links = { linkedWriteVisible: fs.readFileSync(root + '/c').equals(fs.readFileSync(root + '/a')),
    nlink: fs.statSync(root + '/a').nlink, symlinkReadable: true };
  // Report copy-vs-shared-inode semantics; accepting that tradeoff remains an owner decision.
  const wasm = await wasmProbe();
  const workerValue = await new Promise((resolve, reject) => {
    const worker = new Worker("const{parentPort}=require('node:worker_threads'); require('node:dns').lookup('localhost',{all:true},(e,a)=>{if(e)throw e;parentPort.postMessage(a.length>0)})", { eval: true });
    const timer = setTimeout(() => { worker.terminate(); reject(new Error('worker-timeout')); }, 10000);
    worker.once('message', (value) => { clearTimeout(timer); resolve(value); });
    worker.once('error', (error) => { clearTimeout(timer); reject(error); });
  });
  requireCheck(workerValue && (await dns.lookup('localhost', { all: true })).length > 0, 'worker-dns');
  const sharp = requireGuest('sharp');
  requireCheck((await sharp({ create: { width: 2, height: 2, channels: 3, background: '#123456' } }).png().toBuffer()).length > 0, 'native-addon');
  const random = Buffer.alloc(16), fd = fs.openSync(physical('/dev/random'), 'r');
  try { requireCheck(fs.readSync(fd, random) === 16, 'random-device'); } finally { fs.closeSync(fd); }
  const pipe = await runCommand('child-own-stdio', '/bin/sh', ['-c',
    'read x </dev/stdin; printf "out:%s" "$x" >/dev/stdout; printf err >/dev/stderr; cat /dev/fd/0'], { input: 'probe-input\nfd-tail\n' });
  report.checks.push(pipe.evidence);
  requireCheck(ok(pipe) && pipe.stdout === 'out:probe-inputfd-tail\n' && pipe.stderr === 'err', 'file-bind-pipe');
  const address = '\0dsh-prototype-' + crypto.randomBytes(12).toString('hex');
  const token = crypto.randomBytes(24);
  await new Promise((resolve, reject) => {
    const server = net.createServer({ allowHalfOpen: true }, (socket) => {
      const chunks = []; socket.on('data', (part) => chunks.push(part));
      socket.on('end', () => socket.end(Buffer.concat(chunks).equals(token) ? 'ok' : 'bad'));
    });
    const timer = setTimeout(() => { server.close(); reject(new Error('abstract-socket-timeout')); }, 10000);
    server.on('error', (error) => { clearTimeout(timer); reject(error); });
    server.listen(address, () => {
      const client = net.createConnection(address); const received = [];
      client.on('error', (error) => { clearTimeout(timer); server.close(); reject(error); });
      client.on('connect', () => client.end(token)); client.on('data', (part) => received.push(part));
      client.on('end', () => { clearTimeout(timer); server.close();
        Buffer.concat(received).toString() === 'ok' ? resolve() : reject(new Error('abstract-socket-auth')); });
    });
  });
  report.node = { worker: true, wasm, sharp: true, localhostMainAndWorker: true,
    random: true, childOwnStdio: true, abstractSocketSyntheticToken: true,
    devRandomIsSymlink: fs.lstatSync(physical('/dev/random')).isSymbolicLink(),
    untested: ['DNS-network-change', 'native-addon-universe', 'vfork-long-duration'] };
}

async function ptyProbe(report) {
  const pty = requireGuest('node-pty');
  const options = { name: 'xterm-256color', cols: 80, rows: 24, cwd: process.env.HOME, env: process.env };
  const adapter = globalThis[Symbol.for('dsh.go.direct')];
  const plan = adapter ? adapter.command('/bin/bash', ['--noprofile', '--norc', '-i'], options)
    : { file: '/bin/bash', args: ['--noprofile', '--norc', '-i'], options };
  const terminal = pty.spawn(plan.file, plan.args, plan.options);
  let transcript = '', exit = null;
  terminal.onData((text) => { transcript = (transcript + text).slice(-65536); });
  terminal.onExit((event) => { exit = event; });
  async function until(pattern) {
    const deadline = Date.now() + 10000;
    while (!pattern.test(transcript)) {
      requireCheck(Date.now() < deadline && exit === null, 'pty-output-timeout'); await sleep(25);
    }
  }
  try {
    terminal.write("printf '\\nPROBE_PTY_READY\\n'\r"); await until(/\r?\nPROBE_PTY_READY\r?\n/);
    terminal.resize(100, 40); terminal.write('stty size\r'); await until(/\r?\n40 100\r?\n/);
    terminal.write("printf '\\nPROBE_FOREGROUND\\n'; sleep 30\r"); await until(/\r?\nPROBE_FOREGROUND\r?\n/);
    await sleep(250); terminal.write('\x03'); terminal.write("printf '\\nPROBE_INTERRUPT_OK\\n'\r");
    await until(/\r?\nPROBE_INTERRUPT_OK\r?\n/);
    terminal.write("sleep 0.1 & wait; printf '\\nPROBE_JOB_OK\\n'\r"); await until(/\r?\nPROBE_JOB_OK\r?\n/);
    terminal.write('\x04');
    const deadline = Date.now() + 10000;
    while (exit === null && Date.now() < deadline) await sleep(25);
    requireCheck(exit?.exitCode === 0, 'pty-eof-exit');
    report.pty = { sessionAndForegroundViaInteractiveShell: true, resize: true, ctrlC: true, ctrlD: true,
      backgroundJob: true, exitCode: exit.exitCode, transcriptSha256: sha(transcript),
      shellExecutor: adapter ? 'proot-via-existing-direct-adapter' : 'selected-runtime' };
  } finally { if (exit === null) terminal.kill('SIGKILL'); }
  // A second PTY tests explicit destruction independently of EOF.
  const destroyPlan = adapter ? adapter.command('/bin/sh', ['-c', 'sleep 30'], options)
    : { file: '/bin/sh', args: ['-c', 'sleep 30'], options };
  const other = pty.spawn(destroyPlan.file, destroyPlan.args, destroyPlan.options);
  let destroyed = false, destroyExit = null;
  other.onExit((event) => { destroyed = true; destroyExit = event; });
  await sleep(250);
  requireCheck(!destroyed, 'pty-exited-before-destroy');
  other.kill('SIGTERM');
  const deadline = Date.now() + 3000;
  while (!destroyed && Date.now() < deadline) await sleep(25);
  if (!destroyed) other.kill('SIGKILL');
  requireCheck(destroyed, 'pty-destroy-required-kill');
  report.pty.destroyTerm = true;
  report.pty.destroyExit = destroyExit;
}

export async function restartWeb(config) {
  const args = [...config.webArguments];
  for (let at = 0; at < args.length - 1; at++) if (args[at] === '--patch') args[at + 1] = physical(args[at + 1]);
  const child = cp.spawn('node', [DSH, ...args], { stdio: ['ignore', 'pipe', 'pipe'] });
  let token = false, connected = false, exitCode = null, signal = null, ended = false, forced = false, spawnError = false;
  let stopRequested = false, termSent = false, exitObservedBeforeStop = false;
  let tail = '';
  child.stdout.on('data', (data) => {
    tail = (tail + data.toString()).slice(-4096);
    if (/https?:\/\/[^\s'"]*[?&#]token=[A-Za-z0-9._~%-]+/.test(tail)) { token = true; tail = ''; }
  });
  child.stderr.resume();
  child.on('error', () => { spawnError = true; ended = true; });
  child.on('exit', (value, reason) => {
    exitCode = value; signal = reason; ended = true; exitObservedBeforeStop = !stopRequested;
  });
  const port = Number(config.webArguments[config.webArguments.indexOf('--port') + 1]);
  const deadline = Date.now() + 90000;
  try {
    while (!connected && !ended && Date.now() < deadline) {
      if (token) connected = await new Promise((resolve) => {
        const socket = net.createConnection({ host: '127.0.0.1', port });
        socket.setTimeout(250);
        socket.once('connect', () => { socket.destroy(); resolve(true); });
        socket.once('error', () => { socket.destroy(); resolve(false); });
        socket.once('timeout', () => { socket.destroy(); resolve(false); });
      });
      if (!connected) await sleep(50);
    }
  } finally {
    if (!ended) { stopRequested = true; termSent = child.kill('SIGTERM'); }
    const stopDeadline = Date.now() + 3000;
    while (!ended && Date.now() < stopDeadline) await sleep(25);
    if (!ended) { forced = true; child.kill('SIGKILL'); }
    child.stdout.destroy(); child.stderr.destroy();
  }
  const normalStop = ended && stopRequested && termSent && !exitObservedBeforeStop
    && ((exitCode === 0 && signal === null) || (exitCode === null && signal === 'SIGTERM'));
  return { status: token && connected && !spawnError && !forced && normalStop ? 'PASS' : 'FAILED',
    token, port: connected, forced, exitCode, signal, spawnError, stopRequested, termSent, exitObservedBeforeStop };
}

async function run(config, report) {
  if (config.suite === 'node') return nodeProbe(report);
  if (config.suite === 'pty') return ptyProbe(report);
  if (config.suite === 'stop') {
    const fifo = BASE + '/blocked.fifo';
    const mkfifo = await runCommand('stop-mkfifo', 'mkfifo', [fifo]);
    report.checks.push(mkfifo.evidence); requireCheck(ok(mkfifo), 'mkfifo');
    const child = cp.spawn('/bin/sh', ['-c', 'printf PROBE_FIFO_ATTEMPT; exec cat "$1"', 'probe', fifo], { stdio: ['ignore', 'pipe', 'pipe'] });
    child.stderr.resume();
    child.once('error', () => child.stdout.destroy());
    const ready = await waitForMarker(child.stdout, 'PROBE_FIFO_ATTEMPT', 5000);
    requireCheck(ready, 'fifo-child-not-started');
    await sleep(500);
    emit({ event: 'stop-ready', fifoOpenAttempt: true, blockedSyscallProven: false });
    // App sends TERM to identities while this Node and its FIFO child remain alive.
    await new Promise(() => { setInterval(() => {}, 1000); });
  }
  if (config.suite === 'pnpm') {
    const version = await runCommand('pnpm-version', 'pnpm', ['--version']); report.checks.push(version.evidence);
    requireCheck(ok(version), 'pnpm-version'); report.pnpmVersion = version.stdout.trim();
    const store = await runCommand('pnpm-store', 'pnpm', ['store', 'path']); report.checks.push(store.evidence);
    requireCheck(ok(store), 'pnpm-store');
    const storePath = physical(store.stdout.trim());
    const before = fs.existsSync(storePath) ? filesDigest(storePath) : { files: 0, bytes: 0 };
    const add = await plugin('add'); report.main = add.evidence;
    requireCheck(ok(add), 'plugin-add'); report.added = pluginState(true);
    const remove = await plugin('remove'); report.checks.push(remove.evidence);
    requireCheck(ok(remove), 'plugin-remove'); report.removed = pluginState(false);
    report.restart = await restartWeb(config);
    requireCheck(report.restart.status === 'PASS', 'plugin-web-restart');
    const after = fs.existsSync(storePath) ? filesDigest(storePath) : { files: 0, bytes: 0 };
    report.store = { before, after, bytesDelta: after.bytes - before.bytes };
    report.network = 'registry-online; closure not proven offline';
  }
  if (config.suite === 'apt') {
    const plan = JSON.parse(fs.readFileSync(BASE + '/apt-plan.json', 'utf8'));
    const install = await runCommand('apt-install', 'apt-get', ['--assume-yes', '--no-download', 'install', 'hello=' + plan.version]);
    report.main = install.evidence; requireCheck(ok(install), 'apt-install');
    const audit = await runCommand('dpkg-audit', 'dpkg', ['--audit']);
    const query = await runCommand('dpkg-query', 'dpkg-query', ['-W', '-f=${Version} ${db:Status-Status}', 'hello']);
    const hello = await runCommand('hello', 'hello', []);
    report.checks.push(audit.evidence, query.evidence, hello.evidence);
    requireCheck(ok(audit) && audit.evidence.stdoutBytes === 0 && ok(query)
      && query.stdout.trim() === plan.version + ' installed' && ok(hello) && hello.evidence.stdoutBytes > 0, 'apt-content');
    report.apt = { ...plan, auditEmpty: true, installedExactVersion: true, helloExecuted: true };
    const remove = await runCommand('apt-remove', 'apt-get', ['--assume-yes', 'remove', 'hello']);
    report.checks.push(remove.evidence); requireCheck(ok(remove), 'apt-remove');
    const after = await runCommand('dpkg-audit-after', 'dpkg', ['--audit']);
    report.checks.push(after.evidence); requireCheck(ok(after) && after.evidence.stdoutBytes === 0, 'apt-audit-after');
    report.apt.laterPackageManagerAcquiredLocks = true;
    report.apt.interruptedTransactionRecovery = 'not-covered-by-this-single-package-scenario';
  }
  if (config.suite === 'git') {
    const repo = BASE + '/git-input/repo', plan = JSON.parse(fs.readFileSync(BASE + '/git-plan.json', 'utf8'));
    const args = ['-C', repo, 'status', '--porcelain=v1', '--untracked-files=all'];
    const first = await runCommand('git-status-first', 'git', args); report.main = first.evidence;
    requireCheck(ok(first), 'git-status-first');
    const hot = await runCommand('git-status-hot', 'git', args);
    const attribution = await runCommand('git-status-no-caches', 'git', ['-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false', ...args]);
    const fsck = await runCommand('git-fsck', 'git', ['-C', repo, 'fsck', '--full']);
    const untracked = await runCommand('git-untracked', 'git', ['-C', repo, 'ls-files', '--others', '--exclude-standard', '-z']);
    report.checks.push(hot.evidence, attribution.evidence, fsck.evidence, untracked.evidence);
    const after = filesDigest(repo, { skipGit: true });
    requireCheck([hot, attribution, fsck, untracked].every(ok) && first.evidence.stdoutSha256 === hot.evidence.stdoutSha256
      && first.evidence.stdoutSha256 === attribution.evidence.stdoutSha256 && after.sha256 === plan.tree.sha256, 'git-consistency');
    report.git = { ...plan, after, hot: hot.evidence, attribution: attribution.evidence,
      outputDigestMatches: true, worktreeDigestUnchanged: true, firstStatusPass: true,
      normalConfig: true, untrackedCount: untracked.evidence.stdoutNuls, dirty: first.evidence.stdoutBytes > 0 };
  }
}

async function main() {
  const action = process.argv[2];
  const config = JSON.parse(fs.readFileSync(BASE + '/config.json', 'utf8'));
  const report = { event: 'result', schema: 1, suite: config.suite, action, status: 'PASS', checks: [],
    nodeVersion: process.version, nodeExecutor: globalThis[Symbol.for('dsh.go.direct')] ? 'direct-node' : config.mode,
    childExecutor: config.mode === 'direct' ? 'node/pnpm direct; other commands use existing PRoot wrapper' : config.mode,
    failure: null };
  const started = ns();
  try {
    requireCheck(action === 'prepare' || action === 'run', 'action');
    if (action === 'prepare') await prepare(config, report); else await run(config, report);
  } catch (error) {
    report.status = 'FAILED';
    // Only our stable error labels or an exception type; no paths/outputs/credentials.
    report.failure = { type: error.name, code: /^[a-z0-9-]+$/.test(error.message) ? error.message : (error.code ?? 'exception') };
  }
  report.totalWallNs = Number(ns() - started);
  emit(report);
  process.exitCode = report.status === 'PASS' ? 0 : 1;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) await main();
