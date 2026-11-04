"""The fault suite's own logic: the names a plug may go by, the saved plugs
each plug has, and the scenarios it lists."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/espressif/emulator"))
import fault_suite as fs  # noqa: E402

MAC = "12:34:56:ab:cd:ef"


class NameProblemTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(fs, "MAC", MAC)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_conversion_under_the_names_it_should_have(self):
        names = ["espressif", "autoflash-recovery-CDEF", "tasmota-ABCDEF-1234", "tasmota-abcdef"]
        self.assertEqual(fs.name_problems(names, ["autoflash-recovery-CDEF", "ESP_ABCDF0"], "tasmota"), ([], None))
        self.assertEqual(fs.name_problems(["tasmota-abcdef", "autoflash-recovery-CDEF", "esphome-abcdef"],
                                          [], "esphome"), ([], None))
        self.assertEqual(fs.name_problems(["esphome-abcdef", "autoflash-recovery-CDEF", "espressif"],
                                          [], "sonoff"), ([], None))

    def test_a_name_this_plug_should_never_have(self):
        unknown, _ = fs.name_problems(["autoflash-recovery-1234", "tasmota-abcdef"], ["tasmota"], "tasmota")
        self.assertEqual(unknown, ["autoflash-recovery-1234", "tasmota"])

    def test_not_ending_under_its_converted_name(self):
        _, final = fs.name_problems(["autoflash-recovery-CDEF", "tasmota-ABCDEF-1234"], [], "tasmota")
        self.assertEqual(final, "tasmota-ABCDEF-1234")

    def test_names_of_earlier_releases_are_allowed(self):
        names = ["sonoff-s60-abcdef", "s60-ota-bridge", "s60-recovery-0042", "x0042", "sonoff-s60-mover-abcdef",
                 "tasmota-abcdef"]
        self.assertEqual(fs.name_problems(names, ["Sonoff-Setup-CDEF", "S60-OTA-Bridge-CDEF"], "tasmota"),
                         ([], None))


class CloudTurnTests(unittest.TestCase):
    def turns_from_the_start(self, sc):
        monitor = fs.PlugMonitor(mock.Mock(n=24, state=Path("/nonexistent"), sc=sc, say=lambda _: None))
        monitor.preheld = True
        with mock.patch.object(monitor, "live_run", return_value=None), \
                mock.patch.object(monitor, "hold") as hold, \
                mock.patch.object(fs, "plug_internet"), mock.patch.object(fs, "NO_CLOUD", False), \
                mock.patch.object(monitor.stop, "wait", side_effect=lambda _: monitor.stop.set()):
            monitor.watching()
        return [call.args[0] for call in hold.call_args_list]

    def test_a_legacy_run_s_plug_is_online_from_the_start_whatever_step_it_stops_at(self):
        # Seen against the real cloud: its turn held and its way out shut, so
        # eWeLink showed the plug offline and the old release stopped to ask,
        # long before the step under test.
        self.assertEqual(self.turns_from_the_start({"step": "C3", "fault": "legacy"}), ["from the start"])
        self.assertEqual(self.turns_from_the_start({"step": "start", "fault": "legacy"}), ["from the start"])
        self.assertEqual(self.turns_from_the_start({"step": "C2", "fault": "crash"}), ["from the start"])
        self.assertEqual(self.turns_from_the_start({"step": "C3", "fault": "crash"}), [])

    def test_the_installer_sent_again_while_waiting_for_a_turn_is_answered_by_it(self):
        said = []
        monitor = fs.PlugMonitor(mock.Mock(n=24, state=Path("/nonexistent"), sc={"step": "C3"}, say=said.append))
        text = ["=== run started\n"]
        intent = "[11:31:39] Sending the installer again (attempt %d of 3)\n"
        calls = iter(range(1000))
        waits = iter([False] * 20 + [True] * 1000)   # a bounded wait, whatever the turns

        def live_run():
            if next(calls) == 0:
                return ("run", "=== run started\n")
            return ("run", "".join(text))
        turns = []

        def acquire(*_):
            if not turns:
                text.append(intent % 2)   # sent again while it waits
            return True

        def hold(why, run=None):
            turns.append(why)
            return len(fs.INTENT.findall("".join(text)))
        text.append(intent % 1)
        with mock.patch.object(monitor, "live_run", side_effect=live_run), \
                mock.patch.object(monitor, "hold", side_effect=hold), \
                mock.patch.object(fs, "plug_internet"), mock.patch.object(fs, "NO_CLOUD", False), \
                mock.patch.object(fs.CLOUD, "acquire", side_effect=acquire), \
                mock.patch.object(monitor.stop, "wait", side_effect=lambda t: next(waits) and monitor.stop.set()):
            monitor.watching()
        self.assertEqual(turns, ["fallen back"])

    def test_a_turn_starts_again_when_the_installer_is_sent_again_part_way(self):
        # Seen against the real cloud: sent again with 70 s of a turn left, and
        # the plug shut off part way through that attempt.
        with tempfile.TemporaryDirectory() as state:
            (Path(state) / "logs").mkdir()
            (Path(state) / "logs/uart.log").write_text("", encoding="utf-8")
            said = []
            monitor = fs.PlugMonitor(mock.Mock(n=24, state=Path(state), sc={"step": "C3"}, say=said.append))
            text = ["=== run started\n", "Sending the installer again (attempt 1 of 3)\n"]
            clock = [1000.0]

            def wait(_):
                clock[0] += 100
                if clock[0] == 1200:
                    text.append("Sending the installer again (attempt 2 of 3)\n")
            with mock.patch.object(monitor, "live_run", side_effect=lambda: ("run", "".join(text))), \
                    mock.patch.object(monitor, "run_text", return_value=""), \
                    mock.patch.object(fs, "plug_internet"), mock.patch.object(fs.CLOUD, "release"), \
                    mock.patch.object(fs.time, "time", side_effect=lambda: clock[0]), \
                    mock.patch.object(monitor.stop, "wait", side_effect=wait):
                seen = monitor.hold("fallen back", run="run")
        self.assertEqual(seen, 2)
        self.assertIn("CLOUD: turn renewed (asked again)", said)
        # Held a whole turn past the attempt sent at 1200.
        self.assertGreaterEqual(clock[0], 1200 + fs.HOLD_AT_MOST)


class SavedPlugTests(unittest.TestCase):
    def stale(self, made, todo):
        # A saved plug's name from its route; its firmware as prep recorded it.
        with tempfile.TemporaryDirectory() as emu:
            start = Path(emu) / "saved" / fs.saved("suite-esphome")
            start.mkdir(parents=True)
            if made is not None:
                (start / "firmware.json").write_text(made, encoding="utf-8")
            with mock.patch.object(fs, "EMU", Path(emu)), \
                    mock.patch.object(fs, "installs", return_value={"version": "2"}):
                return fs.stale_starts(todo)

    def test_a_batch_is_told_when_a_saved_plug_is_not_on_this_checkout_s_firmware(self):
        # Seen: ESPHome moved on and every esphome> scenario still started from
        # the release before, passing, until the tool began offering it an update.
        todo = [{"route": "esphome>tasmota", "fault": "crash"}]
        name = fs.saved("suite-esphome")
        self.assertEqual(self.stale('{"version": "1"}', todo), [name])
        self.assertEqual(self.stale(None, todo), [name])
        self.assertEqual(self.stale("not json", todo), [name])
        self.assertEqual(self.stale('{"version": "2"}', todo), [])

    def test_a_batch_refuses_to_start_from_one(self):
        with mock.patch.object(fs, "scenarios", return_value=[{"id": "a", "route": "esphome>tasmota", "fault": "crash"}]), \
                mock.patch.object(fs, "latest", return_value={}), mock.patch.object(fs, "LOGS"), \
                mock.patch.object(fs, "stale_starts", return_value=["suite-esphome"]), \
                mock.patch.object(fs, "worker", side_effect=AssertionError("a scenario was run")):
            with self.assertRaisesRegex(SystemExit, "suite-esphome.*Run prep"):
                fs.batch()

    def test_runs_that_start_from_no_converted_plug_ask_nothing_of_it(self):
        self.assertEqual(self.stale('{"version": "1"}', [{"route": "sonoff>esphome", "fault": "crash"},
                                                         {"route": "esphome>sonoff", "fault": "legacy"}]), [])

    def test_a_starting_plug_is_kept_only_when_its_run_got_there(self):
        # All six once listed as saved, each run stopped at "Discovering the device".
        stopped_early = "Stage  2 - Discovering the device - Stopped\nExit code 130"
        self.assertFalse(fs.prepared(stopped_early, None, False))
        self.assertFalse(fs.prepared(stopped_early, "C2", False))
        self.assertTrue(fs.prepared("     Result:               Converted\nExit code 0", None, False))
        self.assertTrue(fs.prepared("Stage  7 - Backing up Sonoff firmware - Stopped", "C2", True))

    def saved_as(self, owner, mac):
        with tempfile.TemporaryDirectory() as emu:
            if owner:
                (Path(emu) / "saved/stub-suite-tasmota/captures/plugs" / owner).mkdir(parents=True)
            with mock.patch.object(fs, "EMU", Path(emu)), mock.patch.object(fs, "STUB_CLOUD", True), \
                 mock.patch.object(fs, "MAC", mac):
                return fs.saved("suite-tasmota")

    def test_the_plug_that_saved_it_has_it(self):
        self.assertEqual(self.saved_as("12-34-56-ab-cd-ef", MAC), "stub-suite-tasmota")

    def test_another_plug_has_its_own_beside_it(self):
        self.assertEqual(self.saved_as("12-34-56-ab-cd-ef", "12:34:56:00:11:22"), "stub-suite-tasmota-1122")

    def test_one_not_saved_yet_is_the_first_plugs(self):
        self.assertEqual(self.saved_as(None, MAC), "stub-suite-tasmota")


class RunWordsTests(unittest.TestCase):
    """The lines of a run the suite times its faults by, as the tool says them
    now: reworded, a fault would never fire there (NOT-FIRED)."""

    def test_c3_starts_at_the_line_the_backup_begins_with(self):
        import re
        source = (Path(__file__).resolve().parents[2] / "tools/autoflash/routes/espressif/installer.py")
        self.assertRegex(source.read_text(encoding="utf-8"), re.compile(fs.STARTS["C3"][0]))
        self.assertRegex("Backing up the plug's Sonoff firmware - about a minute...", fs.STARTS["C3"][0])  # legacy

    def test_f4_without_the_driver_is_told_by_what_its_reset_says(self):
        from types import SimpleNamespace
        from tools.autoflash import output
        from tools.autoflash.routes.espressif import tasmota
        from tests.autoflash import test_autoflash
        # A Tasmota that does not take the name and template at the reset's save.
        device = SimpleNamespace(wait_reachable=lambda **_: None, started_at=lambda: "t",
                                 uptime_seconds=lambda: 10, wait_for_reboot=lambda **_: 1.0,
                                 command=test_autoflash.ConfigureStepTests.console([]), is_safeboot=lambda: False,
                                 at_next_save=lambda commands, timeout=None: False)
        state = SimpleNamespace(input=lambda key: {"product_model": "S60TPF"}.get(key))
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: "192.168.1.51"), device=device, state=state)
        with mock.patch.object(output, "say") as said:
            tasmota.step_configure(ctx)
        self.assertRegex(" ".join(str(c.args[0]) for c in said.call_args_list), fs.NOHOOK_RESET)


class ScaledBatchTests(unittest.TestCase):
    def allowed_with(self, text):
        with tempfile.TemporaryDirectory() as out:
            if text is not None:
                (Path(out) / "workers").write_text(text)
            with mock.patch.object(fs, "OUT", Path(out)):
                return fs.allowed(4)

    def test_the_number_in_the_file_while_it_is_there(self):
        self.assertEqual(self.allowed_with("6\n"), 6)
        self.assertEqual(self.allowed_with("0"), 0)

    def test_how_many_it_began_with_otherwise(self):
        self.assertEqual(self.allowed_with(None), 4)
        self.assertEqual(self.allowed_with("lots"), 4)

    def test_a_worker_held_back_stops_once_nothing_is_left(self):
        todo = fs.queue.Queue()
        with mock.patch.object(fs.time, "sleep", side_effect=AssertionError("waited")):
            fs.worker(5, todo, None, room=lambda n: False)


class ScenarioTests(unittest.TestCase):
    def test_batch_takes_the_file_list_writes(self):
        # `list > all.txt` then `batch all.txt`, as docs/testing.md runs them:
        # each line an id and whether it needs the cloud.
        with tempfile.TemporaryDirectory() as tmp:
            listed = Path(tmp, "all.txt")
            listed.write_text("sonoff>tasmota:C3:outage-5 cloud\ntasmota>esphome:L1:outage-5 any\n\n",
                              encoding="utf-8")
            self.assertEqual(fs.read_ids(listed), ["sonoff>tasmota:C3:outage-5", "tasmota>esphome:L1:outage-5"])

    def test_every_scenario_has_its_own_id(self):
        ids = [sc["id"] for sc in fs.scenarios()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_composite_faults_are_listed(self):
        ids = {sc["id"] for sc in fs.scenarios()}
        for wanted in ("sonoff>tasmota:C3:ownnet-cut", "sonoff>esphome:D1:lostanswer-once",
                       "sonoff>tasmota:D1:seven-advised", "esphome>tasmota:G1:seven-times",
                       "sonoff>tasmota:F4:nohook-run", "esphome>tasmota:F4rs@90:plugoff-1"):
            self.assertIn(wanted, ids)

    def test_each_length_only_where_it_leads_somewhere_of_its_own(self):
        ids = {sc["id"] for sc in fs.scenarios()}
        self.assertFalse([i for i in ids if i.endswith(":outage-1")])
        self.assertIn("tasmota>sonoff:D4:outage-15", ids)
        # A rerun 60 s after a crash: only where a fallback network may come up.
        self.assertIn("tasmota>esphome:L1:crash-60", ids)
        self.assertIn("sonoff>tasmota:C3fb:crash-60", ids)
        self.assertIn("sonoff>esphome:C2:crash-60", ids)
        self.assertNotIn("tasmota>sonoff:D4:crash-60", ids)

    def test_a_packet_is_changed_only_where_a_step_sends_one(self):
        # R2 only sees Sonoff's LAN port take a connection: no packet there
        # carries data, and corrupt-once was always NO-TRAFFIC.
        ids = {sc["id"] for sc in fs.scenarios()}
        for route in ("tasmota>sonoff", "esphome>sonoff"):
            self.assertNotIn(f"{route}:R2:corrupt-once", ids)
            self.assertIn(f"{route}:R2:outage-5", ids)
            self.assertIn(f"{route}:R1:corrupt-once", ids)
        self.assertIn("sonoff>tasmota:H1:corrupt-once", ids)

    def test_the_tools_own_handling_at_the_riskiest_steps(self):
        ids = {sc["id"] for sc in fs.scenarios()}
        for fault in ("second-run", "ctrlc-countdown", "ctrlc-apart", "sleep-120"):
            self.assertIn(f"esphome>tasmota:F4:{fault}", ids)
            self.assertIn(f"sonoff>tasmota:C2:{fault}", ids)
            self.assertNotIn(f"esphome>tasmota:E1:{fault}", ids)
        self.assertIn("tasmota>esphome:M1:sleep-120", ids)
        self.assertNotIn("tasmota>esphome:M1:second-run", ids)
        self.assertIn("esphome>tasmota:E1:hangup-continue", ids)

    def test_runs_left_by_1_2_0_are_carried_on(self):
        ids = {sc["id"] for sc in fs.scenarios()}
        for step in ("C2", "C3", "D1", "D4", "E1", "E2", "E3", "F2", "F4", "G1", "H1"):
            self.assertIn(f"sonoff>tasmota:{step}:legacy-v1.2.0", ids)
        self.assertIn("tasmota>esphome:start:legacy-v1.2.0", ids)
        self.assertIn("tasmota>sonoff:start:legacy-v1.2.0", ids)
        # G1 over that release's startup firmware, unproven, cut off part way.
        self.assertIn("sonoff>tasmota:F4:legacy-v1.2.0+G1off", ids)
        self.assertTrue(fs.needs_cloud({"route": "tasmota>esphome", "step": "start", "fault": "legacy"}))

    def test_runs_left_by_the_release_before_a_firmware_update_are_carried_on(self):
        # Its bundle pinned to the firmware the tool installed then
        # (docs/updating-firmware.md): every step from Sonoff, both ways, and
        # each kind of plug it converted.
        ids = {sc["id"] for sc in fs.scenarios()}
        for route, steps in (("sonoff>tasmota", fs.ROUTES["sonoff>tasmota"][2]),
                             ("sonoff>esphome", fs.ROUTES["sonoff>esphome"][2])):
            for step in set(steps) - {"C3fb"} | {"C2"}:
                self.assertIn(f"{route}:{step}:legacy-v2.0.2", ids)
        for route in ("tasmota>esphome", "tasmota>sonoff", "esphome>tasmota", "esphome>sonoff"):
            self.assertIn(f"{route}:start:legacy-v2.0.2", ids)
        self.assertNotIn("sonoff>tasmota:F4:legacy-v2.0.2+G1off", ids)

    def test_phases_must_ride_out_blips_by_themselves(self):
        # PR #5: one early blip used up a phase's 20 s of retrying, and a later
        # one failed on its first try - the step then tried again from the start.
        ids = {sc["id"] for sc in fs.scenarios()}
        for route, step in fs.BLIP_STEPS:
            self.assertIn(f"{route}:{step}@10:blips-3", ids)
            self.assertIn(f"{route}:{step}@10:lossy-phase", ids)
        # Two blips 80% of the step apart: more than a phase's 20 s of retrying
        # in the longer ones, which PR #5's window, counted from the first
        # failure, ran out of; E2, G1 and L1 are shorter than that from 2.0.2.
        apart = {step: 0.8 * fs.ROUTES[route][2][step] for route, step in fs.BLIP_STEPS}
        self.assertGreaterEqual(sum(gap > 20 for gap in apart.values()), 2, apart)

    def test_how_busy_each_phase_kept_the_plug(self):
        import json
        import os
        import time
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            for name, captures, age in (("new", {"E2.plug_busy_seconds": 31.2, "G1.plug_busy_seconds": 12,
                                                 "E2.seconds": 40}, 0),
                                        ("again", {"E2.plug_busy_seconds": 44.0}, 0),
                                        ("old", {"E2.plug_busy_seconds": 99.0}, 3600)):
                run = state / "work/captures/plugs/12-34-56-ab-cd-ef/runs" / name
                run.mkdir(parents=True)
                path = run / "autoflash-state.json"
                path.write_text(json.dumps({"captures": captures}))
                os.utime(path, (time.time() - age, time.time() - age))
            self.assertEqual(fs.plug_busy(state, time.time() - 60), {"E2": 44.0, "G1": 12})

    def test_an_esphome_factory_image_as_its_builder_makes_one(self):
        image = fs.esphome_factory_image()
        self.assertEqual(image[0], 0xE9)  # all Tasmota's page checks of it, with the size
        self.assertEqual(image[0x8000:0x8002], b"\xaa\x50")  # a partition table
        self.assertEqual(image[0x20000], 0xE9)  # the app, in the first slot
        self.assertIn("tasmota>esphome:start:lostapp-factory", {sc["id"] for sc in fs.scenarios()})

    def test_a_legacy_run_goes_without_firmware_and_the_next_with_it(self):
        with tempfile.TemporaryDirectory() as tree:
            (Path(tree) / "tools").mkdir()
            (Path(tree) / fs.LEGACY_TOOL).touch()
            sc = fs.Scenario(24, {"id": "x", "route": "sonoff>tasmota", "step": "D4", "fault": "legacy",
                                  "arg": "v1.2.0", "at": 40}, "/dev/null")
            with mock.patch.object(fs.subprocess, "call", return_value=0) as call:
                sc.run_tool("tasmota", code=tree)
                sc.run_tool("tasmota", code=fs.REPO)
            old, new = (c.args[0] for c in call.call_args_list)
            self.assertIn(f"--code={tree}", old)
            self.assertNotIn("--firmware", old)
            self.assertEqual(new[-2:], ["--firmware", "tasmota"])

    def test_the_advice_beside_a_question_is_followed_though_the_screen_lost_it(self):
        # sonoff>esphome:C2:cancel-switch: eWeLink showed the plug offline after
        # its update was cut off, the run said to switch it off and on beside
        # "What now", and stopped there - its last screen without the advice.
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, "scenario.log")
            log.write_text("--- --instance=4\n Stage 2 - Discovering the device - Stopped\n"
                           " Cleaned up. Nothing was left half done. Exiting.\n", encoding="utf-8")
            runs = Path(tmp, "state/work/captures/plugs/unidentified/runs/20261103-041854")
            runs.mkdir(parents=True)
            (runs / "run.log").write_text(
                "=== run started ===\n[04:20:00] earlier: switch it off and on\n=== run ended ===\n"
                "=== run started ===\n[04:21:45]     - 'S60' (S60TPG): eWeLink shows it offline - check it is\n"
                "[04:21:45]     switched on. If it is, switch the device off and on: after an update cut off\n"
                "[04:21:45]     What now\n", encoding="utf-8")
            sc = fs.Scenario(24, {"id": "x", "route": "sonoff>esphome", "step": "C2", "fault": "cancel",
                                  "arg": "switch", "at": 40}, str(log))
            sc.state = Path(tmp, "state")
            said = sc.last_said()
        self.assertIn("switch the device off and on", said)
        self.assertNotIn("earlier", said)   # the last run's, not one before it

    def test_a_legacy_tree_is_run_by_its_own_driver(self):
        # unattended-test.sh --code: a release from before the reorganisation
        # keeps its driver, and the tool it starts, directly in tools/.
        import re
        import subprocess
        script = (fs.REPO / "tools/espressif/emulator/unattended-test.sh").read_text(encoding="utf-8")
        choice = re.search(r"^driver=.*\n.*\|\| driver=.*\n", script, re.M).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp, "work")
            (work / "tools/repo").mkdir(parents=True)
            (work / "tools/unattended_run.py").touch()
            for tree, driver in (("old", "tools/unattended_run.py"), ("new", "tools/repo/unattended_run.py")):
                if tree == "new":
                    (work / driver).touch()
                chosen = subprocess.run(["bash", "-c", choice + 'echo "$driver"'], env={"STATE": tmp},
                                        capture_output=True, text=True, check=True).stdout.strip()
                self.assertEqual(chosen, driver, tree)

    def test_a_run_is_given_the_ewelink_account_and_nothing_else_from_env(self):
        # Seen: a token kept in .env for something else reached the run, which
        # refused the name, and every real-cloud scenario after was NOT-FIRED.
        import re
        import subprocess
        script = (fs.REPO / "tools/espressif/emulator/unattended-test.sh").read_text(encoding="utf-8")
        given = re.search(r"^ *grep .*\"\$ROOT/\.env\"", script, re.M).group(0)
        with tempfile.TemporaryDirectory() as root:
            Path(root, ".env").write_text(
                "EWELINK_EMAIL=someone@example.com\nexport EWELINK_PASSWORD=x\nAUTOFLASH_WIFI_PASSWORD=y\n"
                "AUTOFLASH_BACKUP_KEY=z\nOTHER_TOKEN=t\n", encoding="utf-8")
            out = subprocess.run(["bash", "-c", given], env={"ROOT": root},
                                 capture_output=True, text=True).stdout
        self.assertEqual(out, "EWELINK_EMAIL=someone@example.com\nexport EWELINK_PASSWORD=x\n")

    def test_a_legacy_tree_goes_in_only_once_the_stub_has_started_from_this_checkout(self):
        # Swapped in as the emulator started, a release from before the
        # reorganisation took away what the stub reads the plug with: no stub,
        # and every 2.0.2 run stopped at "Connection refused".
        script = (fs.REPO / "tools/espressif/emulator/unattended-test.sh").read_text(encoding="utf-8")
        self.assertLess(script.index("tmux has-session"), script.index('"$code/" "$STATE/work/"'))

    @unittest.skipUnless(all(fs.subprocess.run(["git", "-C", str(fs.REPO), "rev-parse", "-q", "--verify",
                                                f"refs/tags/{tag}"], capture_output=True).returncode == 0
                             for tag in fs.LEGACY_STEPS), "no release tags in this checkout")
    def test_each_legacy_release_takes_its_patch(self):
        # The reorganisation moved 1.2.0's patch to tools/ewelink/, which 1.2.0
        # never had: every 1.2.0 scenario was a HARNESS-ERROR.
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(fs, "LEGACY", Path(tmp)):
            for tag in fs.LEGACY_STEPS:
                with self.subTest(tag=tag):
                    fs.legacy_tree(tag)

    def test_the_person_carries_on_when_a_run_log_moves_as_it_looks(self):
        # Seen on a VM: the tool moved a run from unidentified/ under its
        # plug's MAC between the person's listing and its look at the log,
        # and the person's thread died with FileNotFoundError.
        import threading
        sc = fs.Scenario(24, {"id": "x", "route": "sonoff>tasmota", "step": "D4", "fault": "cancel",
                              "arg": "new", "at": 40}, "/dev/null")
        sc.started = 0
        stop = threading.Event()
        moved = Path(tempfile.gettempdir()) / "no-such-run" / "run.log"

        def logs(state):
            stop.set()  # one look, then stop
            return [(1.0, moved, "a run")]
        with mock.patch.object(sc, "ex", return_value=mock.Mock(stdout="", returncode=1)), \
                mock.patch.object(fs, "run_logs", side_effect=logs):
            sc.person(stop)

    def test_a_router_switch_not_taken_is_noted_and_one_taken_is_not(self):
        sc = fs.Scenario(24, {"id": "x", "route": "sonoff>tasmota", "step": "D4", "fault": "outage",
                              "arg": 60, "at": 40}, "/dev/null")
        with mock.patch.object(sc, "ex", return_value=mock.Mock(returncode=0)) as ex:
            sc.router("off")
        ex.assert_called_once_with(fs.PLUG_CMD, "router", "off")
        self.assertNotIn("router_not_switched", sc.notes)
        with mock.patch.object(sc, "ex", return_value=mock.Mock(returncode=1)):
            sc.router("off")
        self.assertTrue(sc.notes["router_not_switched"])

    def test_a_legacy_run_is_killed_by_its_own_tools_name(self):
        # 2.0.x's tool is tools/autoflash_cli.py, 1.2.0's tools/s60_autoflash.py.
        import re
        sc = fs.Scenario(24, {"id": "x", "route": "sonoff>tasmota", "step": "D4", "fault": "legacy",
                              "arg": "v2.0.2", "at": 40}, "/dev/null")
        with mock.patch.object(sc, "ex") as ex:
            sc.fault("legacy", "v2.0.2", 0, None)
        patterns = [c.args[-1] for c in ex.call_args_list if c.args[0] == "pkill"]
        for tool in ("tools/autoflash_cli.py", "tools/s60_autoflash.py"):
            self.assertTrue(any(re.search(p, tool) for p in patterns), tool)

    def test_the_cut_in_the_g1_after_a_legacy_run_lands_in_this_checkouts_g1(self):
        # Timed by 1.2.0's G1 (33 s), it came after this one's (21 s from
        # 2.0.2) had finished: MISTIMED.
        sc = fs.Scenario(24, {"id": "x", "route": "sonoff>tasmota", "step": "F4", "fault": "legacy",
                              "arg": "v1.2.0+G1off", "at": 40}, "/dev/null")
        sc.fired = "legacy"
        with mock.patch.object(fs, "legacy_tree"), mock.patch.object(sc, "run_tool"), \
             mock.patch.object(sc, "screen_result", return_value="Stopped"), \
             mock.patch.object(fs.threading, "Thread"), mock.patch.object(sc, "again") as again:
            sc.go_legacy()
        self.assertEqual(again.call_args.kwargs["later"],
                         ("G1", "plugoff", 1, 0.6 * fs.ROUTES["sonoff>tasmota"][2]["G1"]))

    def test_an_earlier_releases_own_error_is_not_this_checkouts(self):
        # 1.2.0, killed as G1 began, had stopped on a TimeoutError of its own:
        # this checkout's run carried on in the same run log and converted.
        sc = fs.Scenario(24, {"id": "x", "route": "sonoff>tasmota", "step": "G1", "fault": "legacy",
                              "arg": "v1.2.0", "at": 40}, "/dev/null")
        old = f"=== run started, tool version 1.2.0 ===\nFAILED: {fs.UNEXPECTED}: TimeoutError\n"
        log = Path("run.log")
        sc.earlier_release_wrote = {log: len(old)}
        self.assertNotIn(fs.UNEXPECTED, sc.ours(log, old + "=== run started ===\nConverted\n"))
        self.assertIn(fs.UNEXPECTED, sc.ours(log, old + f"FAILED: {fs.UNEXPECTED}: KeyError\n"))
        self.assertIn(fs.UNEXPECTED, sc.ours(Path("another/run.log"), old))

    def test_a_step_with_a_start_of_its_own_needs_no_length(self):
        # F4rs is in STARTS only: its length comes from there.
        sc = fs.Scenario(24, {"id": "x", "route": "sonoff>tasmota", "step": "F4rs", "fault": "plugoff",
                              "arg": 1, "at": 50}, "/dev/null")
        with mock.patch.object(sc, "run_tool", side_effect=RuntimeError("stop here")), \
             mock.patch.object(fs.threading, "Thread"):
            with self.assertRaisesRegex(RuntimeError, "stop here"):
                sc.go_watched()
        self.assertEqual(sc.step_length, fs.STARTS["F4rs"][1])

    def test_every_step_has_a_length_or_a_start_of_its_own(self):
        for sc in fs.scenarios():
            if sc["fault"] == "legacy":
                steps = fs.LEGACY_STEPS[sc["arg"].partition("+")[0]].get(sc["route"], {})
                self.assertTrue(sc["step"] == "start" or sc["step"] in steps, sc["id"])
                continue
            if sc["fault"] in ("lostapp", "notemplate", "bootloop", "safemode"):
                self.assertEqual(sc["step"], "start", sc["id"])
                continue
            if sc["step"] == "C2":
                continue
            steps = fs.ROUTES[sc["route"]][2]
            self.assertTrue(sc["step"] in steps or sc["step"] in fs.STARTS, sc["id"])


if __name__ == "__main__":
    unittest.main()
