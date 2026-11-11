package io.github.ccch1mneyyy.dsh_app.phone

import java.util.concurrent.CompletableFuture
import java.util.concurrent.Executor

/** Process-wide switch order; dispatch and io submission both run on the channel's main thread. */
internal class PhoneSwitchDispatch(private val controlIo: Executor) {
    private var tail = CompletableFuture.completedFuture(Unit)
    @Volatile private var offGeneration = 0L

    fun dispatch(enabled: Boolean, io: Executor, prepareEnable: () -> Boolean,
                 change: (Boolean) -> Boolean, backgroundOff: () -> Boolean,
                 complete: (Result<Boolean>) -> Unit) {
        val previous = tail
        val finished = CompletableFuture<Unit>()
        tail = finished
        if (!enabled) offGeneration++
        val generation = offGeneration
        // Off never waits for a mode operation. Its later cleanup is reserved on io now,
        // before a subsequent method call can enqueue a new mode or an enable there.
        val disabled = if (!enabled) changeOnControl { change(false) } else null
        io.execute {
            val value = try {
                runCatching {
                    previous.join()
                    if (disabled != null) {
                        disabled.join().getOrThrow() && backgroundOff()
                    } else if (generation != offGeneration) {
                        true // A later off superseded this queued enable.
                    } else if (!prepareEnable()) {
                        false
                    } else {
                        // All real changes use the same control lane, including after reattach.
                        // Recheck after prepare: a newer off may have arrived while it blocked.
                        changeOnControl {
                            if (generation != offGeneration) true else change(true)
                        }.join().getOrThrow()
                    }
                }
            } finally {
                // Completion is ordering, not success. Preserve false/throw in the result.
                finished.complete(Unit)
            }
            complete(value)
        }
    }

    private fun changeOnControl(change: () -> Boolean): CompletableFuture<Result<Boolean>> {
        val changed = CompletableFuture<Result<Boolean>>()
        controlIo.execute { changed.complete(runCatching(change)) }
        return changed
    }
}
