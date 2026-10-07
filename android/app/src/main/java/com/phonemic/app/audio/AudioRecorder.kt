package com.phonemic.app.audio

import android.annotation.SuppressLint
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.util.Log
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.max

/**
 * Manages low-latency audio capture from physical microphones using AudioRecord.
 * Attempts dual-mic Stereo capture first, with transparent fallback to Mono.
 */
class AudioRecorder(
    private val onAudioChunkCaptured: (ByteArray, Int, Boolean, Int) -> Unit
) {
    private var audioRecord: AudioRecord? = null
    private var recordingThread: Thread? = null

    @Volatile
    private var isRecording = false

    @Volatile
    private var isMuted = false

    /** Invoked once if the capture thread dies from an unrecoverable error. */
    var onCaptureError: ((String) -> Unit)? = null

    var isStereo: Boolean = false
        private set

    var frameSizeBytes: Int = AudioConfig.FRAME_BYTES_MONO
        private set

    var samplesPerChannel: Int = AudioConfig.SAMPLES_PER_FRAME_MONO_10MS
        private set

    fun setMuted(muted: Boolean) {
        isMuted = muted
    }

    @SuppressLint("MissingPermission")
    fun start() {
        if (isRecording) return

        data class RecordConfig(
            val audioSource: Int,
            val channelConfig: Int,
            val isStereo: Boolean,
            val frameBytes: Int,
            val samplesPerChannel: Int
        )

        // MTU-Safe Frames (960-byte payload): fits inside a 1472-byte Wi-Fi MTU, no IP fragmentation.
        // 1. CAMCORDER stereo (5ms = 240 samples = 960 bytes) -- also disables AGC/NS on most devices
        // 2. MIC stereo fallback (5ms)
        // 3. MIC mono fallback (10ms = 480 samples = 960 bytes)
        val candidates = listOf(
            RecordConfig(MediaRecorder.AudioSource.CAMCORDER, AudioConfig.CHANNEL_CONFIG_STEREO, true, AudioConfig.FRAME_BYTES_STEREO, AudioConfig.SAMPLES_PER_FRAME_STEREO_5MS),
            RecordConfig(MediaRecorder.AudioSource.MIC, AudioConfig.CHANNEL_CONFIG_STEREO, true, AudioConfig.FRAME_BYTES_STEREO, AudioConfig.SAMPLES_PER_FRAME_STEREO_5MS),
            RecordConfig(MediaRecorder.AudioSource.MIC, AudioConfig.CHANNEL_CONFIG_MONO, false, AudioConfig.FRAME_BYTES_MONO, AudioConfig.SAMPLES_PER_FRAME_MONO_10MS)
        )

        var record: AudioRecord? = null
        var selectedConfig: RecordConfig? = null

        for (candidate in candidates) {
            val minBufSize = AudioRecord.getMinBufferSize(
                AudioConfig.SAMPLE_RATE,
                candidate.channelConfig,
                AudioConfig.AUDIO_FORMAT
            )
            if (minBufSize <= 0) {
                Log.w(TAG, "Unsupported capture config: sr=${AudioConfig.SAMPLE_RATE} ch=${candidate.channelConfig}")
                continue
            }

            val internalBufferSize = max(minBufSize * 2, candidate.frameBytes * 4)
            var candidateRecord: AudioRecord? = null
            try {
                candidateRecord = AudioRecord(
                    candidate.audioSource,
                    AudioConfig.SAMPLE_RATE,
                    candidate.channelConfig,
                    AudioConfig.AUDIO_FORMAT,
                    internalBufferSize
                )
                if (candidateRecord.state == AudioRecord.STATE_INITIALIZED) {
                    record = candidateRecord
                    selectedConfig = candidate
                    Log.i(
                        TAG,
                        "Capture config accepted: source=${candidate.audioSource} " +
                            "stereo=${candidate.isStereo} buffer=${internalBufferSize}B"
                    )
                    break
                }
                Log.w(TAG, "AudioRecord failed to initialize for source=${candidate.audioSource}")
            } catch (e: Exception) {
                Log.w(TAG, "AudioRecord constructor threw for source=${candidate.audioSource}", e)
            }
            // Never leak the half-built recorder when a later candidate wins.
            try {
                candidateRecord?.release()
            } catch (_: Exception) {
            }
        }

        val finalRecord = record ?: throw IllegalStateException(
            "Failed to initialize AudioRecord (tried CAMCORDER stereo, MIC stereo, MIC mono)"
        )
        val config = selectedConfig!!

        isStereo = config.isStereo
        frameSizeBytes = config.frameBytes
        samplesPerChannel = config.samplesPerChannel
        audioRecord = finalRecord

        // startRecording() BEFORE flipping the flag so the reader thread never reads a stopped record.
        finalRecord.startRecording()
        if (finalRecord.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
            finalRecord.release()
            audioRecord = null
            throw IllegalStateException("AudioRecord did not enter RECORDING state")
        }
        isRecording = true

        recordingThread = Thread({
            // Every failure below must be contained: an exception escaping run() kills the process.
            try {
                android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_URGENT_AUDIO)
            } catch (e: Exception) {
                Log.w(TAG, "Could not elevate capture thread priority", e)
            }

            val frameBuffer = ByteArray(frameSizeBytes)
            val silenceBuffer = ByteArray(frameSizeBytes)

            // API 21/22: AudioRecord.read(byte[],int,int) is NON-blocking there, which turns the
            // accumulator loop into a ~1kHz spin. read(ByteBuffer) blocks correctly on all versions.
            val useDirectBuffer = Build.VERSION.SDK_INT < Build.VERSION_CODES.M
            val directBuffer = if (useDirectBuffer) {
                ByteBuffer.allocateDirect(frameSizeBytes).order(ByteOrder.LITTLE_ENDIAN)
            } else {
                null
            }

            var firstFrameLogged = false

            try {
                while (isRecording) {
                    var bytesReadTotal = 0
                    var readError = false

                    while (isRecording && bytesReadTotal < frameSizeBytes) {
                        val remaining = frameSizeBytes - bytesReadTotal
                        val bytesRead = if (useDirectBuffer) {
                            directBuffer!!.position(bytesReadTotal)
                            directBuffer.limit(frameSizeBytes)
                            val n = finalRecord.read(directBuffer, frameSizeBytes)
                            if (n > 0) {
                                directBuffer.position(bytesReadTotal)
                                directBuffer.get(frameBuffer, bytesReadTotal, n)
                            }
                            n
                        } else {
                            finalRecord.read(frameBuffer, bytesReadTotal, remaining)
                        }

                        if (bytesRead > 0) {
                            bytesReadTotal += bytesRead
                        } else if (bytesRead < 0) {
                            Log.e(TAG, "Fatal AudioRecord.read error code $bytesRead")
                            readError = true
                            break
                        } else {
                            // Transient 0-byte read; yield briefly instead of spinning hot.
                            try {
                                Thread.sleep(1)
                            } catch (_: InterruptedException) {
                                break
                            }
                        }
                    }

                    if (readError) {
                        isRecording = false
                        onCaptureError?.invoke("Microphone read error (code $bytesReadTotal)")
                        break
                    }

                    if (bytesReadTotal > 0 && isRecording) {
                        if (!firstFrameLogged) {
                            Log.i(
                                TAG,
                                "First frame captured: $bytesReadTotal bytes, stereo=$isStereo, " +
                                    "samplesPerChannel=$samplesPerChannel"
                            )
                            firstFrameLogged = true
                        }

                        // Delivery is handed straight back. Pacing and transmission belong to
                        // PacedAudioSender, which runs on its own thread. Sleeping here instead
                        // would only fill the queue, because capture and release would still
                        // share one thread and the read time would jitter the schedule.
                        // Deliver silent frames while muted so UDP timing is preserved.
                        val out = if (isMuted) silenceBuffer else frameBuffer
                        onAudioChunkCaptured(out, bytesReadTotal, isStereo, samplesPerChannel)
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Capture thread crashed", e)
                isRecording = false
                onCaptureError?.invoke("Microphone capture failed: ${e.localizedMessage ?: "unknown error"}")
            }
        }, "ZMic-CaptureThread").apply {
            // NORM_PRIORITY, not MAX_PRIORITY: the capture thread also performs the blocking UDP
            // send, and starving the AudioRecord reader causes hardware buffer overruns.
            priority = Thread.NORM_PRIORITY + 1
            start()
        }
    }

    /**
     * Stops capture and releases the hardware.
     *
     * Order matters: interrupt() does NOT unblock a native AudioRecord.read(), so we must
     * call stop() on the AudioRecord to wake the reader, THEN join, and only THEN release.
     * Releasing while the reader thread is still running is a use-after-free.
     */
    fun stop() {
        isRecording = false

        val record = audioRecord
        val thread = recordingThread
        recordingThread = null

        // 1. Unblock the reader thread (stop() makes the pending read return).
        try {
            if (record != null && record.recordingState == AudioRecord.RECORDSTATE_RECORDING) {
                record.stop()
            }
        } catch (e: Exception) {
            Log.w(TAG, "Error stopping AudioRecord", e)
        }

        // 2. Also interrupt, so a thread sitting in Thread.sleep() wakes promptly.
        try {
            thread?.interrupt()
        } catch (e: Exception) {
            Log.w(TAG, "Error interrupting capture thread", e)
        }

        // 3. Wait for the reader to finish touching `record`.
        if (thread != null && thread !== Thread.currentThread()) {
            try {
                thread.join(800)
                if (thread.isAlive) Log.w(TAG, "Capture thread did not exit within 800ms")
            } catch (e: InterruptedException) {
                Log.w(TAG, "Interrupted while joining capture thread", e)
                Thread.currentThread().interrupt()
            }
        }

        // 4. Only now is it safe to release the hardware.
        try {
            record?.release()
        } catch (e: Exception) {
            Log.w(TAG, "Error releasing AudioRecord", e)
        }
        audioRecord = null
    }

    companion object {
        private const val TAG = "ZMicAudioRecorder"
    }
}
