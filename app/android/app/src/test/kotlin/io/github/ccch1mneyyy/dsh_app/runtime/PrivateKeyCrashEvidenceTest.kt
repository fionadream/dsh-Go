package io.github.ccch1mneyyy.dsh_app.runtime

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.util.concurrent.ScheduledThreadPoolExecutor
import java.util.concurrent.TimeUnit

/** Exercise the same raw/storage split as DshProcess -> RuntimeController -> RuntimeLogs. */
class PrivateKeyCrashEvidenceTest {
    private val begin = "-----BEGIN " + "PRIVATE KEY-----"
    private val end = "-----END " + "PRIVATE KEY-----"
    private val body = "SYNTHETIC_PRIVATE_BODY"
    private val fatal = "dsh: startup failed: $body"
    private val heap = "FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory"
    private val exec = "sh: exec: /usr/local/bin/dsh: Permission denied"
    private val proot = "proot error: execve(\"/usr/bin/env\"): Permission denied"
    private val direct = "[dsh-app-direct] launch failed: $body"

    private fun capture(lines: List<String>): List<CrashClassifier.TailLine> {
        val redactor = LogRedactor()
        return lines.map { CrashClassifier.TailLine(it, redactor.line(it)) }
    }

    private fun hidden(vararg lines: String) = capture(listOf(begin, body) + lines)

    private fun classify(
        output: List<CrashClassifier.TailLine>,
        ready: Long? = null,
        now: Long = 120_001L,
        code: Int = 1,
        direct: Boolean = false,
    ) = CrashClassifier.classify(
        code, ready, now, 123L, output.map { it.storageLine }, false, direct,
        output.flatMap { it.signals }.toSet(),
    )

    private fun assertPrivate(failure: RunFailure) {
        assertNull(failure.summary)
        assertTrue(failure.requiredRows.isEmpty())
        assertTrue(failure.suspects.isEmpty())
        assertNull(failure.diagnostics)
        assertFalse(JSONObject(failure.toMap()).toString().contains(body))
    }

    @Test fun hiddenFatalKeepsStartupEarlyAndLateClassificationWithoutCapturingItsText() {
        for (line in listOf(
            fatal,
            "dsh: fatal uncaught exception: $body",
            "dsh: fatal load failure: $body",
            "TypeError: dsh: $body",
            "dsh: profile startup and cleanup failed $body",
            "\u001B[31m$fatal\u001B[0m",
        )) {
            val output = hidden(line)
            val startup = classify(output)
            assertEquals(FailureKind.STARTUP_FAILED, startup.kind)
            assertTrue(CrashClassifier.classifyAfterTokenTimeout(true, startup))
            assertEquals(FailureKind.CRASHED_EARLY, classify(output, ready = 1L).kind)
            assertEquals(FailureKind.CRASHED_LATE, classify(output, ready = 0L).kind)
            assertPrivate(startup)
        }
        assertEquals(FailureKind.KILLED, classify(hidden("plugin says: $fatal")).kind)
        assertEquals(FailureKind.KILLED, classify(hidden("normal progress")).kind)
    }

    @Test fun hiddenHeapMarkerKeepsTheNonzeroExitAndEarlyWindowRequirements() {
        val other = "FATAL ERROR: Ineffective mark-compacts near heap limit Allocation failed - JavaScript heap out of memory"
        for (line in listOf(heap, other)) {
            val output = hidden(line)
            val startup = classify(output)
            assertEquals(FailureKind.STARTUP_FAILED, startup.kind)
            assertEquals(FailureKind.CRASHED_EARLY, classify(output, ready = 1L).kind)
            assertEquals(FailureKind.KILLED, classify(output, ready = 0L).kind)
            assertEquals(FailureKind.KILLED, classify(output, code = 0).kind)
            assertPrivate(startup)
        }
        assertEquals(FailureKind.KILLED, classify(hidden("$heap (sample text)")).kind)
    }

    @Test fun hiddenDiskFullKeepsPriorityOverFatalAndLaunchSignals() {
        for (line in listOf("ENOSPC: $body", "No space left on device: $body")) {
            val output = hidden(direct, exec, proot, fatal, line)
            for (isDirect in listOf(false, true)) {
                val failure = classify(output, direct = isDirect)
                assertEquals(FailureKind.NO_SPACE, failure.kind)
                assertTrue(CrashClassifier.classifyAfterTokenTimeout(true, failure))
                assertFalse(failure.kind.counts)
                assertPrivate(failure)
            }
        }
    }

