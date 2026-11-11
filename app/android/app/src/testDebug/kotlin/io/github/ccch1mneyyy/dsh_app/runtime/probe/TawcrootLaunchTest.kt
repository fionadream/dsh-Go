package io.github.ccch1mneyyy.dsh_app.runtime.probe

import io.github.ccch1mneyyy.dsh_app.runtime.probe.TawcrootLaunch.Reason
import org.junit.Assert.*
import org.junit.Test

class TawcrootLaunchTest {
    private val unit = "/data/user/10/io.github.ccch1mneyyy.dsh_app/files/probe/run"
    private val root = "$unit/rootfs"
    private val native = "/data/app/probe/lib/arm64"
    private val command = "echo \$\$ > /tmp/dsh-web.pid; exec /usr/local/bin/dsh web --patch /etc/dsh-app/dsh-web.patch.yml --host 127.0.0.1 --port 3080 --no-open"

    private fun fixedBinds(rootPath: String = root) = listOf(
        "/dev", "/dev/urandom:/dev/random", "/proc", "/proc/self/fd:/dev/fd",
        "/proc/self/fd/0:/dev/stdin", "/proc/self/fd/1:/dev/stdout", "/proc/self/fd/2:/dev/stderr",
        "/sys", "$rootPath/var/lib/dsh-app/empty:/sys/fs/selinux",
        "$rootPath/var/lib/dsh-app/shm:/dev/shm", "/system", "/apex",
    )

    private fun guest(extra: List<String> = emptyList(), shell: String = command) = listOf(
        "/usr/bin/env", "-i", "HOME=/root", "USER=root", "LOGNAME=root", "SHELL=/bin/bash",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG=C.UTF-8", "TERM=xterm-256color", "TMPDIR=/tmp", "DSH_HOME=/root/.dsh",
        "XDG_DATA_DIRS=/usr/local/share:/usr/share:/system", "pnpm_config_registry=https://registry.npmmirror.com/",
        "UV_USE_IO_URING=0", "NARB_DISABLE_NATIVE_CACHE=1", "NODE_OPTIONS=--dns-result-order=ipv4first",
    ) + extra + listOf("/bin/sh", "-c", shell)

    private fun invocation(
        rootPath: String = root,
        nativePath: String = native,
        binds: List<String> = fixedBinds(rootPath),
        tail: List<String> = guest(),
    ) = listOf(
        "$nativePath/libproot.so", "--kill-on-exit", "--link2symlink", "--sysvipc",
        "-L", "-0", "--rootfs=$rootPath", "--cwd=/root",
    ) + binds.flatMap { listOf("-b", it) } + tail

    private fun hostEnvironment(rootPath: String = root, nativePath: String = native) = linkedMapOf(
        "PATH" to "/system/bin:/system/xbin", "HOME" to unit, "TMPDIR" to "$unit/tmp",
        "ANDROID_ROOT" to "/system", "ANDROID_DATA" to "/data",
        "PROOT_TMP_DIR" to "$unit/tmp", "PROOT_LOADER" to "$nativePath/libproot-loader.so",
        "PROOT_L2S_DIR" to "$rootPath/root/.l2s", "LD_LIBRARY_PATH" to nativePath,
    )

    private fun plan(
        rootPath: String = root,
        nativePath: String = native,
        argv: List<String> = invocation(rootPath, nativePath),
        environment: Map<String, String> = hostEnvironment(rootPath, nativePath),
        api: Int = 29,
        pty: Boolean = false,
    ) = TawcrootLaunch.prepare(rootPath, nativePath, argv, environment, api, pty)

    private fun guestOf(plan: TawcrootLaunch.Plan) = plan.argv.drop(plan.argv.indexOf("--") + 1)

    private fun reject(reason: Reason, argv: List<String>) {
        val error = assertThrows(TawcrootLaunch.Unsupported::class.java) { plan(argv = argv) }
        assertEquals(reason, error.reason)
        assertEquals("tawcroot prototype: ${reason.name}", error.message)
    }

