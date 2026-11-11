package io.github.ccch1mneyyy.dsh_app.runtime.probe

import android.app.Activity
import android.content.pm.ApplicationInfo
import android.os.Build
import android.os.Bundle
import android.os.Process as AndroidProcess
import android.os.SystemClock
import android.system.ErrnoException
import android.system.Os
import android.system.OsConstants
import android.view.WindowManager
import android.widget.TextView
import io.github.ccch1mneyyy.dsh_app.runtime.DirectLaunch
import io.github.ccch1mneyyy.dsh_app.runtime.DnsConfigOutcome
import io.github.ccch1mneyyy.dsh_app.runtime.GuestSetup
import io.github.ccch1mneyyy.dsh_app.runtime.RuntimeManifest
import io.github.ccch1mneyyy.dsh_app.runtime.RuntimeProcessOwnership
import io.github.ccch1mneyyy.dsh_app.runtime.RuntimeProcessReader
import io.github.ccch1mneyyy.dsh_app.runtime.RuntimeProcessRecord
import io.github.ccch1mneyyy.dsh_app.runtime.RuntimeProcessSnapshot
import io.github.ccch1mneyyy.dsh_app.runtime.TarExtractor
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.nio.file.FileVisitResult
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.NoSuchFileException
import java.nio.file.Path
import java.nio.file.SimpleFileVisitor
import java.nio.file.StandardCopyOption
import java.nio.file.attribute.BasicFileAttributes
import java.security.DigestInputStream
import java.security.MessageDigest
import java.util.TimeZone
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference
import kotlin.concurrent.thread
import org.json.JSONArray
import org.json.JSONObject

