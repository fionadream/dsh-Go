package io.github.ccch1mneyyy.dsh_app.phone

import java.util.Collections
import java.util.concurrent.CompletableFuture
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executor
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import org.junit.After
import org.junit.Assert.*
import org.junit.Test

class PhoneSwitchDispatchTest {
    private val control = Executors.newSingleThreadExecutor()
    private val io = Executors.newSingleThreadExecutor()
    private val nextIo = Executors.newSingleThreadExecutor()
    private val dispatch = PhoneSwitchDispatch(control)
    private val enabled = AtomicBoolean(true)
    private val events = Collections.synchronizedList(mutableListOf<String>())

    @After fun close() { control.shutdownNow(); io.shutdownNow(); nextIo.shutdownNow() }

    private fun request(on: Boolean, lane: Executor = io, prepare: () -> Boolean = { true },
                        cleanup: () -> Boolean = { events += "cleanup"; true }): CompletableFuture<Result<Boolean>> {
        val result = CompletableFuture<Result<Boolean>>()
        dispatch.dispatch(on, lane, prepare, { enabled.set(it); events += "enabled=$it"; true }, cleanup,
            { result.complete(it) })
        return result
    }

    private fun result(value: CompletableFuture<Result<Boolean>>) = value.get(5, TimeUnit.SECONDS).getOrThrow()

    @Test fun offIsPublishedWhileModeIsBusyButOnAndFollowingModeWaitForCleanup() {
        val entered = CountDownLatch(1); val release = CountDownLatch(1)
        io.execute { entered.countDown(); check(release.await(5, TimeUnit.SECONDS)); events += "old-mode" }
        try {
            assertTrue(entered.await(5, TimeUnit.SECONDS))
            val off = request(false)
            val on = request(true)
            val following = io.submit { events += "new-mode" }
            control.submit {}.get(5, TimeUnit.SECONDS)
            assertFalse(enabled.get())
            assertFalse(off.isDone); assertFalse(on.isDone)
            assertEquals(listOf("enabled=false"), events.toList())
            release.countDown()
            assertTrue(result(off)); assertTrue(result(on)); following.get(5, TimeUnit.SECONDS)
            assertEquals(listOf("enabled=false", "old-mode", "cleanup", "enabled=true", "new-mode"), events.toList())
        } finally { release.countDown() }
    }

    @Test fun anEarlierQueuedOnCannotUndoALaterOff() {
        val entered = CountDownLatch(1); val release = CountDownLatch(1)
        io.execute { entered.countDown(); check(release.await(5, TimeUnit.SECONDS)) }
        try {
            assertTrue(entered.await(5, TimeUnit.SECONDS))
            val on = request(true, prepare = { error("superseded enable must not prepare") })
            val off = request(false)
            control.submit {}.get(5, TimeUnit.SECONDS)
            assertFalse(enabled.get())
            release.countDown()
            assertTrue(result(on)); assertTrue(result(off)); assertFalse(enabled.get())
            assertEquals(listOf("enabled=false", "cleanup"), events.toList())
        } finally { release.countDown() }
    }

    @Test fun anOffDuringEnablePreparationDoesNotWaitForItOrReviveAfterIt() {
        val entered = CountDownLatch(1); val release = CountDownLatch(1)
        try {
            val on = request(true, prepare = { entered.countDown(); check(release.await(5, TimeUnit.SECONDS)); true })
            assertTrue(entered.await(5, TimeUnit.SECONDS))
            val off = request(false)
            control.submit {}.get(5, TimeUnit.SECONDS)
            assertFalse(enabled.get())
            release.countDown()
            assertTrue(result(on)); assertTrue(result(off)); assertFalse(enabled.get())
            assertEquals(listOf("enabled=false", "cleanup"), events.toList())
        } finally { release.countDown() }
    }

    @Test fun anOffAfterEnableEnteredControlIsStillTheLastRealChange() {
        val entered = CountDownLatch(1); val release = CountDownLatch(1)
        val on = CompletableFuture<Result<Boolean>>()
        try {
            dispatch.dispatch(true, io, { true }, {
                entered.countDown(); check(release.await(5, TimeUnit.SECONDS))
                enabled.set(it); events += "enabled=$it"; true
            }, { error("on has no off cleanup") }, { on.complete(it) })
            assertTrue(entered.await(5, TimeUnit.SECONDS))
            val off = request(false)
            assertFalse(off.isDone)
            release.countDown()
            assertTrue(result(on)); assertTrue(result(off)); assertFalse(enabled.get())
            assertEquals(listOf("enabled=true", "enabled=false", "cleanup"), events.toList())
        } finally { release.countDown() }
    }

    @Test fun replacementPluginEnableWaitsForTheOriginalPluginsCleanup() {
        val entered = CountDownLatch(1); val release = CountDownLatch(1)
        try {
            val off = request(false, cleanup = {
                entered.countDown(); check(release.await(5, TimeUnit.SECONDS)); events += "cleanup"; true
            })
            assertTrue(entered.await(5, TimeUnit.SECONDS))
            val on = request(true, lane = nextIo)
            control.submit {}.get(5, TimeUnit.SECONDS)
            assertFalse(enabled.get()); assertFalse(on.isDone)
            release.countDown()
            assertTrue(result(off)); assertTrue(result(on))
            assertEquals(listOf("enabled=false", "cleanup", "enabled=true"), events.toList())
        } finally { release.countDown() }
    }

    @Test fun falseAndThrowingCleanupRemainVisibleAndReleaseTheNextTicket() {
        assertFalse(result(request(false, cleanup = { false })))
        val failure = IllegalStateException("cleanup failed")
        val failed = request(false, cleanup = { throw failure })
        val on = request(true, lane = nextIo)
        assertSame(failure, failed.get(5, TimeUnit.SECONDS).exceptionOrNull())
        assertTrue(result(on)); assertTrue(enabled.get())
    }

    @Test fun aFailedChangeSkipsCleanupAndAThrowingCompletionCannotStrandSuccessors() {
        val failure = IllegalStateException("change failed")
        val failed = CompletableFuture<Result<Boolean>>()
        val completion = CountDownLatch(1)
        dispatch.dispatch(false, io, { true }, { throw failure }, { error("no cleanup after failed change") }, {
            failed.complete(it); completion.countDown(); throw IllegalStateException("callback failed")
        })
        assertTrue(completion.await(5, TimeUnit.SECONDS))
        val on = request(true, lane = nextIo)
        assertSame(failure, failed.get(5, TimeUnit.SECONDS).exceptionOrNull())
        assertTrue(result(on))
    }

    @Test fun aRejectedPreparationDoesNotPublishEnable() {
        enabled.set(false)
        assertFalse(result(request(true, prepare = { false })))
        assertFalse(enabled.get()); assertTrue(events.isEmpty())
    }
}
