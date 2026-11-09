import random
import tempfile
import threading
import time
import unittest
from pathlib import Path

import json

from fleet.protocol import (UPDATE_STATUSES, Command, CommandType, Heartbeat, StatusCode,
                            UpdateResult, command_message, hello_message, parse_command_message,
                            parse_hello)
from fleet.outage_scheduler import OutageScheduler
from fleet.robot.agent import BATCH_SIZE, Agent
from fleet.robot.command_channel import CommandChannel
from fleet.robot.controller import Controller, ControllerConfig
from fleet.robot.firmware_slots import FirmwareSlots
from fleet.robot.firmware_updater import FirmwareUpdater, UpdateConfig
from fleet.robot.network import SimulatedLink
from fleet.robot.state import RobotState
from fleet.robot.telemetry_buffer import TelemetryBuffer
from fleet.server.app import MAX_BATCH_SIZE, ServerApp, UpdateConflict
from fleet.server.connections import ConnectionRegistry, RobotNotConnected
from fleet.server.firmware import FirmwareCatalog, make_artifact
from fleet.server.heartbeat_log import HeartbeatLog
from fleet.server.store import HeartbeatStore


class FakeConnection:
    def __init__(self, fail=False):
        self.sent = []
        self.closed = False
        self.fail = fail

    def send(self, message):
        if self.fail:
            raise ConnectionError("closed")
        self.sent.append(json.loads(message))

    def close(self):
        self.closed = True


def valid_payload(**overrides):
    payload = {"robot_id": "arm-001", "seq": 1, "firmware_version": "1.0.0",
               "battery_level": 80.0, "status_code": 1, "sent_at": 1000.0}
    payload.update(overrides)
    return payload


class ProtocolTests(unittest.TestCase):
    def test_round_trip(self):
        hb = Heartbeat.from_dict(valid_payload())
        self.assertEqual(Heartbeat.from_dict(hb.to_dict()), hb)

    def test_rejects_bad_input(self):
        bad = [
            "not a dict",
            valid_payload(robot_id=""),
            valid_payload(battery_level=101),
            valid_payload(status_code=99),
            valid_payload(seq=True),
        ]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                Heartbeat.from_dict(payload)

    def test_rejects_missing_field(self):
        payload = valid_payload()
        del payload["firmware_version"]
        with self.assertRaises(ValueError):
            Heartbeat.from_dict(payload)

    def test_firmware_fields_are_optional_for_older_robots(self):
        hb = Heartbeat.from_dict(valid_payload())  # no slot fields at all
        self.assertEqual((hb.active_slot, hb.inactive_slot_version, hb.last_update_result),
                         ("A", None, "none"))

    def test_firmware_fields_round_trip(self):
        hb = Heartbeat.from_dict(valid_payload(
            active_slot="B", inactive_slot_version="1.0.0", last_update_result="rolled_back"))
        self.assertEqual((hb.active_slot, hb.inactive_slot_version, hb.last_update_result),
                         ("B", "1.0.0", "rolled_back"))
        self.assertEqual(Heartbeat.from_dict(hb.to_dict()), hb)

    def test_rejects_bad_firmware_fields(self):
        for overrides in [{"active_slot": "C"}, {"last_update_result": "weird"},
                          {"inactive_slot_version": 5}]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                Heartbeat.from_dict(valid_payload(**overrides))

    def test_all_update_statuses_are_valid_status_codes(self):
        for status in UPDATE_STATUSES:
            Heartbeat.from_dict(valid_payload(status_code=int(status)))


class CommandProtocolTests(unittest.TestCase):
    def test_command_round_trip(self):
        commands = [Command(CommandType.PAUSE), Command(CommandType.RESUME),
                    Command(CommandType.UPDATE_FIRMWARE, "2.0.0", "abc123")]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(parse_command_message(command_message(command)), command)

    def test_rejects_bad_command(self):
        for data in ["x", {"type": "hello"}, {"type": "command", "command": "explode"},
                     {"type": "command", "command": "pause", "version": 5}]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_command_message(data)

    def test_update_command_requires_version_and_checksum(self):
        with self.assertRaises(ValueError):
            Command(CommandType.UPDATE_FIRMWARE)
        with self.assertRaises(ValueError):
            parse_command_message({"type": "command", "command": "update_firmware",
                                   "version": "2.0.0"})  # no sha256

    def test_hello_round_trip_and_rejection(self):
        self.assertEqual(parse_hello(hello_message("arm-001")), "arm-001")
        for data in [{}, {"type": "hello"}, {"type": "hello", "robot_id": ""}]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_hello(data)