    @Test fun completeCurrentTemplateMapsToTheExplicitTawcPlan() {
        val actual = plan()
        val expected = listOf(
            "$native/libtawcroot.so", "-s", "-r", root,
            "-b", "$unit/dev-view:/dev", "-b", "/proc:/proc", "-b", "/proc/self/fd:/dev/fd",
            "-b", "/sys:/sys", "-b", "$root/var/lib/dsh-app/empty:/sys/fs/selinux",
            "-b", "$root/var/lib/dsh-app/shm:/dev/shm", "-b", "/system:/system", "-b", "/apex:/apex", "--",
        ) + guest()
        assertEquals(expected, actual.argv)
        assertEquals("$root/root", actual.cwd)
        assertEquals("$unit/tawcroot", actual.storeDirectory)
        assertEquals(mapOf("PATH" to "/system/bin:/system/xbin", "HOME" to unit, "TMPDIR" to "$unit/tmp",
            "ANDROID_ROOT" to "/system", "ANDROID_DATA" to "/data"), actual.environment)
    }

    @Test fun allFourFileBindsHaveExplicitDevViewRequirements() {
        val actual = plan()
        assertEquals("$unit/dev-view", actual.devView.directory)
        assertEquals("/dev", actual.devView.hostDirectory)
        assertEquals(mapOf("random" to "/dev/urandom", "stdin" to "/proc/self/fd/0",
            "stdout" to "/proc/self/fd/1", "stderr" to "/proc/self/fd/2"), actual.devView.aliases)
        val beforeGuest = actual.argv.takeWhile { it != "--" }
        for (fileBind in listOf("/dev/urandom:/dev/random", "/proc/self/fd/0:/dev/stdin",
                               "/proc/self/fd/1:/dev/stdout", "/proc/self/fd/2:/dev/stderr")) {
            assertFalse(beforeGuest.contains(fileBind))
        }
        assertEquals(listOf("file_binds_use_dev_view", "sysvipc_unverified", "symlink_lstat_size_unverified",
            "shm_bind_semantics_unverified", "probe_stop_required", "no_proot_fallback"), actual.caveats)
    }

    @Test fun ptyCallerDoesNotGetAnotherSessionRequest() {
        val pipe = plan()
        val pty = plan(pty = true)
        assertEquals(pipe.argv.filterIndexed { index, _ -> index != 1 }, pty.argv)
        assertEquals(pipe.environment, pty.environment)
        assertEquals(pipe.cwd, pty.cwd)
        assertEquals(pipe.devView, pty.devView)
    }

    @Test fun guestAssignmentsKeepEmptyValuesDuplicatesAndOverrideOrder() {
        val extra = listOf(
            "TZ=Asia/Shanghai", "EXAMPLE_KEY=synthetic value", "EXAMPLE_KEY=",
            "DSH_APP_WORKSPACE_RECOVERY=1", "UV_THREADPOOL_SIZE=4", "DSH_HOME=/root/.dsh-safe",
            "DSH_APP_ORIGINAL_HOME=/root/.dsh", "DSH_AGENTS_HOME=/root/.dsh-agents", "DSH_APP_SAFE_MODE=1",
            "DSH_GO_RUNTIME_ROOT=$root",
            "NODE_OPTIONS=--max-old-space-size=1536 --import=/opt/dsh-go/direct/proot-dns.mjs",
        )
        val tail = guest(extra)
        assertEquals(tail, guestOf(plan(argv = invocation(tail = tail))))
    }

