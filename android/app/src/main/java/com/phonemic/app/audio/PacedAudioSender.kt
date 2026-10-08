package com.phonemic.app.audio

import android.os.SystemClock
import android.util.Log
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

/**
 * Releases captured audio frames onto the network on a fixed schedule, off the capture thread.
 *
 * [AudioRecorder] hands over a frame whenever the hardware buffer has one ready, which is as
 * fast as the CPU will go. Every one of those frames was then sent inline on that same thread,
 * and the result was measured on the PC as a very uneven arrival pattern: roughly 78% of
 * packets landing back to back within 2ms of the previous one, only 2% arriving at a normal
 * spacing, and gaps of up to 195ms that were always a whole number of frames. That is a hard
 * cut, not a slightly uneven stream.
 *
 * Inline sending has a second cost. Any latency in the send stops the microphone being read,
 * the hardware buffer overruns, and real audio is lost rather than merely delayed.
 *
 * So capture now only copies into a bounded queue and returns immediately, and this thread
 * releases exactly one frame per frame duration measured on the monotonic clock. The queue is
 * deliberately bounded and shallow: if anything stalls, the oldest frames are dropped and
 * counted rather than allowed to pile up, because latency that grows without bound is worse
 * than a gap, and a gap is what the PC can conceal.
 */
class PacedAudioSender(
    private val transmit: (ByteArray, Int, Boolean, Int) -> Unit
) {

    private class Slot {
        val data = ByteArray(AudioConfig.MAX_PACKET_SIZE)
        var length = 0
        var isStereo = false
        var samples = 0
    }

    companion object {
        private const val TAG = "ZMicPacedSender"

        /**
         * Frames allowed to wait. Sixteen is 80ms at a 5ms frame, enough to absorb
         * Android scheduling pauses and Wi-Fi bursts without discarding audio.
         */
        private const val MAX_BACKLOG = 16

        /** Longer than this and we are not scheduling jitter, we are behind. */
        private const val LATE_TOLERANCE_FACTOR = 8L

        private const val LOG_INTERVAL_NS = 10_000_000_000L
    }

    private val freeSlots = ArrayBlockingQueue<Slot>(MAX_BACKLOG * 4)
    private val pending = ArrayBlockingQueue<Slot>(MAX_BACKLOG * 4)
    private val running = AtomicBoolean(false)

    private val framesSent = AtomicLong(0)
    private val framesDroppedNoSlot = AtomicLong(0)
    private val framesDroppedTooLate = AtomicLong(0)

    private var worker: Thread? = null

    /** Derived from the first frame, so the grid matches whatever config was negotiated. */
    @Volatile
    private var frameDurationNs = 5_000_000L

    init {
        repeat(MAX_BACKLOG * 4) { freeSlots.offer(Slot()) }
    }

    fun start() {
        if (running.getAndSet(true)) return
        worker = Thread({ loop() }, "ZMic-PacedSender").apply {
            isDaemon = true
            // Below the capture thread: sending must never outrank reading the microphone,
            // or the hardware buffer overruns and real audio is lost.
            priority = Thread.NORM_PRIORITY + 1
            start()
        }
        Log.i(TAG, "Paced sender started, frame duration ${frameDurationNs / 1_000_000L}ms")
    }

    /**
     * Queues one captured frame. Never blocks on the network and never allocates.
     *
     * @param data source buffer, reused by the caller, so the contents are copied here
     */
    fun enqueue(data: ByteArray, length: Int, isStereo: Boolean, samplesPerChannel: Int) {
        if (!running.get() || length <= 0) return

        val slot = freeSlots.poll()
        if (slot == null) {
            framesDroppedNoSlot.incrementAndGet()
            return
        }
        System.arraycopy(data, 0, slot.data, 0, length)
        slot.length = length
        slot.isStereo = isStereo
        slot.samples = samplesPerChannel

        if (!pending.offer(slot)) {
            freeSlots.offer(slot)
            framesDroppedTooLate.incrementAndGet()
        }
    }

    private fun loop() {
        var nextFrameNs = SystemClock.elapsedRealtimeNanos()
        var maxBacklogSeen = 0
        var lastLogNs = nextFrameNs
        var lateResyncs = 0L

        while (running.get()) {
            // Never let the backlog grow into latency. Anything already past its slot is
            // dropped here rather than sent as a burst later, which is what made the
            // arrival pattern unusable in the first place.
            while (pending.size > MAX_BACKLOG) {
                pending.poll()?.let { freeSlots.offer(it) }
                framesDroppedTooLate.incrementAndGet()
            }
            if (pending.size > maxBacklogSeen) maxBacklogSeen = pending.size

            val slot = pending.poll()
            if (slot == null) {
                // Nothing captured yet. Idle against the grid so a late start does not
                // leave the thread permanently behind.
                nextFrameNs = SystemClock.elapsedRealtimeNanos()
                if (!park(1_000_000L)) return
                continue
            }

            if (slot.samples > 0) {
                frameDurationNs = slot.samples * 1_000_000_000L / AudioConfig.SAMPLE_RATE
            }

            nextFrameNs += frameDurationNs
            var waitNs = nextFrameNs - SystemClock.elapsedRealtimeNanos()
            if (waitNs > 0) {
                if (!park(waitNs)) {
                    freeSlots.offer(slot)
                    return
                }
            } else if (-waitNs > frameDurationNs * LATE_TOLERANCE_FACTOR) {
                // Badly behind, most likely the network stalled. Resynchronise instead of
                // trying to claw back time, otherwise every later frame inherits the debt.
                lateResyncs++
                nextFrameNs = SystemClock.elapsedRealtimeNanos() + frameDurationNs
            }

            transmit(slot.data, slot.length, slot.isStereo, slot.samples)
            freeSlots.offer(slot)
            framesSent.incrementAndGet()

            val now = SystemClock.elapsedRealtimeNanos()
            if (now - lastLogNs >= LOG_INTERVAL_NS) {
                lastLogNs = now
                Log.i(
                    TAG,
                    "sent=${framesSent.get()} droppedNoSlot=${framesDroppedNoSlot.get()} " +
                        "droppedLate=${framesDroppedTooLate.get()} backlogMax=$maxBacklogSeen " +
                        "lateResyncs=$lateResyncs frameMs=${frameDurationNs / 1_000_000L}"
                )
                maxBacklogSeen = 0
            }
        }
    }

    /** Sleeps for [nanos]. Returns false if the thread was asked to stop while waiting. */
    private fun park(nanos: Long): Boolean {
        if (nanos <= 0) return running.get()
        return try {
            val millis = nanos / 1_000_000L
            val remainderNanos = (nanos % 1_000_000L).toInt()
            if (millis > 0 || remainderNanos > 0) {
                Thread.sleep(millis, remainderNanos)
            }
            running.get()
        } catch (_: InterruptedException) {
            false
        }
    }

    fun close() {
        if (!running.getAndSet(false)) return
        worker?.let { w ->
            try {
                w.join(400)
            } catch (_: InterruptedException) {
                Thread.currentThread().interrupt()
            }
        }
        worker = null
        pending.clear()
        Log.i(
            TAG,
            "Paced sender stopped: sent=${framesSent.get()} " +
                "droppedNoSlot=${framesDroppedNoSlot.get()} droppedLate=${framesDroppedTooLate.get()}"
        )
    }
}