class ControllerTests(unittest.TestCase):
    def make(self, battery, status, **config):
        state = RobotState("1.0.0", battery, status)
        return state, Controller(state, random.Random(0), ControllerConfig(**config))

    def test_running_drains_battery(self):
        state, controller = self.make(
            50.0, StatusCode.RUNNING, fault_rate_per_s=0, finish_job_rate_per_s=0)
        controller.step(1.0)
        self.assertAlmostEqual(state.snapshot().battery_level, 49.0)

    def test_low_battery_triggers_charging(self):
        state, controller = self.make(
            15.5, StatusCode.RUNNING, fault_rate_per_s=0, finish_job_rate_per_s=0)
        controller.step(1.0)
        self.assertEqual(state.snapshot().status_code, StatusCode.CHARGING)

    def test_charging_finishes_at_full(self):
        state, controller = self.make(99.0, StatusCode.CHARGING)
        controller.step(1.0)
        snap = state.snapshot()
        self.assertEqual((snap.battery_level, snap.status_code), (100.0, StatusCode.IDLE))

    def test_fault_recovers_after_duration(self):
        state, controller = self.make(
            80.0, StatusCode.RUNNING, fault_rate_per_s=1000, fault_duration_s=2.0)
        controller.step(0.1)
        self.assertEqual(state.snapshot().status_code, StatusCode.ERROR)
        controller.step(2.0)
        self.assertEqual(state.snapshot().status_code, StatusCode.IDLE)


class PauseTests(unittest.TestCase):
    def make(self):
        state = RobotState("1.0.0", 50.0, StatusCode.RUNNING)
        # Rates of 0 for random events so only pause/resume affect behaviour.
        config = ControllerConfig(fault_rate_per_s=0, finish_job_rate_per_s=0)
        return state, Controller(state, random.Random(0), config)

    def test_paused_robot_freezes_battery_and_reports_paused(self):
        state, controller = self.make()
        state.set_paused(True)
        controller.step(1.0)
        controller.step(1.0)
        snap = state.snapshot()
        self.assertEqual(snap.status_code, StatusCode.PAUSED)
        self.assertEqual(snap.battery_level, 50.0)

    def test_resume_returns_to_idle_and_works_again(self):
        state, controller = self.make()
        state.set_paused(True)
        controller.step(1.0)
        state.set_paused(False)
        controller.step(1.0)
        self.assertNotEqual(state.snapshot().status_code, StatusCode.PAUSED)


class FakeUpdater:
    def __init__(self):
        self.requests = []

    def request_update(self, version, sha256):
        self.requests.append((version, sha256))
        return True


class FirmwarePhaseTests(unittest.TestCase):
    def make(self):
        state = RobotState("1.0.0", 50.0, StatusCode.RUNNING)
        config = ControllerConfig(fault_rate_per_s=0, finish_job_rate_per_s=0)
        return state, Controller(state, random.Random(0), config)

    def test_update_phase_is_reported_and_freezes_battery(self):
        state, controller = self.make()
        for phase in UPDATE_STATUSES:
            state.set_update_phase(phase)
            controller.step(1.0)
            snap = state.snapshot()
            self.assertEqual((snap.status_code, snap.battery_level), (phase, 50.0))

    def test_update_takes_precedence_over_pause(self):
        state, controller = self.make()
        state.set_paused(True)
        state.set_update_phase(StatusCode.ROLLING_BACK)
        controller.step(1.0)
        self.assertEqual(state.snapshot().status_code, StatusCode.ROLLING_BACK)

    def test_robot_returns_to_service_when_update_finishes(self):
        state, controller = self.make()
        state.set_update_phase(StatusCode.TRIAL_BOOT)
        controller.step(1.0)
        state.set_update_phase(None)
        controller.step(1.0)
        self.assertNotIn(state.snapshot().status_code, UPDATE_STATUSES)

    def test_paused_flag_applies_again_after_update(self):
        state, controller = self.make()
        state.set_paused(True)
        state.set_update_phase(StatusCode.DOWNLOADING)
        controller.step(1.0)
        state.set_update_phase(None)
        controller.step(1.0)
        self.assertEqual(state.snapshot().status_code, StatusCode.PAUSED)