    @Test fun hostFilteringNeverEditsTheGuestTail() {
        val host = hostEnvironment().apply {
            put("PROOT_NO_SECCOMP", "1")
            put("PROOT_OTHER", "synthetic")
            put("PROBE_LABEL", "value with spaces")
        }
        val tail = guest(listOf("PROOT_NO_SECCOMP=guest-literal", "LD_LIBRARY_PATH=/guest/lib"))
        val actual = plan(argv = invocation(tail = tail), environment = host)
        assertTrue(actual.environment.keys.none { it.startsWith("PROOT_") })
        assertFalse(actual.environment.containsKey("LD_LIBRARY_PATH"))
        assertEquals("value with spaces", actual.environment["PROBE_LABEL"])
        assertEquals(tail, guestOf(actual))
    }

    @Test fun commandTextAndArgumentsStayOpaqueAfterEnvBoundary() {
        val text = "printf '%s' '引号 and --bind=/literal'; " + command
        val tail = guest(shell = text) + listOf("-b", "/usr/bin/env", "--rootfs=/literal", "")
        assertEquals(tail, guestOf(plan(argv = invocation(tail = tail))))
    }

    @Test fun minimalGuestPreflightDoesNotNeedADshShellCommand() {
        for (tail in listOf(listOf("/usr/bin/env", "-i", "/bin/true"),
                            listOf("/usr/bin/env", "-i", "HOME=/root", "/usr/local/bin/node", "--version"))) {
            assertEquals(tail, guestOf(plan(argv = invocation(tail = tail))))
        }
    }

    @Test fun optionalStorageUsesProvidedSourcesIncludingNonzeroUsersAndSpaces() {
        val optional = listOf("/storage/emulated/10:/sdcard", "$unit/synthetic storage:/storage")
        val actual = plan(argv = invocation(binds = fixedBinds() + optional))
        val beforeGuest = actual.argv.takeWhile { it != "--" }
        assertEquals(listOf("-b", optional[0], "-b", optional[1]), beforeGuest.takeLast(4))
        val shorthand = plan(argv = invocation(binds = fixedBinds() + "/storage"))
        assertEquals("/storage:/storage", shorthand.argv[shorthand.argv.indexOf("--") - 1])
    }

    @Test fun samePathAliasIsPreservedWithItsResolverCaveat() {
        val alias = "$root:$root"
        val actual = plan(argv = invocation(binds = fixedBinds() + alias))
        assertEquals(listOf("-b", alias), actual.argv.takeWhile { it != "--" }.takeLast(2))
        assertEquals("rootfs_alias_bypasses_token_resolver", actual.caveats.last())
        assertFalse(plan().caveats.contains("rootfs_alias_bypasses_token_resolver"))
    }

    @Test fun directoryBindsAreNotResortedByGuestPrefixLength() {
        val actual = plan(argv = invocation(binds = fixedBinds().reversed()))
        val binds = actual.argv.subList(4, actual.argv.indexOf("--"))
        assertEquals(listOf(
            "-b", "/apex:/apex", "-b", "/system:/system", "-b", "$root/var/lib/dsh-app/shm:/dev/shm",
            "-b", "$root/var/lib/dsh-app/empty:/sys/fs/selinux", "-b", "/sys:/sys",
            "-b", "/proc/self/fd:/dev/fd", "-b", "/proc:/proc", "-b", "$unit/dev-view:/dev",
        ), binds)
    }

    @Test fun returnedPlanDoesNotShareMutableInputCollections() {
        val argv = invocation().toMutableList()
        val environment = hostEnvironment()
        val beforeArgv = argv.toList()
        val beforeEnvironment = environment.toMap()
        val actual = plan(argv = argv, environment = environment)
        assertEquals(beforeArgv, argv)
        assertEquals(beforeEnvironment, environment)
        argv[argv.lastIndex] = "changed after mapping"
        environment["HOME"] = "/changed-after-mapping"
        assertEquals(command, actual.argv.last())
        assertEquals(unit, actual.environment["HOME"])
        assertEquals(actual, plan())
    }