/** Opt-in development probe. No calls into the product runtime lifecycle. */
class TawcrootPrototypeActivity : Activity() {
    @Volatile private var cancelled = false
    @Volatile private var completed = false
    private var ownsWorker = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContentView(TextView(this).apply { text = "DSH isolated runtime prototype\nResults are read by measure.py." })
        val session = intent.getStringExtra("session") ?: ""
        val action = intent.getStringExtra("action") ?: ""
        if (!SESSION.matches(session) || action !in listOf("prepare", "run", "cleanup") || !BUSY.compareAndSet(false, true)) {
            finish(); return
        }
        ownsWorker = true
        val requested = now()
        thread(name = "tawc-prototype") {
            try { execute(session, action, requested) }
            finally {
                completed = true
                BUSY.set(false)
                runOnUiThread { finish() }
            }
        }
    }

    override fun onDestroy() {
        if (ownsWorker && !completed) cancelled = true
        super.onDestroy()
    }

    private fun unit(session: String): File {
        val base = File(filesDir, "tawcroot-prototype")
        val dir = File(base, session)
        check(Files.isDirectory(base.toPath(), LinkOption.NOFOLLOW_LINKS) &&
            Files.isDirectory(dir.toPath(), LinkOption.NOFOLLOW_LINKS) &&
            dir.canonicalFile.parentFile == base.canonicalFile) { "session-directory" }
        return dir.canonicalFile
    }

    private fun execute(session: String, action: String, requested: Long) {
        var dir: File? = null
        var stage = "session"
        var child: Process? = null
        var tracker: Tracker? = null
        var stopRequired = false
        var pumps: List<Thread> = emptyList()
        val token = AtomicLong(0)
        val stopReady = AtomicBoolean(false)
        val fixture = AtomicReference<JSONObject?>(null)
        val outputFailed = AtomicBoolean(false)
        val clocks = obj("domain" to "android.elapsedRealtimeNanos", "processStartNs" to AndroidProcess.getStartElapsedRealtime() * 1_000_000L,
            "requestedNs" to requested, "prepareStartNs" to null, "prepareEndNs" to null,
            "spawnBeforeNs" to null, "spawnAfterNs" to null, "tokenNs" to null, "portReadyNs" to null,
            "stopStartNs" to null, "stopEndNs" to null, "finishedNs" to null)
        val result = obj("schema" to 1, "prototype" to true, "session" to session, "phase" to action,
            "status" to "FAILED", "failureStage" to stage, "clocks" to clocks,
            "readiness" to obj("token" to false, "port" to false), "exitCode" to null,
            "stop" to emptyStop(), "workload" to null)
        try {
            dir = unit(session)
            val configFile = File(dir, "config.json")
            check(Files.isRegularFile(configFile.toPath(), LinkOption.NOFOLLOW_LINKS)) { "config-file" }
            val config = JSONObject(configFile.readText())
            check(config.getInt("schema") == 1 && config.getString("session") == session) { "config-session" }
            val mode = config.getString("mode")
            val suite = config.getString("suite")
            check(mode in listOf("proot", "direct", "tawcroot") && suite in listOf("startup", "node", "pty", "stop", "pnpm", "apt", "git")) { "config-choice" }
            for (key in listOf("runId", "round", "mode", "suite")) result.put(key, config.get(key))
            result.put("actualExecutor", JSONObject.NULL)
            result.put("executionClassification", "outer launcher exec; fixture reports Node and existing adapter child plans; not syscall tracing")
            val root = File(dir, "rootfs")
            val marker = File(dir, ".owned")
            val expectedMarker = "DSH_TAWC_PROTOTYPE_V1\n$session\n"
            if (marker.exists()) {
                check(marker.isFile && marker.readText() == expectedMarker) { "session-marker" }
                // A recreated Activity must account for identities captured before it died.
                stopRequired = true
                tracker = Tracker(root)
            }
            if (action == "cleanup") {
                stage = "cleanup-ownership"
                check(marker.isFile && marker.readText() == expectedMarker) { "session-marker" }
                val stop = checkNotNull(tracker).stop(null)
                result.put("stop", stop)
                check(stop.getBoolean("clean")) { "cleanup-processes-unconfirmed" }
                deleteTree(dir)
                result.put("status", "PASS").put("failureStage", JSONObject.NULL)
                return
            }
            if (action == "prepare") {
                check(!marker.exists() && !root.exists()) { "session-reuse" }
                marker.writeText(expectedMarker)
            }
            stage = "preflight"
            check(applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0) { "debug-only" }
            check(Build.VERSION.SDK_INT >= 29) { "api-below-29" }
            // Metadata only: never open an existing product runtime or reset/carry tree.
            check(listOf("runtime", "runtime.reset", "runtime-keep").none { File(filesDir, it).exists() }) { "existing-product-data" }
            check(!File(dir, "cancel").exists()) { "cancelled" }
            val timeout = config.getLong("timeoutSeconds")
            check(timeout in 10..3600) { "timeout-range" }
            check(hash(File(dir, "workloads.mjs")) == config.getString("fixtureSha256")) { "fixture-sha256" }
            val manifest = RuntimeManifest.load(this)
            clocks.put("prepareStartNs", now())
            result.put("identity", identity(config, manifest))
            if (action == "prepare") {
                stage = "unpack"
                check(root.mkdir()) { "root-create" }
                for (archive in manifest.archives) {
                    check(!cancelled && !File(dir, "cancel").exists()) { "cancelled" }
                    check(archive.sha256 != null && archive.bytes > 0) { "archive-identity" }
                    var read = 0L
                    val sha = MessageDigest.getInstance("SHA-256")
                    assets.open("runtime/${archive.asset}").use { raw ->
                        DigestInputStream(raw, sha).use { input ->
                            TarExtractor(root) { read = it }.extract(input, archive.asset)
                        }
                    }
                    check(read == archive.bytes && hex(sha.digest()) == archive.sha256) { "archive-digest" }
                }
                for (name in listOf("root", "tmp", "dev", "proc", "sys", "var/lib/dsh-app/empty", "var/lib/dsh-app/shm")) {
                    check(File(root, name).isDirectory || File(root, name).mkdirs()) { "guest-directory" }
                }
                val target = File(root, "opt/dsh-prototype")
                check(target.mkdir()) { "fixture-directory" }
                File(dir, "workloads.mjs").copyTo(File(target, "workloads.mjs"))
                if (suite == "git") {
                    check(hash(File(dir, "git.tar")) == config.getString("gitSha256")) { "git-archive-sha256" }
                    val input = File(target, "git-input").apply { check(mkdir()) }
                    File(dir, "git.tar").inputStream().use { TarExtractor(input) {}.extract(it, "git.tar") }
                }
                val dns = GuestSetup.writeResolvConf(this, root)
                result.put("dns", obj("outcome" to dns.outcome.wire, "networkRead" to dns.networkRead.wire,
                    "stage" to dns.stage.wire, "error" to dns.error.wire, "errno" to dns.errno))
                check(dns.outcome != DnsConfigOutcome.FAILED) { "dns-write" }
            } else {
                check(marker.isFile && marker.readText() == expectedMarker) { "session-marker" }
                val prepared = JSONObject(File(dir, "prepare.json").readText())
                check(prepared.getString("status") == "PASS" && prepared.getJSONObject("stop").getBoolean("clean")) { "prepare-not-passed" }
                check(!File(dir, "run.json").exists()) { "run-reuse" }
                check(File(dir, ".run-started").createNewFile()) { "run-reuse" }
            }
            stage = "plan"
            check(!cancelled && !File(dir, "cancel").exists()) { "cancelled" }
            val port = ServerSocket(0, 1, InetAddress.getByName("127.0.0.1")).use { it.localPort }
            val spec = prepared(manifest, dir, root, port, mode)
            val guestConfig = JSONObject(config.toString()).put("webArguments", JSONArray(DirectLaunch.webArguments(spec.argv.last())))
            File(root, "opt/dsh-prototype/config.json").writeText(guestConfig.toString())
            val needsGuest = action == "run" || suite in listOf("apt", "git") || (suite == "pnpm" && config.getString("pnpmCache") == "warm")
            val plan = if (needsGuest) launchPlan(spec, root, manifest.abi, mode, action, suite, result) else null
            val selectedLauncher = when (mode) {
                "direct" -> File(root, DirectLaunch.loaderPath(manifest.abi))
                "proot" -> File(applicationInfo.nativeLibraryDir, "libproot.so")
                else -> File(applicationInfo.nativeLibraryDir, "libtawcroot.so")
            }
            result.getJSONObject("identity").put("selectedLauncher", obj("name" to selectedLauncher.name,
                "bytes" to selectedLauncher.length(), "sha256" to hash(selectedLauncher)))
            result.put("instrumentation", obj("ownershipScanIntervalMs" to 250, "readinessPollMs" to 50,
                "connectTimeoutMs" to 250, "includesProductionControllerOrUI" to false))
            result.put("cold", obj("process" to (action == "run"), "osPageCache" to "retained",
                "fixture" to "private fresh APK copy", "pnpmStore" to config.getString("pnpmCache"),
                "wrapperPreparation" to if (action == "run" && suite !in listOf("apt", "git") &&
                    !(suite == "pnpm" && config.getString("pnpmCache") == "warm")) "first" else "setup may have prepared wrappers"))
            result.put("resources", obj("before" to diskUsage(dir), "storeBefore" to diskUsage(File(dir, "tawcroot"))))
            clocks.put("prepareEndNs", now())
            if (plan != null) {
                stage = "spawn"
                stopRequired = true
                if (tracker == null) tracker = Tracker(root)
                val builder = ProcessBuilder(plan.argv).directory(plan.cwd)
                builder.environment().apply { clear(); putAll(plan.environment) }
                clocks.put("spawnBeforeNs", now())
                child = builder.start()
                clocks.put("spawnAfterNs", now())
                result.put("actualExecutor", if (mode == "direct") "direct-glibc-loader" else mode)
                child.outputStream.close()
                pumps = listOf(pump(child.inputStream, true, token, stopReady, fixture, outputFailed),
                    pump(child.errorStream, false, token, stopReady, fixture, outputFailed))
                stage = "guest"
                val deadline = now() + timeout * 1_000_000_000L
                var nextScan = 0L
                while (child.isAlive) {
                    check(!cancelled && !File(dir, "cancel").exists()) { "cancelled" }
                    check(now() < deadline) { "guest-deadline" }
                    if (now() >= nextScan) { tracker.capture(); nextScan = now() + 250_000_000L }
                    if (action == "run" && suite == "startup" && token.get() != 0L && connect(port)) {
                        clocks.put("portReadyNs", now()); break
                    }
                    if (action == "run" && suite == "stop" && stopReady.get()) break
                    Thread.sleep(50)
                }
                if (!child.isAlive) result.put("exitCode", child.exitValue())
                if (suite != "startup" && suite != "stop") pumps.forEach { it.join(2000) }
                fixture.get()?.let { result.put("workload", it) }
                check(!outputFailed.get()) { "guest-output" }
                when {
                    action == "run" && suite == "startup" -> check(token.get() != 0L && !clocks.isNull("portReadyNs")) { "startup-not-ready" }
                    action == "run" && suite == "stop" -> check(stopReady.get()) { "stop-not-ready" }
                    else -> check(!child.isAlive && child.exitValue() == 0 && fixture.get()?.optString("status") == "PASS") { "workload-failed" }
                }
            }
            result.put("status", "PASS").put("failureStage", JSONObject.NULL)
        } catch (error: Exception) {
            result.put("status", "FAILED").put("failureStage", stage)
            result.put("error", obj("type" to error.javaClass.simpleName,
                "code" to error.message?.takeIf { it.matches(Regex("[a-z0-9-]+")) },
                "errno" to (error as? ErrnoException)?.errno))
        } finally {
            if (action != "cleanup") {
                val exitedBeforeStop = child?.let { !it.isAlive }
                result.put("exitObservedBeforeStop", exitedBeforeStop ?: JSONObject.NULL)
                if (action == "run" && result.optString("suite") in listOf("startup", "stop") && exitedBeforeStop == true) {
                    if (result.optString("status") == "PASS") result.put("failureStage", "guest-exited-before-stop")
                    result.put("status", "FAILED")
                }
                clocks.put("stopStartNs", now())
                val stopped = try { tracker?.stop(child) ?: emptyStop().put("clean", !stopRequired) }
                catch (error: Exception) {
                    emptyStop().put("clean", false).put("error", error.javaClass.simpleName)
                }
                clocks.put("stopEndNs", now())
                result.put("stop", stopped)
                if (!stopped.getBoolean("clean") || stopped.getInt("killSent") != 0 || stopped.getBoolean("parentForced")) {
                    if (result.optString("status") == "PASS") result.put("failureStage", "stop")
                    result.put("status", "FAILED")
                }
                pumps.forEach { it.join(2000) }
                if (pumps.any { it.isAlive } || outputFailed.get()) {
                    if (result.optString("status") == "PASS") result.put("failureStage", "output-drain")
                    result.put("status", "FAILED")
                }
                child?.let {
                    if (!it.isAlive && result.isNull("exitCode")) result.put("exitCode", it.exitValue())
                    for (stream in listOf(it.inputStream, it.errorStream)) runCatching { stream.close() }.onFailure {
                        if (result.optString("status") == "PASS") result.put("failureStage", "output-drain")
                        result.put("status", "FAILED")
                    }
                }
                fixture.get()?.let { result.put("workload", it) }
                clocks.put("tokenNs", token.get().takeIf { it != 0L } ?: JSONObject.NULL)
                result.put("readiness", obj("token" to (token.get() != 0L), "port" to !clocks.isNull("portReadyNs")))
                dir?.takeIf { File(it, "rootfs").isDirectory }?.let {
                    runCatching {
                        result.optJSONObject("resources")?.put("after", diskUsage(it))?.put("storeAfter", diskUsage(File(it, "tawcroot")))
                    }.onFailure {
                        if (result.optString("status") == "PASS") result.put("failureStage", "resource-measurement")
                        result.put("status", "FAILED").put("resourceMeasurementFailed", true)
                    }
                }
            }
            clocks.put("finishedNs", now())
            val output = if (action == "cleanup") File(filesDir, "tawcroot-prototype-cleanup/$session.json")
                else dir?.let { File(it, "$action.json") }
            if (output != null) writeResult(output, result)
        }
    }

    private fun identity(config: JSONObject, manifest: RuntimeManifest): JSONObject {
        check(manifest.abi in listOf("arm64-v8a", "x86_64") && manifest.abi in Build.SUPPORTED_ABIS && AndroidProcess.is64Bit()) { "runtime-abi" }
        val runtimeBytes = assets.open("runtime/manifest.json").use { it.readBytes() }
        val buildBytes = assets.open("tawcroot-prototype/manifest.json").use { it.readBytes() }
        val build = JSONObject(buildBytes.toString(Charsets.UTF_8))
        val source = build.getJSONObject("source")
        check(build.getInt("schema") == 1 && source.getString("repo") == "wmww/tawc" &&
            source.getString("commit") == COMMIT && source.getString("archiveSha256") == ARCHIVE_SHA256 &&
            build.getString("ndkVersion") == "28.2.13676358") { "tawc-source" }
        val artifacts = build.getJSONArray("artifacts")
        val matches = (0 until artifacts.length()).map { artifacts.getJSONObject(it) }.filter { it.getString("abi") == manifest.abi }
        check(matches.size == 1) { "tawc-artifact" }
        val artifact = matches.single()
        check(artifact.getString("path") == "jniLibs/${manifest.abi}/libtawcroot.so") { "tawc-artifact-path" }
        val binaries = JSONObject()
        val native = File(applicationInfo.nativeLibraryDir)
        fun verify(name: String, record: JSONObject) {
            val file = File(native, name)
            val digest = hash(file)
            check(file.length() == record.getLong("bytes") && digest == record.getString("sha256")) { "native-digest" }
            binaries.put(name, obj("bytes" to file.length(), "sha256" to digest))
        }
        verify("libtawcroot.so", artifact)
        val jni = manifest.raw.getJSONObject("jniLibs")
        for (name in listOf("libproot.so", "libproot-loader.so", "libtalloc.so", "libandroid-shmem.so")) verify(name, jni.getJSONObject(name))
        val values = obj("abi" to manifest.abi, "api" to Build.VERSION.SDK_INT, "actualTarget" to applicationInfo.targetSdkVersion,
            "kernel" to Os.uname().release, "pageSize" to Os.sysconf(OsConstants._SC_PAGESIZE), "fingerprint" to Build.FINGERPRINT,
            "uid" to AndroidProcess.myUid(), "selinux" to File("/proc/self/attr/current").readText().trim().trimEnd('\u0000'),
            "apkSha256" to hash(File(applicationInfo.sourceDir)), "runtimeManifestSha256" to hex(MessageDigest.getInstance("SHA-256").digest(runtimeBytes)),
            "tawcManifestSha256" to hex(MessageDigest.getInstance("SHA-256").digest(buildBytes)), "runtimeVersion" to manifest.runtimeVersion,
            "nodeVersion" to manifest.nodeVersion, "dshVersion" to manifest.dshVersion, "nativeBinaries" to binaries,
            "tawcSource" to source, "ndkVersion" to build.getString("ndkVersion"), "tawcElf" to artifact.getJSONObject("elf"))
        for (key in listOf("apkSha256", "runtimeManifestSha256", "tawcManifestSha256", "abi")) {
            check(values.getString(key) == config.getString(key)) { "installed-identity" }
        }
        return values
    }

    /** Fixed manifest plus controlled probe overrides, never the user's preferences/env. */
    private fun prepared(manifest: RuntimeManifest, dir: File, root: File, port: Int, mode: String): DirectLaunch.Plan {
        val tmp = File(dir, "proot-tmp").apply { check(isDirectory || mkdir()) }
        val storage = File(dir, "storage").apply { check(isDirectory || mkdir()) }
        val variables = mapOf("{NATIVE_LIB_DIR}" to applicationInfo.nativeLibraryDir, "{ROOTFS}" to root.canonicalPath,
            "{TMP}" to tmp.path, "{FILES_DIR}" to dir.path, "{PORT}" to port.toString())
        fun expand(value: String) = variables.entries.fold(value) { text, pair -> text.replace(pair.key, pair.value) }
        val guest = linkedMapOf("TZ" to TimeZone.getDefault().id, "DSH_GO_RUNTIME_ROOT" to root.canonicalPath)
        if (mode != "direct") {
            val options = manifest.launchArgv.lastOrNull { it.startsWith("NODE_OPTIONS=") }?.substringAfter('=')
            guest["NODE_OPTIONS"] = DirectLaunch.prootNodeOptions(options)
        }
        check(manifest.launchArgv.count { it == "{GUEST_ENV}" } == 1) { "guest-env-template" }
        val argv = mutableListOf<String>()
        for (arg in DirectLaunch.prootArguments(root, manifest.launchArgv, mode == "direct")) {
            when (arg) {
                "/usr/bin/env" -> argv.addAll(listOf("-b", "${storage.path}:/sdcard", "-b", "${storage.path}:/storage", arg))
                "{GUEST_ENV}" -> guest.forEach { (key, value) -> argv += "$key=$value" }
                else -> argv += expand(arg)
            }
        }
        val env = linkedMapOf("PATH" to "/system/bin:/system/xbin", "HOME" to dir.path, "TMPDIR" to tmp.path,
            "ANDROID_ROOT" to (System.getenv("ANDROID_ROOT") ?: "/system"), "ANDROID_DATA" to (System.getenv("ANDROID_DATA") ?: "/data"),
            "PROOT_TMP_DIR" to tmp.path)
        manifest.launchEnv.forEach { (key, value) -> env[key] = expand(value) }
        return DirectLaunch.Plan(argv, env, dir)
    }

    private fun launchPlan(spec: DirectLaunch.Plan, root: File, abi: String, mode: String, action: String, suite: String, result: JSONObject): DirectLaunch.Plan {
        val web = action == "run" && suite == "startup"
        if (mode == "direct") {
            // prepare accepts only the real web template. Keep its existing preload/wrappers.
            val plan = DirectLaunch.prepare(root, abi, spec.argv, spec.environment)
            if (web) return plan
            val entry = File(root, "opt/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js").path
            val at = plan.argv.indexOf(entry)
            check(at > 0) { "direct-node-entry" }
            return DirectLaunch.Plan(plan.argv.take(at) + listOf(File(root, "opt/dsh-prototype/workloads.mjs").path, action),
                plan.environment - "DSH_GO_DIRECT_MAIN", plan.cwd)
        }
        val argv = spec.argv.toMutableList()
        if (!web) {
            check(argv.takeLast(3).take(2) == listOf("/bin/sh", "-c")) { "guest-command-template" }
            argv[argv.lastIndex] = "exec /opt/node/bin/node /opt/dsh-prototype/workloads.mjs $action"
        }
        if (mode == "proot") return DirectLaunch.Plan(argv, spec.environment, spec.cwd)
        val plan = TawcrootLaunch.prepare(root.canonicalPath, applicationInfo.nativeLibraryDir, argv, spec.environment, Build.VERSION.SDK_INT, pty = false)
        result.put("tawcCaveats", JSONArray(plan.caveats))
        val expected = File(root.parentFile, "dev-view")
        check(File(plan.devView.directory) == expected && File(plan.storeDirectory) == File(root.parentFile, "tawcroot")) { "tawc-unit-layout" }
        check(!Files.isSymbolicLink(expected.toPath()) && (expected.isDirectory || expected.mkdir())) { "dev-view-directory" }
        val host = File(plan.devView.hostDirectory)
        val names = host.list() ?: error("host-dev-enumeration")
        val links = names.associateWith { File(host, it).path } + plan.devView.aliases
        for ((name, target) in links) {
            val link = File(expected, name).toPath()
            Files.deleteIfExists(link)
            Files.createSymbolicLink(link, File(target).toPath())
        }
        // The external hard-link store belongs to this same unit. No fallback on this tree.
        return DirectLaunch.Plan(plan.argv, plan.environment, File(plan.cwd))
    }

    private fun pump(input: InputStream, stdout: Boolean, token: AtomicLong, stopReady: AtomicBoolean,
                     fixture: AtomicReference<JSONObject?>, failed: AtomicBoolean): Thread =
        thread(name = "prototype-output", isDaemon = true) {
            try {
                input.bufferedReader().use { reader ->
                    while (true) {
                        val line = reader.readLine() ?: break
                        // Inspect readiness in memory; never log or persist raw guest output/token.
                        if (stdout && TOKEN.containsMatchIn(line)) token.compareAndSet(0, now())
                        if (stdout && line.startsWith(OUTPUT_PREFIX)) {
                            check(line.length <= 524288) { "fixture-result-size" }
                            val value = JSONObject(line.removePrefix(OUTPUT_PREFIX))
                            when (value.getString("event")) {
                                "result" -> fixture.set(value)
                                "stop-ready" -> { fixture.set(value); stopReady.set(true) }
                                else -> error("fixture-event")
                            }
                        }
                    }
                }
            } catch (_: Exception) { failed.set(true) }
        }

    private fun connect(port: Int): Boolean = try {
        Socket().use { it.connect(InetSocketAddress("127.0.0.1", port), 250) }
        true
    } catch (_: IOException) { false }

    private class Tracker(private val root: File) {
        private val uid = AndroidProcess.myUid()
        private val appPid = AndroidProcess.myPid()
        private val identities = File(root.parentFile, "owned-pids.json")
        private val known = mutableMapOf<Int, String>()
        private var signalErrors = 0
        private val reader = object : RuntimeProcessReader {
            override fun pids(): List<Int> = (File("/proc").list() ?: throw IOException("proc-enumeration"))
                .mapNotNull { it.toIntOrNull() }
            override fun uid(pid: Int): Int? = try { Os.stat("/proc/$pid").st_uid }
            catch (error: ErrnoException) {
                if (error.errno == OsConstants.ENOENT || error.errno == OsConstants.ESRCH) null else throw error
            }
            private fun text(pid: Int, name: String) = Files.readAllBytes(File("/proc/$pid/$name").toPath()).toString(Charsets.UTF_8)
            override fun stat(pid: Int) = text(pid, "stat")
            override fun command(pid: Int) = text(pid, "cmdline")
            override fun environment(pid: Int) = text(pid, "environ")
            override fun cwd(pid: Int) = Os.readlink("/proc/$pid/cwd")
        }

        init {
            if (identities.exists()) {
                val saved = JSONObject(identities.readText())
                for (key in saved.keys()) {
                    val pid = key.toInt()
                    val started = saved.getString(key)
                    check(pid > 0 && started.matches(Regex("[0-9]+"))) { "captured-identity" }
                    known[pid] = started
                }
            }
        }

        fun capture(): RuntimeProcessSnapshot {
            val before = known.toMap()
            val snapshot = RuntimeProcessOwnership.scan(reader, uid, appPid, known.keys)
            snapshot.retired.forEach { known.remove(it) }
            for (record in snapshot.records) if (known[record.pid]?.let { it != record.started } == true) known.remove(record.pid)
            RuntimeProcessOwnership.owned(snapshot.records, setOf(root.canonicalPath), appPid, known)
                .forEach { known[it.pid] = it.started }
            if (before != known) writeResult(identities, JSONObject(known.mapKeys { it.key.toString() }))
            return snapshot
        }

        private fun owned(snapshot: RuntimeProcessSnapshot) =
            RuntimeProcessOwnership.owned(snapshot.records, setOf(root.canonicalPath), appPid, known)

        private fun signal(record: RuntimeProcessRecord, signal: Int): Boolean = try {
            if (reader.uid(record.pid) == uid && RuntimeProcessOwnership.stat(reader.stat(record.pid))?.second == record.started) {
                Os.kill(record.pid, signal); true
            } else false
        } catch (_: NoSuchFileException) { false }
        catch (error: ErrnoException) {
            if (error.errno != OsConstants.ENOENT && error.errno != OsConstants.ESRCH) signalErrors++
            false
        } catch (_: Exception) { signalErrors++; false }

        fun stop(process: Process?): JSONObject {
            var snapshot = capture()
            val term = owned(snapshot).count { signal(it, OsConstants.SIGTERM) }
            if (process?.isAlive == true && known.isEmpty()) process.destroy()
            fun waitFor(ms: Long) {
                val deadline = now() + ms * 1_000_000L
                do {
                    snapshot = capture()
                    if (known.isEmpty() && process?.isAlive != true) break
                    Thread.sleep(25)
                } while (now() < deadline)
            }
            waitFor(2000)
            val quit = owned(snapshot).filter { it.proot }.count { signal(it, OsConstants.SIGQUIT) }
            if (quit > 0) waitFor(500)
            val killed = owned(snapshot).count { signal(it, OsConstants.SIGKILL) }
            val forced = process?.isAlive == true
            if (forced) process?.destroyForcibly()
            waitFor(2000)
            val clean = known.isEmpty() && snapshot.observed && process?.isAlive != true && signalErrors == 0
            return obj("termSent" to term, "prootQuitSent" to quit, "killSent" to killed,
                "parentForced" to forced, "remainingIdentities" to known.size,
                "observationUnavailable" to snapshot.uncertain.size, "enumerationFailed" to snapshot.enumerationFailed,
                "signalErrors" to signalErrors, "clean" to clean)
        }
    }

    private fun diskUsage(directory: File): JSONObject {
        var logical = 0L
        var allocated = 0L
        var files = 0L
        val seen = mutableSetOf<String>()
        if (directory.exists()) Files.walkFileTree(directory.toPath(), object : SimpleFileVisitor<Path>() {
            private fun count(path: Path) {
                val stat = Os.lstat(path.toString())
                if (seen.add("${stat.st_dev}:${stat.st_ino}")) {
                    allocated += stat.st_blocks * 512
                    if (OsConstants.S_ISREG(stat.st_mode)) { files++; logical += stat.st_size }
                }
            }
            override fun preVisitDirectory(dir: Path, attrs: BasicFileAttributes): FileVisitResult {
                count(dir); return FileVisitResult.CONTINUE
            }
            override fun visitFile(file: Path, attrs: BasicFileAttributes): FileVisitResult {
                count(file); return FileVisitResult.CONTINUE
            }
        })
        return obj("logicalBytes" to logical, "allocatedBytes" to allocated, "regularFiles" to files,
            "scope" to "App lstat; unique device/inode within this tree; no symlink traversal")
    }

    private fun deleteTree(directory: File) {
        Files.walkFileTree(directory.toPath(), object : SimpleFileVisitor<Path>() {
            override fun visitFile(file: Path, attrs: BasicFileAttributes): FileVisitResult {
                Files.delete(file); return FileVisitResult.CONTINUE
            }
            override fun postVisitDirectory(dir: Path, exc: IOException?): FileVisitResult {
                if (exc != null) throw exc
                Files.delete(dir); return FileVisitResult.CONTINUE
            }
        })
    }

    companion object {
        private val BUSY = AtomicBoolean(false)
        private val SESSION = Regex("[0-9a-f]{32}")
        private const val COMMIT = "a49a4075228f4801dadc1eee584aed33fe8ea4ce"
        private const val ARCHIVE_SHA256 = "74487243bde86c303e786e79cd4a13e98c3279cae4ab7725dcf5fa51c0b530c1"
        private const val OUTPUT_PREFIX = "DSH_TAWC_PROBE "
        private val TOKEN = Regex("""https?://[^\s'"]*[?&#]token=[A-Za-z0-9._~%-]+""")
        private fun now() = SystemClock.elapsedRealtimeNanos()
        private fun obj(vararg fields: Pair<String, Any?>) = JSONObject().apply {
            fields.forEach { (key, value) -> put(key, value ?: JSONObject.NULL) }
        }
        private fun emptyStop() = obj("termSent" to 0, "prootQuitSent" to 0, "killSent" to 0,
            "parentForced" to false, "remainingIdentities" to 0, "observationUnavailable" to 0,
            "enumerationFailed" to false, "signalErrors" to 0, "clean" to true)
        private fun hex(bytes: ByteArray) = bytes.joinToString("") { (it.toInt() and 255).toString(16).padStart(2, '0') }
        private fun hash(file: File): String {
            val digest = MessageDigest.getInstance("SHA-256")
            file.inputStream().use { stream ->
                val block = ByteArray(65536)
                while (true) {
                    val read = stream.read(block)
                    if (read < 0) break
                    digest.update(block, 0, read)
                }
            }
            return hex(digest.digest())
        }
        private fun writeResult(file: File, value: JSONObject) {
            check(file.parentFile?.isDirectory == true || file.parentFile?.mkdirs() == true) { "result-directory" }
            val temp = File(file.parentFile, file.name + ".tmp")
            temp.writeText(value.toString(2) + "\n")
            Files.move(temp.toPath(), file.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
        }
    }
}