    @Test fun hiddenLaunchMarkersKeepTheirEngineReadyAndExitCodeGuards() {
        for (line in listOf(exec, proot, direct)) {
            val isDirect = line == direct
            val output = hidden(line)
            val failure = classify(output, direct = isDirect)
            assertEquals(FailureKind.LAUNCH_FAILED, failure.kind)
            assertTrue(CrashClassifier.classifyAfterTokenTimeout(true, failure))
            assertEquals(isDirect, SafePolicy.directLaunchFailed(failure, isDirect))
            assertEquals(FailureKind.KILLED, classify(output, ready = 1L, direct = isDirect).kind)
            assertEquals(FailureKind.KILLED, classify(output, code = 0, direct = isDirect).kind)
            assertPrivate(failure)
        }
        assertEquals(FailureKind.KILLED, classify(hidden(proot), direct = true).kind)
        assertEquals(FailureKind.KILLED, classify(hidden(direct)).kind)
        assertEquals(FailureKind.STARTUP_FAILED, classify(hidden(direct, fatal), direct = true).kind)
    }

    @Test fun unfinishedPemDoesNotChangeSigsysFallbackOrSignalPriority() {
        val output = hidden("ordinary progress")
        val failure = classify(output, direct = true, code = CrashClassifier.SIGSYS_EXIT)
        assertEquals(FailureKind.LAUNCH_FAILED, failure.kind)
        assertEquals("killed by the system's syscall filter (SIGSYS)", failure.summary)
        assertTrue(SafePolicy.directLaunchFailed(failure, true))
        assertEquals(FailureKind.KILLED, classify(output, code = CrashClassifier.SIGSYS_EXIT).kind)
        assertEquals(FailureKind.KILLED, classify(output, ready = 1L, direct = true, code = CrashClassifier.SIGSYS_EXIT).kind)
        assertEquals(FailureKind.STARTUP_FAILED, classify(hidden(fatal), direct = true, code = CrashClassifier.SIGSYS_EXIT).kind)
        assertEquals(FailureKind.NO_SPACE, classify(hidden("ENOSPC"), direct = true, code = CrashClassifier.SIGSYS_EXIT).kind)
        assertFalse(JSONObject(failure.toMap()).toString().contains(body))
    }

    @Test fun rawPrivateBodyCannotReturnThroughMetadataOrEitherLogFile() {
        val raw = listOf(
            begin, body, fatal,
            "  $body-row (required)",
            "  Package: $body-package",
            "    at /profiles/web/node_modules/$body-stack/index.js:1:2",
            "Full diagnostics: /tmp/$body-diagnostics.txt",
        )
        val output = capture(raw)
        val failure = classify(output)
        assertEquals(FailureKind.STARTUP_FAILED, failure.kind)
        assertPrivate(failure)
        assertTrue(output.drop(1).all { it.storageLine == "•••" })
        val dir = Files.createTempDirectory("private-key-crash").toFile()
        val writer = ScheduledThreadPoolExecutor(1)
        try {
            val engineFile = File(dir, "dsh.log")
            val logs = RuntimeLogs(engineFile, { 1L }, { }, { }, writer)
            raw.zip(output).forEach { (line, saved) -> logs.accept(line, "stderr", saved.storageLine) }
            logs.flush().get(5, TimeUnit.SECONDS)
            // recordFailure's inputs: only storageLine reaches the failure log and toMap reaches prefs.
            val failureFile = File(dir, "failure.log")
            failureFile.writeText(output.joinToString("\n", postfix = "\n") { LogRedactor.redact(it.storageLine) })
            val metadataFile = File(dir, "failure.json")
            metadataFile.writeText(JSONObject(failure.toMap()).toString())
            for (file in listOf(engineFile, failureFile, metadataFile)) {
                assertFalse(file.name, file.readText().contains(body))
            }
            assertEquals(raw, logs.recent(raw.size)) // The live token-only path is unchanged.
        } finally {
            writer.shutdownNow()
            writer.awaitTermination(2, TimeUnit.SECONDS)
            dir.deleteRecursively()
        }
    }

    @Test fun closingPemRestoresVisibleMetadataAndUnaffectedOutputKeepsItsSummary() {
        val outside = listOf(
            "dsh: startup failed: ordinary plugin failure",
            "  visible-row (required)",
            "  Package: visible-package",
            "Full diagnostics: /tmp/visible-diagnostics.txt",
        )
        val failure = classify(capture(listOf(begin, body, end) + outside))
        assertEquals(FailureKind.STARTUP_FAILED, failure.kind)
        assertEquals("startup failed: ordinary plugin failure", failure.summary)
        assertEquals(listOf("visible-row"), failure.requiredRows)
        assertEquals(listOf("visible-package"), failure.suspects)
        assertEquals("/tmp/visible-diagnostics.txt", failure.diagnostics)
        assertFalse(JSONObject(failure.toMap()).toString().contains(body))
        assertEquals(CrashClassifier.classify(1, null, 120_001L, 123L, outside, false), classify(capture(outside)))
    }
}