    @Test fun apiGateIsTwentyNineAndDoesNotNeedAKernelVersion() {
        val error = assertThrows(TawcrootLaunch.Unsupported::class.java) { plan(api = 28) }
        assertEquals(Reason.API, error.reason)
        assertEquals(plan(api = 29), plan(api = 36))
    }

    @Test fun rootAndNativePathsMustAlreadyBeAbsoluteAndCanonicalInShape() {
        for (bad in listOf("", "relative/rootfs", "/", "/rootfs", "$unit/../rootfs", "$root/", "$unit/a:b", "$unit/a\nb", "$unit/a\u0000b")) {
            val error = assertThrows(TawcrootLaunch.Unsupported::class.java) { plan(rootPath = bad) }
            assertEquals(Reason.PATH, error.reason)
        }
        val nativeError = assertThrows(TawcrootLaunch.Unsupported::class.java) { plan(nativePath = "relative/lib") }
        assertEquals(Reason.PATH, nativeError.reason)
        val spacedRoot = "$unit/session with spaces/rootfs"
        val spaced = plan(rootPath = spacedRoot, nativePath = "/data/app/space here/lib/arm64")
        assertEquals("$spacedRoot/root", spaced.cwd)
        assertEquals("$unit/session with spaces/dev-view", spaced.devView.directory)
        assertEquals("$unit/session with spaces/tawcroot", spaced.storeDirectory)
        assertEquals("/data/app/space here/lib/arm64/libtawcroot.so", spaced.argv.first())
    }

    @Test fun everyCurrentPrefixElementIsRequired() {
        for (index in 0 until 8) {
            val missing = invocation().toMutableList().apply { removeAt(index) }
            reject(Reason.PREFIX, missing)
        }
        reject(Reason.PREFIX, invocation().map { if (it == "--cwd=/root") "--cwd=/tmp" else it })
        reject(Reason.PREFIX, invocation().map { if (it == "--rootfs=$root") "--rootfs=/other/rootfs" else it })
    }

    @Test fun everyFixedBindIncludingEachFileBindIsRequiredOnce() {
        for (index in fixedBinds().indices) {
            reject(Reason.BIND, invocation(binds = fixedBinds().filterIndexed { at, _ -> at != index }))
        }
        reject(Reason.BIND, invocation(binds = fixedBinds() + "/dev"))
        val missingValue = invocation().toMutableList().apply { removeAt(9) }
        reject(Reason.BIND, missingValue)
    }

    @Test fun unknownOrAmbiguousBindsAreNotSilentlyDropped() {
        for (bad in listOf("/host:/etc", "relative:/sdcard", "/host:/sdcard:ro", "/other:/other", "/host:/", "/host/:/storage")) {
            reject(Reason.BIND, invocation(binds = fixedBinds() + bad))
        }
        reject(Reason.BIND, invocation(binds = fixedBinds() + listOf("/first:/sdcard", "/second:/sdcard")))
        reject(Reason.BIND, invocation(binds = fixedBinds() + listOf("$root:$root", "$root:$root")))
    }

    @Test fun missingOrChangedEnvBoundaryIsUnsupported() {
        reject(Reason.GUEST_COMMAND, invocation(tail = listOf("/usr/bin/env", "-u", "HOME", "/bin/true")))
        reject(Reason.GUEST_COMMAND, invocation(tail = listOf("/bin/true")))
        reject(Reason.GUEST_COMMAND, invocation(tail = listOf("/usr/bin/env", "-i")))
    }

    @Test fun rejectionDoesNotEchoAnUnknownArgumentValue() {
        val text = "--unknown=synthetic-private-value"
        val error = assertThrows(TawcrootLaunch.Unsupported::class.java) {
            plan(argv = invocation().toMutableList().apply { this[1] = text })
        }
        assertEquals(Reason.PREFIX, error.reason)
        assertEquals("tawcroot prototype: PREFIX", error.message)
        assertFalse(error.message.orEmpty().contains(text))
    }
}
