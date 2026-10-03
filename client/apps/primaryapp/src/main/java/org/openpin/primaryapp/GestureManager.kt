package org.openpin.primaryapp

import org.openpin.primaryapp.gestureinterpreter.GestureInterpreter
import org.openpin.appframework.media.soundplayer.SoundPlayer
import android.net.Uri
import android.util.Log
import androidx.camera.video.Quality
import androidx.camera.video.QualitySelector
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.openpin.appframework.media.speechplayer.SpeechPlayer
import org.openpin.appframework.daemonbridge.gesture.GestureHandler
import org.openpin.appframework.daemonbridge.process.ProcessHandler
import org.openpin.appframework.media.soundplayer.SystemSound
import org.openpin.appframework.sensors.camera.CameraManager
import org.openpin.appframework.sensors.camera.ImageCaptureConfig
import org.openpin.appframework.sensors.camera.PostProcessConfig
import org.openpin.appframework.sensors.microphone.MicrophoneManager
import org.openpin.appframework.sensors.microphone.RecordSession
import org.openpin.appframework.media.soundplayer.withLoadingSounds
import org.openpin.appframework.sensors.camera.CaptureResult
import org.openpin.appframework.sensors.camera.CaptureSession
import org.openpin.appframework.sensors.camera.VideoCaptureConfig
import org.openpin.primaryapp.backend.BackendManager
import org.openpin.primaryapp.gestureinterpreter.InterpreterMode
import java.io.File