class FirmwareSlotsTests(unittest.TestCase):
    def test_starts_on_slot_a_with_empty_standby(self):
        slots = FirmwareSlots("1.0.0")
        self.assertEqual((slots.active_slot, slots.active_version, slots.inactive_version),
                         ("A", "1.0.0", None))

    def test_stage_writes_inactive_slot_and_leaves_active_untouched(self):
        slots = FirmwareSlots("1.0.0")
        slots.stage("2.0.0")
        self.assertEqual((slots.active_slot, slots.active_version), ("A", "1.0.0"))
        self.assertEqual(slots.inactive_version, "2.0.0")

    def test_trial_boot_then_commit_keeps_new_firmware_and_old_as_fallback(self):
        slots = FirmwareSlots("1.0.0")
        slots.stage("2.0.0")
        slots.boot_into_staged()
        self.assertEqual((slots.active_slot, slots.active_version, slots.in_trial),
                         ("B", "2.0.0", True))
        slots.commit()
        self.assertEqual((slots.active_slot, slots.active_version, slots.in_trial),
                         ("B", "2.0.0", False))
        self.assertEqual(slots.inactive_version, "1.0.0")

    def test_rollback_restores_previous_firmware(self):
        slots = FirmwareSlots("1.0.0")
        slots.stage("2.0.0")
        slots.boot_into_staged()
        slots.rollback()
        self.assertEqual((slots.active_slot, slots.active_version, slots.in_trial),
                         ("A", "1.0.0", False))
        self.assertEqual(slots.inactive_version, "2.0.0")  # bad image stays until overwritten

    def test_second_update_alternates_back_to_slot_a(self):
        slots = FirmwareSlots("1.0.0")
        slots.stage("2.0.0"); slots.boot_into_staged(); slots.commit()
        slots.stage("3.0.0"); slots.boot_into_staged(); slots.commit()
        self.assertEqual((slots.active_slot, slots.active_version, slots.inactive_version),
                         ("A", "3.0.0", "2.0.0"))

    def test_invalid_transitions_raise(self):
        slots = FirmwareSlots("1.0.0")
        with self.assertRaises(RuntimeError):
            slots.boot_into_staged()  # nothing staged
        with self.assertRaises(RuntimeError):
            slots.commit()  # no trial
        with self.assertRaises(RuntimeError):
            slots.rollback()  # no trial
        slots.stage("2.0.0")
        slots.boot_into_staged()
        with self.assertRaises(RuntimeError):
            slots.stage("3.0.0")  # can't write mid-trial


