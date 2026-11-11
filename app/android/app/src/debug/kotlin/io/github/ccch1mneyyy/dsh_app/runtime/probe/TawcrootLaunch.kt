package io.github.ccch1mneyyy.dsh_app.runtime.probe

/** Prototype only: a pure translation of this runtime's prepared PRoot template.
 *
 * The debug probe owns a fresh private session, verifies the packaged launcher,
 * creates the dev view, and stops its identified process tree before cleanup.
 * A tree handed to tawcroot must never be retried with PRoot. No normal startup
 * path calls this mapper; it does not inspect files, preferences or the device.
 */
object TawcrootLaunch {
    data class DevView(
        val directory: String,
        val hostDirectory: String,
        val aliases: Map<String, String>,
    )

    /** Values may contain prepared guest credentials; do not serialize this as a log. */
    data class Plan(
        val argv: List<String>,
        val environment: Map<String, String>,
        val cwd: String,
        val devView: DevView,
        val storeDirectory: String,
        val caveats: List<String>,
    )

    enum class Reason { API, PATH, PREFIX, BIND, GUEST_COMMAND }

    /** Deliberately reports only a category, never an argv element or env value. */
    class Unsupported(val reason: Reason) : IllegalArgumentException("tawcroot prototype: ${reason.name}")

    /** [rootfs] is the caller's already canonical, marker-checked private root.
     * [pty] means the caller has already done PTY/session initialization; pipe
     * launches instead ask tawcroot to create the session with its own -s.
     * The caller checks the launcher is the static ELF from the prototype manifest.
     */
    fun prepare(
        rootfs: String,
        nativeLibraryDir: String,
        prootArgv: List<String>,
        hostEnvironment: Map<String, String>,
        apiLevel: Int,
        pty: Boolean,
    ): Plan {
        if (apiLevel < 29) throw Unsupported(Reason.API)
        if (!absolutePath(rootfs) || rootfs.lastIndexOf('/') == 0 || !absolutePath(nativeLibraryDir)) {
            throw Unsupported(Reason.PATH)
        }
        val prefix = listOf(
            "$nativeLibraryDir/libproot.so", "--kill-on-exit", "--link2symlink",
            "--sysvipc", "-L", "-0", "--rootfs=$rootfs", "--cwd=/root",
        )
        if (prootArgv.take(prefix.size) != prefix) throw Unsupported(Reason.PREFIX)
        val envAt = prootArgv.indexOf("/usr/bin/env")
        if (envAt < prefix.size || prootArgv.getOrNull(envAt + 1) != "-i" || envAt + 2 >= prootArgv.size) {
            throw Unsupported(Reason.GUEST_COMMAND)
        }

        val session = rootfs.substringBeforeLast('/')
        val view = DevView(
            "$session/dev-view", "/dev",
            linkedMapOf(
                "random" to "/dev/urandom",
                "stdin" to "/proc/self/fd/0",
                "stdout" to "/proc/self/fd/1",
                "stderr" to "/proc/self/fd/2",
            ),
        )
        // tawcroot only binds directories. Null entries are represented by the
        // dev-view aliases above, not silently omitted from the launch contract.
        val fixed = linkedMapOf<String, String?>(
            "/dev" to "${view.directory}:/dev",
            "/dev/urandom:/dev/random" to null,
            "/proc" to "/proc:/proc",
            "/proc/self/fd:/dev/fd" to "/proc/self/fd:/dev/fd",
            "/proc/self/fd/0:/dev/stdin" to null,
            "/proc/self/fd/1:/dev/stdout" to null,
            "/proc/self/fd/2:/dev/stderr" to null,
            "/sys" to "/sys:/sys",
            "$rootfs/var/lib/dsh-app/empty:/sys/fs/selinux" to "$rootfs/var/lib/dsh-app/empty:/sys/fs/selinux",
            "$rootfs/var/lib/dsh-app/shm:/dev/shm" to "$rootfs/var/lib/dsh-app/shm:/dev/shm",
            "/system" to "/system:/system",
            "/apex" to "/apex:/apex",
        )
        val remaining = fixed.keys.toMutableSet()
        val optionalTargets = mutableSetOf<String>()
        val caveats = mutableListOf(
            "file_binds_use_dev_view", "sysvipc_unverified", "symlink_lstat_size_unverified",
            "shm_bind_semantics_unverified", "probe_stop_required", "no_proot_fallback",
        )
        val argv = mutableListOf("$nativeLibraryDir/libtawcroot.so")
        if (!pty) argv += "-s"
        argv += listOf("-r", rootfs)
        var at = prefix.size
        while (at < envAt) {
            if (prootArgv[at] != "-b" || at + 1 >= envAt) throw Unsupported(Reason.BIND)
            val value = prootArgv[at + 1]
            if (fixed.containsKey(value)) {
                if (!remaining.remove(value)) throw Unsupported(Reason.BIND)
                fixed[value]?.let { argv += listOf("-b", it) }
            } else {
                val parts = value.split(':')
                if (parts.size !in 1..2 || parts.any { !absolutePath(it) }) throw Unsupported(Reason.BIND)
                val host = parts[0]
                val guest = parts.getOrElse(1) { host }
                val alias = host == rootfs && guest == rootfs
                if (!alias && guest != "/sdcard" && guest != "/storage") throw Unsupported(Reason.BIND)
                if (!optionalTargets.add(guest)) throw Unsupported(Reason.BIND)
                if (alias) caveats += "rootfs_alias_bypasses_token_resolver"
                argv += listOf("-b", "$host:$guest")
            }
            at += 2
        }
        if (remaining.isNotEmpty()) throw Unsupported(Reason.BIND)
        argv += "--"
        // env -i, assignment order (including empty/duplicate values), and the
        // entire command are already prepared. Never parse or shell-quote them.
        argv += prootArgv.drop(envAt)
        val environment = hostEnvironment.filterKeys { !it.startsWith("PROOT_") && it != "LD_LIBRARY_PATH" }
        return Plan(argv.toList(), environment, "$rootfs/root", view, "$session/tawcroot", caveats.toList())
    }

    private fun absolutePath(value: String): Boolean =
        value.startsWith('/') && value != "/" &&
            value.none { it == ':' || it == '\n' || it == '\u0000' } &&
            value.substring(1).split('/').all { it.isNotEmpty() && it != "." && it != ".." }
}