class GestureManager(
    private val processHandler: ProcessHandler,
    private val gestureHandler: GestureHandler,
    private val cameraManager: CameraManager,
    private val microphoneManager: MicrophoneManager,
    private val speechPlayer: SpeechPlayer,
    private val soundPlayer: SoundPlayer,
    private val backendManager: BackendManager
) : ViewModel() {
    private val visionCameraConfig = ImageCaptureConfig(
        jpegQuality = 20,
        postProcessConfig = PostProcessConfig(
            newWidth = 960,
            newHeight = 720,
        )
    )
    private val minVoiceInputLen = 1000

    private var speechCapture: RecordSession? = null
    private var videoCaptureSession: CaptureSession<Uri>? = null

    private var voiceInputStart: Long = 0

    private var settingsToggled = false
    private var onSettingsToggle: ((Boolean) -> Unit)? = null

    private enum class State {
        IDLE,
        PHOTO_CAPTURE,
        VIDEO_CAPTURE,
        VOICE_INPUT,
        VOICE_THINKING,
        VOICE_RESPONDING,
        PAIRING,
        SETTINGS_TOGGLE
    }
    private var state = State.IDLE

    val activity: String get() = state.name.lowercase()

    /** Reserve the shared camera while the link screen scans a QR code. */
    fun beginPairing(): Boolean {
        if (state != State.IDLE) return false
        state = State.PAIRING
        gestureInterpreter.setMode(InterpreterMode.DISABLED)
        return true
    }

    fun endPairing() {
        if (state == State.PAIRING) {
            state = State.IDLE
            gestureInterpreter.setMode(InterpreterMode.NORMAL)
        }
    }

    class CaptureException(s: String) : Exception(s)

    private val gestureInterpreter = GestureInterpreter(
        gestureHandler = gestureHandler,
        onCapturePhoto = {
            Log.w("org.openpin.primaryapp.gestureinterpreter.GestureInterpreter", "onTakePhoto")
            viewModelScope.launch {
                handleCapturePhoto()
            }
        },
        onCaptureVideoStart = {
            Log.w("org.openpin.primaryapp.gestureinterpreter.GestureInterpreter", "onTakeVideoStart")
            viewModelScope.launch {
                handleCaptureVideo()
            }
        },
        onAssistantStartAction = { useVision ->
            Log.w("org.openpin.primaryapp.gestureinterpreter.GestureInterpreter", "onStartAssistantVoiceInput: $useVision")
            viewModelScope.launch {
                handleStartVoiceInput(isTranslating = false)
            }
        },
        onAssistantStopAction = { useVision ->
            Log.w("org.openpin.primaryapp.gestureinterpreter.GestureInterpreter", "onStopAssistantVoiceInput: $useVision")
            viewModelScope.launch {
                handleEndVoiceInput(isTranslating = false, useVision = useVision)
            }
        },
        onTranslateStartAction = {
            Log.w("org.openpin.primaryapp.gestureinterpreter.GestureInterpreter", "onStartTranslateVoiceInput")
            viewModelScope.launch {
                handleStartVoiceInput(isTranslating = true)
            }
        },
        onTranslateStopAction = {
            Log.w("org.openpin.primaryapp.gestureinterpreter.GestureInterpreter", "onStopTranslateVoiceInput")
            viewModelScope.launch {
                handleEndVoiceInput(isTranslating = true, useVision = false)
            }
        },
        onCancelAction = {
            Log.w("org.openpin.primaryapp.gestureinterpreter.GestureInterpreter", "onCancelAction")
            viewModelScope.launch {
                handleCancel()
            }
        },
        scope = viewModelScope
    )

    fun addListeners() {
        gestureInterpreter.subscribeGestures()
    }

    fun enableSettingsToggle(onToggle: (Boolean) -> Unit) {
        gestureInterpreter.setMode(InterpreterMode.CANCELABLE)
        state = State.SETTINGS_TOGGLE

        settingsToggled = false
        onSettingsToggle = onToggle
    }

    fun disableSettingsToggle() {
        gestureInterpreter.setMode(InterpreterMode.NORMAL)
        state = State.IDLE
        onSettingsToggle = null
    }

    private suspend fun handleCapturePhoto() {
        if (state != State.IDLE) return
        runIfPaired {
            gestureInterpreter.setMode(InterpreterMode.DISABLED)
            state = State.PHOTO_CAPTURE

            val imgFile = processHandler.createTempFile("jpeg")
            try {
                // Capture image
                val result = cameraManager.captureImage(imgFile)

                when (result) {
                    is CaptureResult.Success -> {
                        soundPlayer.play(SystemSound.SHUTTER.key)
                    }
                    else -> {
                        soundPlayer.play(SystemSound.CAPTURE_FAILED.key)
                        throw CaptureException("Image capture failed")
                    }
                }

                // Upload image
                backendManager.sendUploadRequest(imgFile)

            } catch (err: Exception) {
                Log.e("Assistant", "Failed to complete image capture: ${err.message}")

                if (err !is CaptureException) {
                    soundPlayer.play(SystemSound.FAILED.key)
                }
            } finally {
                imgFile.delete()

                gestureInterpreter.setMode(InterpreterMode.NORMAL)
                state = State.IDLE
            }
        }
    }

    private suspend fun handleCaptureVideo() {
        if (state != State.IDLE) return
        runIfPaired {
            gestureInterpreter.setMode(InterpreterMode.CANCELABLE)
            state = State.VIDEO_CAPTURE

            soundPlayer.play(SystemSound.VIDEO_START.key)

            val videoFile = processHandler.createTempFile("mp4")
            try {
                // Start video capture with a 15-second maximum
                videoCaptureSession = cameraManager.captureVideo(
                    outputFile = videoFile,
                    duration = 15000L,
                    captureConfig = null
                )

                // Wait for the video capture to complete (even if stopped early)
                val result = videoCaptureSession?.waitForResult()

                when (result) {
                    is CaptureResult.Success -> {
                        soundPlayer.play(SystemSound.VIDEO_END.key)
                    }
                    else -> {
                        soundPlayer.play(SystemSound.CAPTURE_FAILED.key)
                        throw CaptureException("Image capture failed")
                    }
                }

                // Upload video
                backendManager.sendUploadRequest(videoFile)

            } catch (err: Exception) {
                Log.e("Assistant", "Failed to complete video capture: ${err.message}")

                if (err !is CaptureException) {
                    soundPlayer.play(SystemSound.FAILED.key)
                }
            } finally {
                videoCaptureSession?.stop()
                videoCaptureSession = null
                videoFile.delete()

                gestureInterpreter.setMode(InterpreterMode.NORMAL)
                state = State.IDLE
            }
        }
    }

    private fun handleStartVoiceInput(isTranslating: Boolean) {
        if (state != State.IDLE) return
        runIfPaired {
            gestureInterpreter.setMode(InterpreterMode.DISABLED)
            state = State.VOICE_INPUT

            voiceInputStart = System.currentTimeMillis()

            if (isTranslating) {
                soundPlayer.play(SystemSound.TRANSLATE_START.key)
            } else {
                soundPlayer.play(SystemSound.ASSISTANT_START.key)
            }

            val speechFile = processHandler.createTempFile("ogg")
            speechCapture = microphoneManager.recordAudio(speechFile)
        }
    }

    private suspend fun handleEndVoiceInput(isTranslating: Boolean, useVision: Boolean) {
        // We need to check this for edge cases
        // Ex: if not paired voice input will not start, but this will still get called
        if (state != State.VOICE_INPUT)
            return

        speechCapture?.stop()
        soundPlayer.play(SystemSound.INPUT_END.key)

        if (System.currentTimeMillis() - voiceInputStart < minVoiceInputLen) {
            discardSpeech()
            gestureInterpreter.setMode(InterpreterMode.NORMAL)
            state = State.IDLE
            return
        }

        state = State.VOICE_THINKING

        var imgFile: File? = null
        if (useVision) {
            delay(300)
            soundPlayer.play(SystemSound.VISION.key)

            imgFile = processHandler.createTempFile("jpg")
            cameraManager.captureImage(imgFile, visionCameraConfig)
        }

        val endpoint = if (isTranslating) "translate" else "handle"

        speechCapture?.let { capture ->
            try {
                val res = withLoadingSounds(soundPlayer) {
                    backendManager.sendVoiceRequest(endpoint, capture.result, imgFile)
                }

                res?.let {
                    gestureInterpreter.setMode(InterpreterMode.CANCELABLE)
                    state = State.VOICE_RESPONDING

                    speechPlayer.play(it)
                    speechPlayer.awaitPlaybackCompletion()
                }
            } catch (err: Exception) {
                Log.e("Assistant", "Failed to complete voice request: ${err.message}")
                soundPlayer.play(SystemSound.FAILED.key)
            } finally {
                imgFile?.delete()
                discardSpeech()
            }
        }

        gestureInterpreter.setMode(InterpreterMode.NORMAL)
        state = State.IDLE
    }

    private fun handleCancel() {
        when (state) {
            State.VIDEO_CAPTURE -> {
                videoCaptureSession?.stop()
            }
            State.VOICE_RESPONDING -> {
                speechPlayer.stop()
            }
            State.SETTINGS_TOGGLE -> {
                settingsToggled = !settingsToggled
                onSettingsToggle?.invoke(settingsToggled)
            }

            else -> Unit
        }
    }

    private inline fun runIfPaired(action: () -> Unit) {
        if (backendManager.isPaired) {
            action()
        } else {
            soundPlayer.play(SystemSound.FAILED.key)
        }
    }

    private fun discardSpeech() {
        speechCapture?.let {
            it.stop()
            it.result.delete()
            speechCapture = null
        }
    }

    /** Runs on the main dispatcher, sharing the same state gate as local gestures. */
    suspend fun captureRemotely(durationSeconds: Int? = null): Map<String, Any> {
        check(backendManager.isPaired) { "Pin is not linked" }
        check(state == State.IDLE) { "Pin is busy; finish the current interaction first" }
        require(durationSeconds == null || durationSeconds in 1..15)
        val file = processHandler.createTempFile(if (durationSeconds == null) "jpg" else "mp4")
        state = if (durationSeconds == null) State.PHOTO_CAPTURE else State.VIDEO_CAPTURE
        gestureInterpreter.setMode(
            if (durationSeconds == null) InterpreterMode.DISABLED else InterpreterMode.CANCELABLE
        )
        try {
            val result = if (durationSeconds == null) {
                // Audible notice precedes any remotely initiated camera capture.
                soundPlayer.play(SystemSound.VISION.key)
                delay(300)
                cameraManager.captureImage(file, ImageCaptureConfig(
                    jpegQuality = 85,
                    postProcessConfig = PostProcessConfig(newWidth = 1920, newHeight = 1440)
                ))
            } else {
                soundPlayer.play(SystemSound.VIDEO_START.key)
                delay(300)
                videoCaptureSession = cameraManager.captureVideo(
                    file, durationSeconds * 1000L,
                    VideoCaptureConfig(qualitySelector = QualitySelector.from(Quality.SD))
                )
                videoCaptureSession!!.waitForResult()
            }
            check(result is CaptureResult.Success) { "Camera capture failed" }
            soundPlayer.play(
                if (durationSeconds == null) SystemSound.SHUTTER.key else SystemSound.VIDEO_END.key
            )
            check(file.length() in 1..8L * 1024 * 1024) { "Capture exceeded the 8 MiB upload limit" }
            val uploaded = backendManager.sendUploadRequest(file)
            val captureId = uploaded.get("captureId")?.asString
                ?: throw IllegalStateException("Capture upload was not acknowledged")
            return mapOf("ok" to true, "captureId" to captureId)
        } finally {
            videoCaptureSession?.stop()
            videoCaptureSession = null
            file.delete()
            gestureInterpreter.setMode(InterpreterMode.NORMAL)
            state = State.IDLE
        }
    }

    override fun onCleared() {
        super.onCleared()

        gestureInterpreter.clear()
        discardSpeech()
    }
}