class FirmwareUpdaterTests(unittest.TestCase):
    URL = "http://x/api/firmware"

    def make(self, *, success=1.0, fetch=None, stop=None):
        self.artifact = make_artifact("2.0.0", size=1024)
        self.state = RobotState("1.0.0")
        self.slots = FirmwareSlots("1.0.0")
        self.fetched = []

        def default_fetch(url):
            self.fetched.append(url)
            return self.artifact.payload

        self.phases = []
        original = self.state.set_update_phase
        self.state.set_update_phase = lambda p: (self.phases.append(p), original(p))[1]

        config = UpdateConfig(download_s=0, reboot_s=0, trial_s=0, rollback_s=0,
                              success_probability=success)
        return FirmwareUpdater("arm-001", self.state, self.slots, fetch or default_fetch,
                               self.URL, random.Random(0), stop or threading.Event(), config)

    def wait_until_finished(self):
        deadline = time.time() + 3
        while time.time() < deadline:
            snap = self.state.snapshot()
            if snap.last_update_result != UpdateResult.NONE and snap.update_phase is None:
                return
            time.sleep(0.005)
        self.fail("update did not finish")

    def test_successful_update_commits_new_firmware(self):
        updater = self.make(success=1.0)
        result = updater.run_update("2.0.0", self.artifact.sha256)
        snap = self.state.snapshot()
        self.assertEqual(result, UpdateResult.COMMITTED)
        self.assertEqual((snap.firmware_version, snap.active_slot, snap.inactive_slot_version),
                         ("2.0.0", "B", "1.0.0"))
        self.assertEqual((snap.last_update_result, snap.update_phase),
                         (UpdateResult.COMMITTED, None))
        self.assertEqual(self.fetched, [f"{self.URL}/2.0.0"])

    def test_successful_update_reports_each_phase_in_order(self):
        updater = self.make(success=1.0)
        updater.run_update("2.0.0", self.artifact.sha256)
        self.assertEqual(self.phases, [StatusCode.DOWNLOADING, StatusCode.REBOOTING,
                                       StatusCode.TRIAL_BOOT, None])

    def test_failed_trial_rolls_back_to_previous_firmware(self):
        updater = self.make(success=0.0)
        result = updater.run_update("2.0.0", self.artifact.sha256)
        snap = self.state.snapshot()
        self.assertEqual(result, UpdateResult.ROLLED_BACK)
        self.assertEqual((snap.firmware_version, snap.active_slot), ("1.0.0", "A"))
        self.assertEqual(snap.last_update_result, UpdateResult.ROLLED_BACK)
        self.assertEqual(self.phases, [StatusCode.DOWNLOADING, StatusCode.REBOOTING,
                                       StatusCode.TRIAL_BOOT, StatusCode.ROLLING_BACK, None])

    def test_new_firmware_runs_during_trial_then_old_is_restored_on_rollback(self):
        updater = self.make(success=0.0)
        versions_by_phase = {}
        original = self.state.set_update_phase

        def record(phase):
            versions_by_phase[phase] = self.state.snapshot().firmware_version
            original(phase)
        self.state.set_update_phase = record
        updater.run_update("2.0.0", self.artifact.sha256)
        # Old firmware while downloading/rebooting; new firmware booted for the trial;
        # old firmware again once rolled back.
        self.assertEqual(versions_by_phase[StatusCode.REBOOTING], "1.0.0")
        self.assertEqual(versions_by_phase[StatusCode.ROLLING_BACK], "2.0.0")
        self.assertEqual(self.state.snapshot().firmware_version, "1.0.0")

    def test_fetch_failure_leaves_slots_untouched(self):
        def broken(url):
            raise ConnectionError("network link is down")
        updater = self.make(fetch=broken)
        result = updater.run_update("2.0.0", self.artifact.sha256)
        snap = self.state.snapshot()
        self.assertEqual(result, UpdateResult.DOWNLOAD_FAILED)
        self.assertEqual((snap.firmware_version, snap.active_slot, snap.inactive_slot_version),
                         ("1.0.0", "A", None))
        self.assertEqual(self.phases, [StatusCode.DOWNLOADING, None])

    def test_checksum_mismatch_is_rejected(self):
        updater = self.make(fetch=lambda url: b"corrupted artifact")
        result = updater.run_update("2.0.0", self.artifact.sha256)
        self.assertEqual(result, UpdateResult.DOWNLOAD_FAILED)
        self.assertIsNone(self.state.snapshot().inactive_slot_version)

    def test_shutdown_mid_update_aborts_without_changing_firmware(self):
        stop = threading.Event()
        stop.set()
        updater = self.make(stop=stop)
        self.assertIsNone(updater.run_update("2.0.0", self.artifact.sha256))
        snap = self.state.snapshot()
        self.assertEqual((snap.firmware_version, snap.last_update_result),
                         ("1.0.0", UpdateResult.NONE))

    def test_request_update_runs_in_background_and_rejects_concurrent_requests(self):
        started, release = threading.Event(), threading.Event()
        artifact = make_artifact("2.0.0", size=1024)

        def slow_fetch(url):
            started.set()
            release.wait(3)
            return artifact.payload

        updater = self.make(success=1.0, fetch=slow_fetch)
        self.assertTrue(updater.request_update("2.0.0", self.artifact.sha256))
        self.assertTrue(started.wait(2))
        self.assertFalse(updater.request_update("2.0.0", self.artifact.sha256))  # busy
        release.set()
        self.wait_until_finished()
        self.assertEqual(self.state.snapshot().firmware_version, "2.0.0")

    def test_request_update_to_current_version_is_ignored(self):
        updater = self.make()
        self.assertFalse(updater.request_update("1.0.0", "whatever"))
        self.assertEqual(self.phases, [])  # nothing started


class CommandChannelTests(unittest.TestCase):
    def make(self):
        state = RobotState("1.0.0")
        updater = FakeUpdater()
        channel = CommandChannel("arm-001", state, "ws://x", connect=None, updater=updater)
        return state, channel, updater

    def test_pause_and_resume_messages_update_state(self):
        state, channel, _ = self.make()
        channel.handle_message(json.dumps(command_message(Command(CommandType.PAUSE))))
        self.assertTrue(state.snapshot().paused)
        channel.handle_message(json.dumps(command_message(Command(CommandType.RESUME))))
        self.assertFalse(state.snapshot().paused)

    def test_update_command_is_handed_to_the_updater(self):
        _, channel, updater = self.make()
        command = Command(CommandType.UPDATE_FIRMWARE, "2.0.0", "abc123")
        channel.handle_message(json.dumps(command_message(command)))
        self.assertEqual(updater.requests, [("2.0.0", "abc123")])

    def test_bad_messages_are_ignored(self):
        state, channel, updater = self.make()
        bad = ["not json", json.dumps({"type": "command", "command": "explode"}),
               json.dumps({"type": "command", "command": "update_firmware"})]  # missing params
        for raw in bad:
            channel.handle_message(raw)  # must not raise
        self.assertFalse(state.snapshot().paused)
        self.assertEqual(updater.requests, [])


