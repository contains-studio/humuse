package org.openpin.primaryapp

import com.google.gson.JsonObject
import org.junit.Assert.assertEquals
import org.junit.Test
import org.openpin.primaryapp.backend.PinCommand

class RemoteCommandDeadlineTest {
    private fun command(expiry: Long, timeout: Long = 30000) =
        PinCommand("test-command", "ring", JsonObject(), expiry, timeout)

    @Test fun freshCommandSurvivesIncorrectPinWallClock() {
        // Both a past and a future wall-clock deadline represent the same fresh
        // server TTL. Only elapsed round-trip time reduces the action budget.
        assertEquals(29500L, commandTimeRemaining(command(1), 500))
        assertEquals(29500L, commandTimeRemaining(command(Long.MAX_VALUE), 500))
    }

    @Test fun delayedResponseCannotExecuteAnExpiredCommand() {
        assertEquals(0L, commandTimeRemaining(command(Long.MAX_VALUE), 30001))
    }

    @Test fun invalidServerTimeoutCannotExpandTheActionBudget() {
        assertEquals(0L, commandTimeRemaining(command(Long.MAX_VALUE, 45001), 0))
        assertEquals(0L, commandTimeRemaining(command(Long.MAX_VALUE, 0), 0))
    }
}
