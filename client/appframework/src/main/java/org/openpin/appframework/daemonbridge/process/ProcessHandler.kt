package org.openpin.appframework.daemonbridge.process

import android.os.Bundle
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.CancellableContinuation
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import org.openpin.appframework.daemonbridge.manager.DaemonFileSystem
import org.openpin.appframework.daemonbridge.manager.DaemonIntentReceiver
import java.io.Closeable
import java.io.File
import java.io.FileWriter
import java.io.IOException
import java.util.UUID
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

class ProcessHandler : DaemonIntentReceiver, Closeable {

    private lateinit var fileSystem: DaemonFileSystem
    private lateinit var activeProcessesFile: File
    private val activeProcesses = mutableSetOf<String>()
    private val processLock = Any()

    private data class WaitingProcess(
        val process: ShellProcess,
        val continuation: CancellableContinuation<ShellProcess>
    )

    private val waiting = mutableMapOf<String, WaitingProcess>()

    override fun setFileSystem(fileSystem: DaemonFileSystem) {
        this.fileSystem = fileSystem
        activeProcessesFile = fileSystem.get("active-processes.txt")
    }

    override fun onReceive(extras: Bundle?) {
        val pid = extras?.getString("pid") ?: return
        val entry = synchronized(processLock) { waiting.remove(pid) }

        if (entry == null) {
            // Cancelled requests may still send a completion broadcast.
            return
        }

        val outFile = fileSystem.get("processes/$pid-out.txt")
        val errFile = fileSystem.get("processes/$pid-err.txt")

        try {
            entry.process.output = outFile.takeIf { it.exists() }?.readText().orEmpty()
            entry.process.error = errFile.takeIf { it.exists() }?.readText().orEmpty()
        } catch (error: IOException) {
            entry.continuation.resumeWithException(error)
            return
        }

        entry.continuation.resume(entry.process)
    }

    fun createTempFile(ext: String): File {
        val fid = UUID.randomUUID().toString()
        val file = fileSystem.get("processes/$fid.$ext")
        file.parentFile?.mkdirs()
        file.createNewFile()
        return file
    }

    suspend fun execute(process: ShellProcess): ShellProcess = withContext(Dispatchers.IO) {
        val pid = UUID.randomUUID().toString()
        process.pid = pid

        val cmdFile = fileSystem.get("processes/$pid-cmd.txt")
        cmdFile.parentFile?.mkdirs()

        try {
            FileWriter(cmdFile).use { it.write(process.command) }
        } catch (e: IOException) {
            throw e
        }

        suspendCancellableCoroutine { cont ->
            synchronized(processLock) {
                // Register before publishing the PID; a fast daemon response must
                // never arrive before the continuation exists.
                waiting[pid] = WaitingProcess(process, cont)
                cont.invokeOnCancellation {
                    synchronized(processLock) {
                        waiting.remove(pid)
                        activeProcesses.remove(pid)
                        updateActiveProcessesFile()
                    }
                }
                if (cont.isActive) {
                    activeProcesses.add(pid)
                    if (!updateActiveProcessesFile()) {
                        waiting.remove(pid)
                        activeProcesses.remove(pid)
                        cont.resumeWithException(IOException("Unable to publish daemon request"))
                    }
                }
            }
        }
    }

    fun release(process: ShellProcess) {
        synchronized(processLock) {
            activeProcesses.remove(process.pid)
            updateActiveProcessesFile()
        }
    }

    override fun close() {
        val pending = synchronized(processLock) {
            val pending = waiting.values.toList()
            waiting.clear()
            activeProcesses.clear()
            updateActiveProcessesFile()
            pending
        }
        pending.forEach { it.continuation.cancel() }
    }

    private fun updateActiveProcessesFile(): Boolean {
        try {
            // Never expose a half-written set, which could release other requests.
            val temporary = File(activeProcessesFile.parentFile, "active-processes.tmp")
            FileWriter(temporary, false).use { writer ->
                activeProcesses.forEach { writer.write("$it\n") }
            }
            if (!temporary.renameTo(activeProcessesFile)) {
                throw IOException("Unable to replace active process list")
            }
            return true
        } catch (e: IOException) {
            Log.e("ProcessHandler", "Failed to update active-processes.txt", e)
            return false
        }
    }
}