class ConnectionRegistryTests(unittest.TestCase):
    def test_send_delivers_to_registered_robot(self):
        registry, conn = ConnectionRegistry(), FakeConnection()
        registry.register("arm-001", conn)
        registry.send("arm-001", {"hello": 1})
        self.assertEqual(conn.sent, [{"hello": 1}])

    def test_send_to_unknown_robot_raises(self):
        with self.assertRaises(RobotNotConnected):
            ConnectionRegistry().send("nope", {})

    def test_failed_send_unregisters_robot(self):
        registry = ConnectionRegistry()
        registry.register("arm-001", FakeConnection(fail=True))
        with self.assertRaises(RobotNotConnected):
            registry.send("arm-001", {})
        self.assertEqual(registry.connected_ids(), set())

    def test_reconnect_closes_old_and_stale_unregister_is_ignored(self):
        registry, old, new = ConnectionRegistry(), FakeConnection(), FakeConnection()
        registry.register("arm-001", old)
        registry.register("arm-001", new)
        self.assertTrue(old.closed)
        registry.unregister("arm-001", old)  # late cleanup of the old socket
        self.assertEqual(registry.connected_ids(), {"arm-001"})


class AgentTests(unittest.TestCase):
    def make_agent(self, sender, buffer=None):
        state = RobotState("1.2.0", 77.77, StatusCode.RUNNING)
        return Agent("arm-007", state, "http://x/api/heartbeats", 1.0, sender,
                     random.Random(0), buffer)

    @staticmethod
    def seqs(requests):
        return [h["seq"] for payload in requests for h in payload["heartbeats"]]

    def test_sends_snapshot_with_incrementing_seq(self):
        sent = []
        agent = self.make_agent(lambda url, payload: sent.append(payload))
        agent.tick()
        agent.tick()
        self.assertEqual(self.seqs(sent), [1, 2])
        first = sent[0]["heartbeats"][0]
        self.assertEqual(first["firmware_version"], "1.2.0")
        self.assertEqual(first["battery_level"], 77.8)
        self.assertEqual(first["status_code"], int(StatusCode.RUNNING))

    def test_survives_send_failure_and_keeps_telemetry(self):
        buffer = TelemetryBuffer()
        def failing(url, payload):
            raise ConnectionError("down")
        agent = self.make_agent(failing, buffer)
        self.assertFalse(agent.tick())
        self.assertEqual(len(buffer), 1)

    def test_backlog_is_delivered_in_order_once_connection_returns(self):
        sent, online = [], [False]
        def sender(url, payload):
            if not online[0]:
                raise ConnectionError("down")
            sent.append(payload)

        buffer = TelemetryBuffer()
        agent = self.make_agent(sender, buffer)
        for _ in range(3):  # three intervals offline
            agent.tick()
        self.assertEqual((sent, len(buffer)), ([], 3))

        online[0] = True
        self.assertTrue(agent.tick())  # reconnects: backlog + the new sample, one burst
        self.assertEqual(self.seqs(sent), [1, 2, 3, 4])
        self.assertEqual(len(buffer), 0)

    def test_buffered_heartbeats_keep_their_original_timestamps(self):
        sent, online = [], [False]
        def sender(url, payload):
            if not online[0]:
                raise ConnectionError("down")
            sent.append(payload)

        agent = self.make_agent(sender)
        agent.tick()
        sampled_at = agent._buffer.peek(1)[0].sent_at
        time.sleep(0.02)
        online[0] = True
        agent.tick()
        self.assertEqual(sent[0]["heartbeats"][0]["sent_at"], sampled_at)
        self.assertLess(sampled_at, sent[0]["heartbeats"][1]["sent_at"])

    def test_large_backlog_is_split_into_batches(self):
        sent, online = [], [False]
        def sender(url, payload):
            if not online[0]:
                raise ConnectionError("down")
            sent.append(payload)

        agent = self.make_agent(sender)
        for _ in range(BATCH_SIZE + 10):
            agent.tick()
        online[0] = True
        agent.tick()
        self.assertEqual([len(p["heartbeats"]) for p in sent], [BATCH_SIZE, 11])

    def test_failed_batch_stays_buffered_and_retries_whole(self):
        calls = []
        def flaky(url, payload):
            calls.append(len(payload["heartbeats"]))
            if len(calls) == 1:
                raise ConnectionError("lost response")

        buffer = TelemetryBuffer()
        agent = self.make_agent(flaky, buffer)
        agent.tick()
        self.assertEqual(len(buffer), 1)
        agent.tick()
        self.assertEqual((calls, len(buffer)), ([1, 2], 0))

    def test_run_stops_when_event_set(self):
        stop = threading.Event()
        stop.set()
        self.make_agent(lambda *_: None).run(stop)  # returns immediately


