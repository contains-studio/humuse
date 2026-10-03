import asyncio

import pytest

from openpin_muse.controls import COMMANDS, PinControls


async def next_command(controls):
    for _ in range(20):
        command = await controls.poll({"battery": 0.5, "isCharging": False, "activity": "idle"})
        if command:
            return command
        await asyncio.sleep(0)
    raise AssertionError("Command was not queued")


async def test_command_waits_for_ack_and_repeated_delivery_is_identical():
    controls = PinControls()
    task = asyncio.create_task(controls.submit("set_volume", {"volume": 0.4}))
    command = await next_command(controls)
    assert command["name"] == "set_volume"
    assert command["params"] == {"volume": 0.4}
    assert not task.done()
    assert (await controls.poll())["id"] == command["id"]
    result = {"ok": True, "volume": 0.4}
    await controls.complete(command["id"], result)
    assert await task == result
    await controls.complete(command["id"], result)
    with pytest.raises(ValueError, match="conflict"):
        await controls.complete(command["id"], {"ok": False})
    assert await controls.poll() is None


@pytest.mark.parametrize("name,params", [
    ("system.run", {"command": "anything"}), ("set_volume", {"volume": True}),
    ("set_volume", {"volume": float("nan")}), ("set_volume", {"volume": 1.1}),
    ("set_volume", {}), ("record_video", {"duration_seconds": 16}),
    ("record_video", {"duration_seconds": 0}), ("record_video", {"duration_seconds": 1.5}),
    ("ring", {"shell": "anything"}), ("capture_photo", []),
])
async def test_only_bounded_allowlisted_commands(name, params):
    controls = PinControls()
    with pytest.raises(ValueError):
        await controls.submit(name, params)
    assert (await controls.snapshot())["pending"] == 0
    assert set(COMMANDS) == {"get_status", "ring", "set_volume", "capture_photo", "record_video"}


async def test_expired_command_is_never_delivered():
    controls = PinControls()
    result = await controls.submit("ring", {}, timeout_ms=10)
    assert result["ok"] is False
    assert result["delivered"] is False
    assert await controls.poll() is None


async def test_delivered_timeout_does_not_claim_hardware_did_not_act():
    controls = PinControls()
    task = asyncio.create_task(controls.submit("ring", {}, timeout_ms=20))
    command = await next_command(controls)
    result = await task
    assert result["ok"] is False and result["delivered"] is True
    assert "may have run" in result["error"]
    with pytest.raises(KeyError):
        await controls.complete(command["id"], {"ok": True})


async def test_cancel_removes_queued_work_and_shutdown_rejects_new_work():
    controls = PinControls()
    task = asyncio.create_task(controls.submit("ring", {}))
    await next_command(controls)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await controls.poll() is None
    task = asyncio.create_task(controls.submit("get_status", {}))
    await next_command(controls)
    await controls.close()
    assert (await task)["ok"] is False
    assert (await controls.submit("ring", {}))["ok"] is False


async def test_results_must_be_delivered_json_objects_with_boolean_ok():
    controls = PinControls()
    task = asyncio.create_task(controls.submit("get_status", {}))
    command = await next_command(controls)
    for result in ({}, {"ok": "true"}, {"ok": True, "x": float("inf")}, {"ok": True, "x": "x" * 17000}):
        with pytest.raises(ValueError):
            await controls.complete(command["id"], result)
    await controls.complete(command["id"], {"ok": True, "battery": 0.5})
    assert (await task)["battery"] == 0.5


async def test_snapshot_tracks_pin_heartbeat_without_claiming_hardware_online_before_poll():
    controls = PinControls()
    assert (await controls.snapshot())["online"] is False
    await controls.poll({"battery": 0.75, "isCharging": True, "activity": "idle"})
    snapshot = await controls.snapshot()
    assert snapshot["online"] is True
    assert snapshot["status"]["battery"] == 0.75
    with pytest.raises(ValueError):
        await controls.poll({"battery": 15})


async def test_sdk_thread_adapter_and_wrong_thread_rejection():
    controls = PinControls()
    controls.bind_loop()
    assert controls.run("ring", {}, 100)["ok"] is False
    task = asyncio.create_task(asyncio.to_thread(controls.run, "get_status", {}, 2000))
    command = await next_command(controls)
    await controls.complete(command["id"], {"ok": True})
    assert (await task)["ok"] is True


async def test_queue_limit_and_default_video_duration():
    controls = PinControls(max_pending=1)
    task = asyncio.create_task(controls.submit("record_video", {}))
    command = await next_command(controls)
    assert command["params"] == {"duration_seconds": 5}
    assert (await controls.submit("ring", {}))["ok"] is False
    await controls.complete(command["id"], {"ok": False, "error": "Camera busy"})
    assert (await task)["error"] == "Camera busy"


async def test_relink_cancels_old_commands_and_forgets_old_status():
    controls = PinControls()
    old = asyncio.create_task(controls.submit("ring", {}))
    delivered = await next_command(controls)
    await controls.reset()
    assert (await old)["ok"] is False
    assert (await controls.snapshot())["online"] is False
    assert (await controls.snapshot())["status"] == {}
    assert await controls.poll() is None
    with pytest.raises(KeyError):
        await controls.complete(delivered["id"], {"ok": True})
    fresh = asyncio.create_task(controls.submit("get_status", {}))
    delivered = await next_command(controls)
    await controls.complete(delivered["id"], {"ok": True})
    assert (await fresh)["ok"] is True


@pytest.mark.parametrize("reserved", ["id", "method", "params", "event", "command", "type"])
async def test_pin_ack_cannot_override_muse_rpc_envelope(reserved):
    controls = PinControls()
    pending = asyncio.create_task(controls.submit("ring", {}))
    await asyncio.sleep(0)
    command = await controls.poll()
    with pytest.raises(ValueError):
        await controls.complete(command["id"], {"ok": True, reserved: "injected"})
    assert not pending.done()
    await controls.complete(command["id"], {"ok": True})
    assert await pending == {"ok": True}
