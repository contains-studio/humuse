package org.openpin.primaryapp

import android.content.Context
import android.os.SystemClock
import android.util.Log
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.cancel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.openpin.appframework.daemonbridge.power.PowerHandler
import org.openpin.appframework.devicestate.battery.BatteryManager
import org.openpin.appframework.media.soundplayer.SoundPlayer
import org.openpin.appframework.media.soundplayer.SystemSound
import org.openpin.appframework.media.volume.VolumeManager
import org.openpin.primaryapp.backend.BackendManager
import org.openpin.primaryapp.backend.PinCommand
import java.io.Closeable

/** Poll only while OpenPin is foreground and awake. No command wakes the device. */
class RemoteCommandsManager(
    context: Context,
    private val backend: BackendManager,
    private val gestures: GestureManager,
    private val battery: BatteryManager,
    private val sound: SoundPlayer,
    private val volume: VolumeManager,
    private val power: PowerHandler
) : Closeable {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val journal = context.getSharedPreferences("pin_commands", Context.MODE_PRIVATE)
    private val gson = Gson()
    private val resultType = object : TypeToken<Map<String, Any>>() {}.type
    private var active = false
    private var awake = true
    private var pollJob: Job? = null
    private val powerSubscription = power.subscribePowerEvents { event ->
        awake = !event.sleeping
        updatePolling()
    }

    fun start() {
        active = true
        updatePolling()
    }

    fun stop() {
        active = false
        updatePolling()
    }

    private fun updatePolling() {
        if (!active || !awake) {
            pollJob?.cancel()
            return
        }
        // Finish the previous daemon request's cleanup before a new consumer.
        if (pollJob != null) return
        pollJob = scope.launch {
            try {
                while (isActive && active && awake) {
                    try {
                        if (backend.isPaired) {
                            val started = SystemClock.elapsedRealtime()
                            val command = backend.pollCommands(status())
                            currentCoroutineContext().ensureActive()
                            if (active && awake && command != null) {
                                handle(command, SystemClock.elapsedRealtime() - started)
                            }
                        }
                    } catch (cancelled: CancellationException) {
                        throw cancelled
                    } catch (_: Exception) {
                        // URLs and transport errors can contain device credentials.
                        Log.w("PinCommands", "Command poll failed; retrying while awake")
                    }
                    delay(2000)
                }
            } finally {
                pollJob = null
                if (active && awake && scope.isActive) {
                    scope.launch { updatePolling() }
                }
            }
        }
    }

    private fun status(): Map<String, Any> {
        val status = battery.status
        return mapOf(
            "battery" to (status.percentage.takeIf { it.isFinite() }?.coerceIn(0f, 1f) ?: 0f),
            "isCharging" to status.isCharging,
            "activity" to gestures.activity
        )
    }

    private suspend fun handle(command: PinCommand, pollDurationMs: Long) {
        require(command.id.matches(Regex("[A-Za-z0-9_-]{16,64}"))) { "Invalid command ID" }
        val previous = journal.getString(command.id, null)
        if (previous != null) {
            backend.acknowledgeCommand(command.id, gson.fromJson(previous, resultType))
            return
        }
        val remaining = commandTimeRemaining(command, pollDurationMs)
        if (remaining <= 0) {
            backend.acknowledgeCommand(command.id, mapOf("ok" to false, "error" to "Command expired"))
            return
        }
        val interrupted = mapOf<String, Any>(
            "ok" to false,
            "error" to "Pin interrupted the command; it may have run before interruption"
        )
        // Persist BEFORE touching hardware. A restart or lost ACK must not take
        // the same photo twice or restart a recording.
        saveResult(command.id, interrupted)
        val result = try {
            withTimeout(remaining) { execute(command) }
        } catch (_: TimeoutCancellationException) {
            mapOf("ok" to false, "error" to "Pin command timed out; it may have partially run")
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (error: IllegalArgumentException) {
            mapOf("ok" to false, "error" to (error.message ?: "Invalid Pin command"))
        } catch (error: IllegalStateException) {
            mapOf("ok" to false, "error" to (error.message ?: "Pin command failed"))
        } catch (_: Exception) {
            mapOf("ok" to false, "error" to "Pin could not complete the command")
        }
        saveResult(command.id, result)
        currentCoroutineContext().ensureActive()
        backend.acknowledgeCommand(command.id, result)
    }

    private suspend fun execute(command: PinCommand): Map<String, Any> {
        val params = command.params
        val allowed = when (command.name) {
            "set_volume" -> setOf("volume")
            "record_video" -> setOf("duration_seconds")
            "get_status", "ring", "capture_photo" -> emptySet()
            else -> throw IllegalArgumentException("Unsupported Pin command")
        }
        require(params.keySet().all { it in allowed }) { "Unsupported command parameter" }
        return when (command.name) {
            "get_status" -> status() + ("ok" to true)
            "ring" -> {
                check(gestures.activity == "idle") { "Pin is busy" }
                val stream = sound.play(SystemSound.LASER_FOCUS_BUTTON.key)
                check(stream != 0) { "Pin could not play its chime" }
                try { delay(1500) } finally { sound.stop(stream) }
                mapOf("ok" to true)
            }
            "set_volume" -> {
                val input = params.get("volume")
                require(input?.isJsonPrimitive == true && input.asJsonPrimitive.isNumber) {
                    "Volume must be a number between 0 and 1"
                }
                val level = input.asFloat
                require(level.isFinite() && level in 0f..1f) { "Volume must be between 0 and 1" }
                volume.setMasterVolume(level)
                mapOf("ok" to true, "volume" to level)
            }
            "capture_photo" -> gestures.captureRemotely()
            "record_video" -> {
                val input = params.get("duration_seconds")
                require(input == null || (input.isJsonPrimitive && input.asJsonPrimitive.isNumber)) {
                    "Video duration must be an integer from 1 to 15 seconds"
                }
                val seconds = input?.asDouble ?: 5.0
                require(seconds.isFinite() && seconds in 1.0..15.0 && seconds % 1.0 == 0.0) {
                    "Video duration must be an integer from 1 to 15 seconds"
                }
                gestures.captureRemotely(seconds.toInt())
            }
            else -> throw IllegalArgumentException("Unsupported Pin command")
        }
    }

    private suspend fun saveResult(id: String, result: Map<String, Any>) = withContext(Dispatchers.IO) {
        val order = journal.getString("_order", "")!!.split(',').filter { it.isNotEmpty() && it != id }
        val retained = (order + id).takeLast(128)
        val editor = journal.edit()
        (order - retained.toSet()).forEach { editor.remove(it) }
        editor.putString("_order", retained.joinToString(","))
        check(editor.putString(id, gson.toJson(result)).commit()) { "Unable to save Pin command receipt" }
    }

    override fun close() {
        active = false
        power.unsubscribe(powerSubscription)
        scope.cancel()
    }
}

/** The server supplies remaining TTL; a boxed Pin may have an incorrect wall clock. */
internal fun commandTimeRemaining(command: PinCommand, roundTripMillis: Long): Long {
    if (command.timeoutMs !in 1..45000) return 0
    return (command.timeoutMs - roundTripMillis.coerceAtLeast(0)).coerceAtLeast(0)
}