class TelemetryBufferTests(unittest.TestCase):
    @staticmethod
    def hb(seq):
        return Heartbeat("arm-001", seq, "1.0.0", 50.0, 0, float(seq))

    def test_fifo_peek_and_discard(self):
        buffer = TelemetryBuffer()
        for seq in (1, 2, 3):
            buffer.add(self.hb(seq))
        self.assertEqual([h.seq for h in buffer.peek(2)], [1, 2])
        self.assertEqual(len(buffer), 3)  # peek doesn't remove
        buffer.discard(2)
        self.assertEqual([h.seq for h in buffer.peek(10)], [3])

    def test_drops_oldest_when_full(self):
        buffer = TelemetryBuffer(capacity=2)
        self.assertFalse(buffer.add(self.hb(1)))
        self.assertFalse(buffer.add(self.hb(2)))
        self.assertTrue(buffer.add(self.hb(3)))
        self.assertEqual([h.seq for h in buffer.peek(10)], [2, 3])


class SimulatedLinkTests(unittest.TestCase):
    def test_sender_fails_only_while_down(self):
        sent, link = [], SimulatedLink()
        sender = link.wrap_sender(lambda url, payload: sent.append(payload))
        sender("u", {"a": 1})
        link.set_down(True)
        with self.assertRaises(ConnectionError):
            sender("u", {"a": 2})
        link.set_down(False)
        sender("u", {"a": 3})
        self.assertEqual(sent, [{"a": 1}, {"a": 3}])

    def test_connect_refused_while_down(self):
        link = SimulatedLink()
        link.set_down(True)
        with self.assertRaises(ConnectionError):
            link.wrap_connect(lambda url: FakeWebSocket())("ws://x")

    def test_outage_severs_an_established_connection(self):
        link = SimulatedLink()
        inner = FakeWebSocket()
        with link.wrap_connect(lambda url: inner)("ws://x") as ws:
            self.assertEqual(ws.recv(timeout=0.1), "msg")
            link.set_down(True)
            with self.assertRaises(ConnectionError):
                ws.recv(timeout=0.1)
        self.assertTrue(inner.closed)  # leaving the context closes the real socket


class FakeWebSocket:
    def __init__(self):
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def send(self, message):
        pass

    def recv(self, timeout=None):
        return "msg"


class OutageSchedulerTests(unittest.TestCase):
    class FakeRobot:
        def __init__(self, robot_id, restored: threading.Event):
            self.robot_id = robot_id
            self.events = []
            self._restored = restored

        def set_network_down(self, down):
            self.events.append(down)
            if not down:
                self._restored.set()

    def test_takes_one_robot_down_then_restores_it(self):
        restored = threading.Event()
        robots = [self.FakeRobot(f"arm-{i}", restored) for i in range(3)]
        scheduler = OutageScheduler(robots, up_for_s=0.01, down_for_s=0.01, rng=random.Random(0))
        stop = threading.Event()
        thread = threading.Thread(target=scheduler.run, args=(stop,))
        thread.start()
        self.assertTrue(restored.wait(2))
        stop.set()
        thread.join(2)

        touched = [r for r in robots if r.events]
        self.assertEqual(len(touched), 1)  # only one robot affected by the first outage
        self.assertEqual(touched[0].events[:2], [True, False])

    def test_shutdown_during_outage_still_restores_robot(self):
        restored = threading.Event()
        robot = self.FakeRobot("arm-1", restored)
        scheduler = OutageScheduler([robot], up_for_s=0.0, down_for_s=60, rng=random.Random(0))
        stop = threading.Event()
        thread = threading.Thread(target=scheduler.run, args=(stop,))
        thread.start()
        while not robot.events:  # wait until the outage begins
            time.sleep(0.005)
        stop.set()
        thread.join(2)
        self.assertEqual(robot.events, [True, False])


class FirmwareCatalogTests(unittest.TestCase):
    def test_artifact_checksum_matches_payload_and_is_deterministic(self):
        import hashlib
        artifact = make_artifact("2.0.0", size=256)
        self.assertEqual(artifact.sha256, hashlib.sha256(artifact.payload).hexdigest())
        self.assertEqual(make_artifact("2.0.0", size=256), artifact)
        self.assertNotEqual(make_artifact("2.1.0", size=256).payload, artifact.payload)

    def test_latest_compares_versions_numerically(self):
        catalog = FirmwareCatalog(make_artifact(v, size=8) for v in ["2.0.0", "10.0.0", "2.1.0"])
        self.assertEqual(catalog.versions(), ["2.0.0", "2.1.0", "10.0.0"])
        self.assertEqual(catalog.latest(), "10.0.0")

    def test_unknown_version_and_empty_catalog(self):
        catalog = FirmwareCatalog([])
        self.assertIsNone(catalog.get("2.0.0"))
        self.assertIsNone(catalog.latest())


class ServerAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_path = Path(self.tmp.name) / "hb.jsonl"
        self.log = HeartbeatLog(self.log_path)
        self.addCleanup(self.log.close)
        self.now = 1000.0
        self.registry = ConnectionRegistry()
        self.artifact = make_artifact("2.0.0", size=64)
        self.app = ServerApp(HeartbeatStore(), self.log, self.registry,
                             FirmwareCatalog([self.artifact]), offline_after_s=6.0,
                             clock=lambda: self.now)

    def test_send_command_pushes_to_connected_robot(self):
        conn = FakeConnection()
        self.registry.register("arm-001", conn)
        self.app.send_command("arm-001", "pause")
        self.assertEqual(conn.sent, [command_message(Command(CommandType.PAUSE))])

    # --- firmware updates ---

    def test_start_firmware_update_sends_version_and_checksum(self):
        conn = FakeConnection()
        self.registry.register("arm-001", conn)
        self.app.ingest(valid_payload())
        self.app.start_firmware_update("arm-001", "2.0.0")
        expected = Command(CommandType.UPDATE_FIRMWARE, "2.0.0", self.artifact.sha256)
        self.assertEqual(conn.sent, [command_message(expected)])

    def test_start_firmware_update_rejects_unknown_version(self):
        self.registry.register("arm-001", FakeConnection())
        for version in ["9.9.9", None, 2]:
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.app.start_firmware_update("arm-001", version)

    def test_start_firmware_update_requires_connected_robot(self):
        with self.assertRaises(RobotNotConnected):
            self.app.start_firmware_update("arm-001", "2.0.0")

    def test_start_firmware_update_rejects_robot_already_updating(self):
        conn = FakeConnection()
        self.registry.register("arm-001", conn)
        self.app.ingest(valid_payload(status_code=int(StatusCode.TRIAL_BOOT)))
        with self.assertRaises(UpdateConflict):
            self.app.start_firmware_update("arm-001", "2.0.0")
        self.assertEqual(conn.sent, [])

    def test_start_firmware_update_rejects_robot_already_on_that_version(self):
        self.registry.register("arm-001", FakeConnection())
        self.app.ingest(valid_payload(firmware_version="2.0.0"))
        with self.assertRaises(UpdateConflict):
            self.app.start_firmware_update("arm-001", "2.0.0")

    def test_generic_command_endpoint_refuses_firmware_updates(self):
        self.registry.register("arm-001", FakeConnection())
        with self.assertRaises(ValueError):
            self.app.send_command("arm-001", "update_firmware")

    def test_fleet_view_reports_firmware_state(self):
        self.app.ingest(valid_payload(
            status_code=int(StatusCode.ROLLING_BACK), firmware_version="2.0.0", active_slot="B",
            inactive_slot_version="1.0.0", last_update_result="rolled_back"))
        robot = self.app.fleet()[0]
        self.assertEqual(
            (robot["status"], robot["updating"], robot["active_slot"],
             robot["inactive_slot_version"], robot["last_update_result"]),
            ("ROLLING_BACK", True, "B", "1.0.0", "rolled_back"))

    def test_firmware_info_lists_catalog(self):
        self.assertEqual(self.app.firmware_info(), {"latest": "2.0.0", "versions": ["2.0.0"]})

    def test_send_command_rejects_unknown_command_and_disconnected_robot(self):
        self.registry.register("arm-001", FakeConnection())
        with self.assertRaises(ValueError):
            self.app.send_command("arm-001", "explode")
        with self.assertRaises(RobotNotConnected):
            self.app.send_command("arm-002", "pause")

    def test_fleet_view_reports_connected(self):
        self.app.ingest(valid_payload())
        self.assertFalse(self.app.fleet()[0]["connected"])
        self.registry.register("arm-001", FakeConnection())
        self.assertTrue(self.app.fleet()[0]["connected"])

    # --- burst / batch ingestion ---

    def test_batch_stores_every_heartbeat_individually(self):
        result = self.app.ingest_batch({"heartbeats": [valid_payload(seq=s) for s in (1, 2, 3)]})
        self.assertEqual(result.to_dict(), {"accepted": 3, "duplicates": 0, "rejected": 0})
        self.assertEqual(len(self.app.history("arm-001", 10)), 3)
        self.assertEqual(len(self.log_path.read_text().splitlines()), 3)

    def test_batch_is_idempotent(self):
        batch = {"heartbeats": [valid_payload(seq=s, sent_at=1000.0 + s) for s in (1, 2)]}
        self.app.ingest_batch(batch)
        result = self.app.ingest_batch(batch)  # robot retries after a lost response
        self.assertEqual(result.to_dict(), {"accepted": 0, "duplicates": 2, "rejected": 0})
        self.assertEqual(len(self.app.history("arm-001", 10)), 2)
        self.assertEqual(len(self.log_path.read_text().splitlines()), 2)

    def test_bad_items_are_rejected_without_blocking_the_rest(self):
        batch = {"heartbeats": [valid_payload(seq=1), {"junk": True}, valid_payload(seq=2)]}
        result = self.app.ingest_batch(batch)
        self.assertEqual(result.to_dict(), {"accepted": 2, "duplicates": 0, "rejected": 1})

    def test_malformed_batch_envelope_raises(self):
        for payload in [[], {"heartbeats": "nope"}, {"other": []}]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.app.ingest_batch(payload)

    def test_oversized_batch_raises(self):
        with self.assertRaises(ValueError):
            self.app.ingest_batch({"heartbeats": [valid_payload()] * (MAX_BATCH_SIZE + 1)})

    def test_burst_after_outage_is_flagged_late_and_keeps_robot_order(self):
        # Robot sampled at t=1000..1008 while offline; server receives it all at t=1010.
        self.now = 1010.0
        self.app.ingest_batch({"heartbeats": [
            valid_payload(seq=i + 1, sent_at=1000.0 + 2 * i) for i in range(5)]})
        self.app.ingest(valid_payload(seq=6, sent_at=1010.0))  # live heartbeat

        entries = self.app.history("arm-001", 10)
        self.assertEqual([e["seq"] for e in entries], [1, 2, 3, 4, 5, 6])
        # Delays are 10, 8, 6, 4, 2, 0s. Late means strictly more than the 6s offline threshold.
        self.assertEqual([e["late"] for e in entries], [True, True, False, False, False, False])
        self.assertEqual(entries[0]["delay_s"], 10.0)
        self.assertEqual(entries[0]["status"], "RUNNING")

    def test_ingest_logs_each_heartbeat_individually(self):
        self.app.ingest(valid_payload(seq=1))
        self.app.ingest(valid_payload(seq=2))
        self.assertEqual(len(self.log_path.read_text().splitlines()), 2)

    def test_invalid_heartbeat_not_stored(self):
        with self.assertRaises(ValueError):
            self.app.ingest(valid_payload(battery_level=-5))
        self.assertEqual(self.app.fleet(), [])
        self.assertEqual(self.log_path.read_text(), "")

    def test_fleet_view_marks_stale_robot_offline(self):
        self.app.ingest(valid_payload())
        self.assertTrue(self.app.fleet()[0]["online"])
        self.now += 10
        self.assertFalse(self.app.fleet()[0]["online"])

    def test_fleet_view_reports_latest_only(self):
        self.app.ingest(valid_payload(seq=1, battery_level=90))
        self.app.ingest(valid_payload(seq=2, battery_level=80))
        fleet = self.app.fleet()
        self.assertEqual(len(fleet), 1)
        self.assertEqual(fleet[0]["battery_level"], 80)
        self.assertEqual(len(self.app.history("arm-001", 10)), 2)


if __name__ == "__main__":
    unittest.main()
