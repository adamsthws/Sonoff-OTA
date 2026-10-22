import datetime as dt
import hashlib
import http.client
import json
import struct
import tempfile
import threading
import unittest
import zlib
from pathlib import Path

from tools.safeboot_migration import (
    MD5_MAGIC,
    PARTITION_MAGIC,
    PINNED_ARTIFACTS,
    RECOVERY_MODE,
    SECTOR_SIZE,
    SOURCE_PARTITIONS,
    SOURCE_TABLE_SHA256,
    TARGET_PARTITIONS,
    analyze_canonical_nvs,
    official_release,
    parse_partition_sector,
    require_pinned_artifacts,
    sector_aligned_size,
    sha256_bytes,
)
from tools.autoflash import esphome as esphome_files
from tools.autoflash import plans
from tools.configure_recovery_safeboot import render_header
from tools.validate_recovery_safeboot import header_value
from tools.serve_safeboot_migration import (
    COMMIT_FLAG,
    RESTORE_FLAG,
    MigrationHandler,
    MigrationServer,
    MigrationState,
    parse_report,
    phase_confirmation_refusal,
)


def partition_sector(entries):
    data = bytearray(b"\xff" * SECTOR_SIZE)
    offset = 0
    for label, ptype, subtype, start, size, flags in entries:
        struct.pack_into(
            "<HBBII16sI",
            data,
            offset,
            PARTITION_MAGIC,
            ptype,
            subtype,
            start,
            size,
            label.encode("ascii").ljust(16, b"\0"),
            flags,
        )
        offset += 32
    digest = hashlib.md5(data[:offset]).digest()
    data[offset : offset + 32] = struct.pack("<H", MD5_MAGIC) + b"\xff" * 14 + digest
    return bytes(data)


def canonical_nvs():
    data = bytearray(b"\xff" * 0x5000)
    page = bytearray(b"\xff" * SECTOR_SIZE)
    struct.pack_into("<II", page, 0, 0xFFFFFFFE, 7)
    struct.pack_into("<I", page, 28, zlib.crc32(page[4:28], 0xFFFFFFFF) & 0xFFFFFFFF)

    def mark_written(index):
        shift = (index % 4) * 2
        page[32 + index // 4] &= ~(0x03 << shift)
        page[32 + index // 4] |= 0x02 << shift

    def entry_crc(entry):
        struct.pack_into(
            "<I", entry, 4, zlib.crc32(entry[:4] + entry[8:32], 0xFFFFFFFF) & 0xFFFFFFFF
        )

    next_entry = 0
    for key, chunk_index in (("Settings", 0), ("sta.apinfo", 128)):
        payload = key.encode("ascii")[:4]
        entry = bytearray(b"\xff" * 32)
        struct.pack_into("<BBBB", entry, 0, 1, 0x42, 2, chunk_index)
        entry[8:24] = key.encode("ascii").ljust(16, b"\0")
        struct.pack_into("<H", entry, 24, len(payload))
        struct.pack_into("<I", entry, 28, zlib.crc32(payload, 0xFFFFFFFF) & 0xFFFFFFFF)
        entry_crc(entry)
        page[64 + next_entry * 32 : 96 + next_entry * 32] = entry
        page[96 + next_entry * 32 : 128 + next_entry * 32] = payload.ljust(32, b"\xff")
        mark_written(next_entry)
        mark_written(next_entry + 1)
        next_entry += 2

        index_entry = bytearray(b"\xff" * 32)
        struct.pack_into("<BBBB", index_entry, 0, 1, 0x48, 1, 0xFF)
        index_entry[8:24] = key.encode("ascii").ljust(16, b"\0")
        struct.pack_into("<IBBH", index_entry, 24, len(payload), 1, chunk_index, 0xFFFF)
        entry_crc(index_entry)
        page[64 + next_entry * 32 : 96 + next_entry * 32] = index_entry
        mark_written(next_entry)
        next_entry += 1
    data[:SECTOR_SIZE] = page
    return bytes(data)


class MigrationFixture:
    def __init__(self, root: Path):
        self.root = root
        # The production source table is deliberately allow-listed by hash.
        # Unit tests need its exact bytes, so construct it and assert the fixture.
        self.source_table = partition_sector(SOURCE_PARTITIONS)
        if sha256_bytes(self.source_table) != SOURCE_TABLE_SHA256:
            raise AssertionError("source table fixture does not match production allow-list")
        self.target_table = partition_sector(TARGET_PARTITIONS)
        self.safeboot = bytes((index * 29 + 11) & 0xFF for index in range(12_345))
        self.app = bytes((index * 17 + 3) & 0xFF for index in range(16_789))
        (root / "target.bin").write_bytes(self.target_table)
        (root / "safeboot.bin").write_bytes(self.safeboot)
        (root / "app.bin").write_bytes(self.app)
        self.manifest = {
            "schema": 1,
            "artifacts": {
                "target_table": {
                    "file": "target.bin",
                    "size": len(self.target_table),
                    "sha256": sha256_bytes(self.target_table),
                },
                "safeboot": {
                    "file": "safeboot.bin",
                    "size": len(self.safeboot),
                    "sha256": sha256_bytes(self.safeboot),
                },
                "app": {
                    "file": "app.bin",
                    "size": len(self.app),
                    "sha256": sha256_bytes(self.app),
                },
            },
        }
        self.manifest_path = root / "manifest.json"
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.evidence_dir = root / "live"

    def state(self, phase: str) -> MigrationState:
        return MigrationState(
            self.manifest_path,
            self.manifest,
            phase,
            "http://192.168.1.10:8089",
            self.evidence_dir,
            7200,
        )

    def write_preflight_evidence(self, nvs_hash: str) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence = {
            "schema": 1,
            "phase": "preflight",
            "status": "PASS",
            "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "manifest_sha256": sha256_bytes(self.manifest_path.read_bytes()),
            "table_sha256": SOURCE_TABLE_SHA256,
            "nvs_sha256": nvs_hash,
        }
        (self.evidence_dir / "preflight-report.json").write_text(
            json.dumps(evidence), encoding="utf-8"
        )

    def write_stage_evidence(self, nvs_hash: str) -> None:
        evidence = {
            "schema": 1,
            "phase": "stage",
            "status": "PASS",
            "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "manifest_sha256": sha256_bytes(self.manifest_path.read_bytes()),
            "nvs_sha256": nvs_hash,
            "staged_sha256": sha256_bytes(self.safeboot),
        }
        (self.evidence_dir / "stage-report.json").write_text(
            json.dumps(evidence), encoding="utf-8"
        )

    def enable_recovery(self) -> bytes:
        recovery = bytes((index * 31 + 7) & 0xFF for index in range(13_579))
        (self.root / "recovery.bin").write_bytes(recovery)
        self.manifest["migration_mode"] = RECOVERY_MODE
        self.manifest["artifacts"]["recovery_safeboot"] = {
            "file": "recovery.bin",
            "size": len(recovery),
            "sha256": sha256_bytes(recovery),
        }
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        return recovery

    def write_commit_evidence(self, safeboot_hash: str) -> None:
        evidence = {
            "schema": 1,
            "phase": "commit",
            "status": "PASS",
            "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "manifest_sha256": sha256_bytes(self.manifest_path.read_bytes()),
            "target_table_sha256": sha256_bytes(self.target_table),
            "safeboot_sha256": safeboot_hash,
        }
        (self.evidence_dir / "commit-report.json").write_text(
            json.dumps(evidence), encoding="utf-8"
        )


def send_chunks(state: MigrationState, kind: str, data: bytes) -> None:
    for index, offset in enumerate(range(0, len(data), SECTOR_SIZE)):
        state.receive_capture(kind, index, data[offset : offset + SECTOR_SIZE].hex().encode())


class MigrationValidationTests(unittest.TestCase):
    def test_commit_and_restore_require_separate_host_confirmations(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "REPARTITION_LOCK"
            lock_path.write_text("locked\n", encoding="utf-8")
            self.assertIn(
                "repartition lock is active",
                phase_confirmation_refusal("commit", False, False, lock_path),
            )
            lock_path.unlink()
            self.assertEqual(
                phase_confirmation_refusal("commit", False, False, lock_path),
                f"commit requires {COMMIT_FLAG}",
            )
            self.assertIsNone(
                phase_confirmation_refusal("commit", True, False, lock_path)
            )
            self.assertEqual(
                phase_confirmation_refusal("restore", False, False, lock_path),
                f"restore requires {RESTORE_FLAG}",
            )
            self.assertIsNone(
                phase_confirmation_refusal("restore", False, True, lock_path)
            )

    def test_private_recovery_header_round_trip_without_plaintext(self):
        ssid = "Home test café"
        password = 'safe-quote-"-test'
        header = render_header(ssid, password)
        self.assertEqual(header_value(header, "STA_SSID1"), ssid.encode("utf-8"))
        self.assertEqual(header_value(header, "STA_PASS1"), password.encode("utf-8"))
        self.assertNotIn(ssid.encode("utf-8"), header)
        self.assertNotIn(password.encode("utf-8"), header)

    def test_partition_fixtures_and_nvs_gate(self):
        source = parse_partition_sector(partition_sector(SOURCE_PARTITIONS))
        self.assertEqual(source["sha256"], SOURCE_TABLE_SHA256)
        report = analyze_canonical_nvs(canonical_nvs())
        self.assertTrue(report["ready"])
        self.assertEqual(report["missing_keys"], [])
        self.assertEqual(sector_aligned_size(12_345), 16_384)

    def test_nvs_gate_rejects_blob_whose_index_survives_but_chunk_does_not(self):
        nvs = bytearray(canonical_nvs())
        # Erase the Settings BLOB_DATA entry and its payload while leaving its
        # BLOB_IDX record written. Key-name-only validation would miss this.
        nvs[32] |= 0x0F
        report = analyze_canonical_nvs(bytes(nvs))
        self.assertFalse(report["ready"])
        self.assertEqual(report["missing_keys"], [])
        self.assertIn("Settings", report["keys"])
        self.assertEqual(report["incomplete_keys"], ["Settings"])

    def test_pinned_artifact_gate_rejects_manifest_substitution(self):
        manifest = {
            "official_release": official_release(),
            "artifacts": {
                name: dict(expected) for name, expected in PINNED_ARTIFACTS.items()
            },
        }
        require_pinned_artifacts(manifest)
        manifest["artifacts"]["safeboot"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "safeboot sha256 differs"):
            require_pinned_artifacts(manifest)

    def test_a_bundle_of_a_release_pinned_before_is_taken_unless_current_only(self):
        # A run that installed that release at F2, carried on by a version
        # that pins a later one, finishes with its bundle.
        from unittest import mock
        from tools import safeboot_migration as sm
        earlier = {"version": "1.0.0", "files": {
            name: {"name": name, "size": 10 + index, "sha256": f"{index}" * 64}
            for index, name in enumerate(("factory", "safeboot", "app"))}}
        manifest = {
            "official_release": {"install_commit": "abc", "tasmota_commit": "def"},
            "artifacts": {"bluetooth": dict(PINNED_ARTIFACTS["bluetooth"]),
                          **{name: {"size": f["size"], "sha256": f["sha256"]}
                             for name, f in earlier["files"].items()}},
        }
        with mock.patch.dict(sm.FIRMWARE["tasmota"], {"earlier": [earlier]}):
            self.assertEqual(sm.earlier_release(manifest), earlier)
            require_pinned_artifacts(manifest)
            with self.assertRaisesRegex(ValueError, "identity differs"):
                require_pinned_artifacts(manifest, current_only=True)
            manifest["artifacts"]["app"]["sha256"] = "f" * 64
            self.assertIsNone(sm.earlier_release(manifest))
            with self.assertRaisesRegex(ValueError, "identity differs"):
                require_pinned_artifacts(manifest)

    def test_report_parser_rejects_ambiguous_input(self):
        self.assertEqual(parse_report(b"status=PASS\nvalue=1\n"), {"status": "PASS", "value": "1"})
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_report(b"status=PASS\nstatus=FAIL\n")
        with self.assertRaisesRegex(ValueError, "not ASCII"):
            parse_report(b"status=\xff")

    def test_read_only_preflight_records_only_complete_validated_captures(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            state = fixture.state("preflight")
            rendered = state.render_berry().decode()
            self.assertNotIn("flash.write", rendered)
            self.assertNotIn("flash.erase", rendered)
            self.assertNotIn("@SOURCE", rendered)

            nvs = canonical_nvs()
            otadata = b"\xa5" * 0x2000
            send_chunks(state, "table", fixture.source_table)
            send_chunks(state, "nvs", nvs)
            send_chunks(state, "otadata", otadata)
            body = (
                f"session={state.session}\nstatus=PASS\nflash_size={0x400000}\ncurrent_ota=1\n"
                f"table_sha256={SOURCE_TABLE_SHA256.upper()}\n"
                f"nvs_sha256={sha256_bytes(nvs).upper()}\n"
                f"otadata_sha256={sha256_bytes(otadata).upper()}\n"
            ).encode()
            state.receive_report("preflight", body)
            self.assertTrue(state.complete)
            evidence = json.loads((fixture.evidence_dir / "preflight-report.json").read_text())
            self.assertEqual(evidence["status"], "PASS")
            self.assertTrue(evidence["canonical_nvs"]["ready"])

    def test_preflight_refuses_incomplete_or_changed_table(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            state = fixture.state("preflight")
            nvs = canonical_nvs()
            otadata = b"\x00" * 0x2000
            send_chunks(state, "nvs", nvs)
            send_chunks(state, "otadata", otadata)
            with self.assertRaisesRegex(ValueError, "incomplete"):
                state.receive_report(
                    "preflight", f"session={state.session}\nstatus=PASS\n".encode()
                )
            changed = bytearray(fixture.source_table)
            changed[-1] = 0
            with self.assertRaisesRegex(ValueError, "padding"):
                parse_partition_sector(bytes(changed))

    def test_preflight_records_structural_nvs_refusal_and_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            state = fixture.state("preflight")
            nvs = bytearray(canonical_nvs())
            nvs[32] |= 0x0F
            otadata = b"\x5a" * 0x2000
            send_chunks(state, "table", fixture.source_table)
            send_chunks(state, "nvs", bytes(nvs))
            send_chunks(state, "otadata", otadata)
            with self.assertRaisesRegex(ValueError, "not self-contained"):
                state.receive_report(
                    "preflight",
                    f"session={state.session}\nstatus=PASS\n".encode(),
                )
            self.assertTrue(state.complete)
            self.assertEqual(state.result_status, "FAIL")
            evidence = json.loads(
                (fixture.evidence_dir / "preflight-report.json").read_text()
            )
            self.assertEqual(evidence["status"], "FAIL")
            self.assertEqual(
                evidence["reason"], "canonical_nvs_not_self_contained"
            )
            self.assertEqual(evidence["canonical_nvs"]["incomplete_keys"], ["Settings"])

    def test_recovery_preflight_accepts_but_records_incomplete_nvs(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            state = fixture.state("preflight")
            state.recovery_mode = True
            state.manifest["migration_mode"] = RECOVERY_MODE
            nvs = bytearray(canonical_nvs())
            nvs[32] |= 0x0F
            nvs = bytes(nvs)
            otadata = b"\x6b" * 0x2000
            send_chunks(state, "table", fixture.source_table)
            send_chunks(state, "nvs", nvs)
            send_chunks(state, "otadata", otadata)
            report = (
                f"session={state.session}\nstatus=PASS\nflash_size={0x400000}\ncurrent_ota=1\n"
                f"table_sha256={SOURCE_TABLE_SHA256}\n"
                f"nvs_sha256={sha256_bytes(nvs)}\n"
                f"otadata_sha256={sha256_bytes(otadata)}\n"
            ).encode()
            state.receive_report("preflight", report)
            evidence = json.loads(
                (fixture.evidence_dir / "preflight-report.json").read_text()
            )
            self.assertEqual(evidence["status"], "PASS")
            self.assertFalse(evidence["canonical_nvs_ready"])
            self.assertEqual(evidence["migration_mode"], RECOVERY_MODE)
            self.assertEqual(evidence["canonical_nvs"]["incomplete_keys"], ["Settings"])

    def test_stage_requires_preflight_and_independent_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            with self.assertRaisesRegex(ValueError, "preflight evidence"):
                fixture.state("stage")

            nvs_hash = sha256_bytes(canonical_nvs())
            fixture.write_preflight_evidence(nvs_hash)
            state = fixture.state("stage")
            rendered = state.render_berry().decode()
            self.assertIn(
                "if count < 4096 flash.erase(stage_address + offset, 0x1000) end",
                rendered,
            )
            self.assertIn(
                sha256_bytes(
                    b"\xff"
                    * (
                        sector_aligned_size(len(fixture.safeboot))
                        - len(fixture.safeboot)
                    )
                ),
                rendered,
            )
            self.assertIn("staged Safeboot sector padding is not erased", rendered)
            # Nothing it writes is sent back: its report hashes the region.
            self.assertNotIn("/capture/staged", rendered)
            with self.assertRaisesRegex(ValueError, "unavailable"):
                send_chunks(state, "staged", fixture.safeboot)
            report = (
                f"session={state.session}\nstatus=PASS\ncurrent_ota=1\n"
                f"table_sha256={SOURCE_TABLE_SHA256}\nnvs_sha256={nvs_hash}\n"
                f"staged_sha256={sha256_bytes(fixture.safeboot)}\n"
                f"staged_size={len(fixture.safeboot)}\n"
            ).encode()
            state.receive_report("stage", report)
            self.assertEqual((state.complete, state.result_status), (True, "PASS"))
            self.assertFalse((fixture.evidence_dir / "staged-safeboot-readback.bin").exists())

    def test_commit_is_not_self_executing_and_uses_safe_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            nvs_hash = sha256_bytes(canonical_nvs())
            fixture.write_preflight_evidence(nvs_hash)
            fixture.write_stage_evidence(nvs_hash)
            state = fixture.state("commit")
            rendered = state.render_berry().decode()
            self.assertTrue(rendered.rstrip().endswith("return commit"))
            self.assertIn('tasmota.cmd("Restart 99")', rendered)
            self.assertNotRegex(rendered, r"(?m)^\s*flash\.factory\(")
            self.assertIn(state.arm_token, rendered)
            self.assertIn(sha256_bytes(fixture.target_table), rendered)

    def test_the_interim_layout_moves_only_the_low_regions_clear_of_the_commit(self):
        import tools.safeboot_migration as migration
        table = migration.interim_table()
        self.assertEqual(sha256_bytes(table), migration.INTERIM_TABLE_SHA256)
        source = {e["label"]: e for e in migration.parse_partition_sector(
            migration.SOURCE_TABLE_FILE.read_bytes())["entries"]}
        interim = {e["label"]: e for e in migration.parse_partition_sector(table)["entries"]}
        low = ("nvs", "reserve", "otadata", "phy_init")
        # Moved whole, each the same size and the same distance apart.
        for label in low:
            self.assertEqual(interim[label]["size"], source[label]["size"], label)
            self.assertEqual(interim[label]["offset"] - source[label]["offset"],
                             migration.MOVED_LOW_OFFSET - migration.LOW_REGIONS_OFFSET, label)
        self.assertEqual(interim["ota_1"], source["ota_1"])  # the Tasmota that starts
        moved = (migration.MOVED_LOW_OFFSET, migration.MOVED_LOW_OFFSET + migration.LOW_REGIONS_SIZE)
        # In what was ota_0, clear of everything the commit writes or reads its
        # copy from, and of the shrunk ota_0.
        self.assertGreaterEqual(moved[0], interim["ota_0"]["end"])
        self.assertEqual(moved[1], migration.OLD_OTA1_OFFSET)
        self.assertGreaterEqual(moved[0], migration.SAFEBOOT_STAGE_OFFSET + migration.SAFEBOOT_SIZE)
        self.assertGreaterEqual(moved[0], migration.SAFEBOOT_OFFSET + migration.SAFEBOOT_SIZE)
        with self.assertRaises(ValueError):
            migration.interim_table(b"\xff" * 4096)

    def test_the_commit_moves_the_settings_aside_before_writing_over_them(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            nvs_hash = sha256_bytes(canonical_nvs())
            fixture.write_preflight_evidence(nvs_hash)
            fixture.write_stage_evidence(nvs_hash)
            state = fixture.state("commit")
            rendered = state.render_berry().decode()
            self.assertEqual(MigrationHandler._answer_get("/interim-table", state)[1],
                             state.interim_table.hex().encode())
        import tools.safeboot_migration as migration
        self.assertIn(migration.INTERIM_TABLE_SHA256, rendered)
        commit = rendered[rendered.index("def run_commit"):]
        order = [commit.index(text) for text in (
            "move_low_regions()",                      # the settings copied aside
            "flash.write(0x8000, interim)",            # the interim table pointing at them
            "flash.write(0x10000 + offset, data)",     # only then the copy over them
            "flash.write(0x8000, target_table)")]      # and the final table last
        self.assertEqual(order, sorted(order))
        # Failing once the interim table is in place keeps it: it starts the
        # same Tasmota, with its settings, where Sonoff's would start one without.
        self.assertIn("keep_interim_layout(interim)", commit)

    def test_restore_is_armed_and_requires_official_app_before_factory_write(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            recovery = fixture.enable_recovery()
            fixture.evidence_dir.mkdir(parents=True, exist_ok=True)
            fixture.write_commit_evidence(sha256_bytes(recovery))
            state = fixture.state("restore")
            rendered = state.render_berry().decode()
            self.assertTrue(rendered.rstrip().endswith("return restore"))
            self.assertIn("official app0 must be running", rendered)
            self.assertIn("running app0 differs from pinned official image", rendered)
            self.assertIn(sha256_bytes(fixture.app), rendered)
            self.assertIn(sha256_bytes(recovery), rendered)
            self.assertIn(state.arm_token, rendered)

            self.assertNotIn("/capture/restored", rendered)
            report = (
                f"session={state.session}\nstatus=PASS\ncurrent_ota=0\n"
                f"target_table_sha256={sha256_bytes(fixture.target_table)}\n"
                f"app_sha256={sha256_bytes(fixture.app)}\n"
                f"official_safeboot_sha256={sha256_bytes(fixture.safeboot)}\n"
                f"official_safeboot_size={len(fixture.safeboot)}\n"
            ).encode()
            state.receive_report("restore", report)
            self.assertEqual((state.complete, state.result_status), (True, "PASS"))

    def factory(self, official, recovery, official_sectors):
        """The factory partition as a restore cut off after `official_sectors` leaves it."""
        from tools.serve_safeboot_migration import SAFEBOOT_SIZE, SECTOR_SIZE
        off = official + b"\xff" * (SAFEBOOT_SIZE - len(official))
        rec = recovery + b"\xff" * (SAFEBOOT_SIZE - len(recovery))
        cut = official_sectors * SECTOR_SIZE
        return off[:cut] + rec[cut:]

    def digests(self, partition):
        import hashlib
        return [hashlib.sha256(partition[i:i + SECTOR_SIZE]).digest()
                for i in range(0, len(partition), SECTOR_SIZE)]

    def test_a_restore_cut_off_part_way_carries_on_where_it_stopped(self):
        from tools.serve_safeboot_migration import restore_resume_point
        official = bytes((i * 7) & 0xFF for i in range(20_000))
        recovery = bytes((i * 11 + 3) & 0xFF for i in range(21_000))
        for done, expected in ((0, 0), (2, 2), (4, 4), (5, 5), (40, 5)):
            partition = self.factory(official, recovery, done)
            self.assertEqual(restore_resume_point(self.digests(partition), official, recovery),
                             expected, done)

    def test_a_sector_a_power_cut_came_in_is_written_again(self):
        # Seen in the fault suite: the plug switched off during G1 left the
        # sector being written erased and part written - and every later
        # restore refused, the conversion stuck for good.
        from tools.serve_safeboot_migration import restore_resume_point
        official = bytes((i * 7) & 0xFF for i in range(20_000))
        recovery = bytes((i * 11 + 3) & 0xFF for i in range(21_000))
        for written in (0, 256, 2048, SECTOR_SIZE - 256):
            partition = bytearray(self.factory(official, recovery, 2))
            sector = slice(2 * SECTOR_SIZE, 3 * SECTOR_SIZE)
            partition[sector] = official[sector][:written] + b"\xff" * (SECTOR_SIZE - written)
            self.assertEqual(restore_resume_point(self.digests(bytes(partition)), official, recovery),
                             2, written)
        # Only that one: two sectors from neither image are refused.
        partition[3 * SECTOR_SIZE + 10] ^= 0xFF
        with self.assertRaisesRegex(ValueError, "sector 3 holds neither"):
            restore_resume_point(self.digests(bytes(partition)), official, recovery)

    def test_anything_else_in_the_partition_is_refused(self):
        from tools.serve_safeboot_migration import restore_resume_point
        official = bytes((i * 7) & 0xFF for i in range(20_000))
        recovery = bytes((i * 11 + 3) & 0xFF for i in range(21_000))
        partition = bytearray(self.factory(official, recovery, 2))
        partition[3 * SECTOR_SIZE + 10] ^= 0xFF  # a sector from neither image
        with self.assertRaisesRegex(ValueError, "sector 3 holds neither"):
            restore_resume_point(self.digests(bytes(partition)), official, recovery)

    def test_unproven_whatever_follows_the_official_sectors_is_written_over(self):
        # A run begun by 1.2.0: its startup firmware, which this checkout cannot
        # rebuild, is in the partition - after any official sectors a cut left.
        from tools.serve_safeboot_migration import restore_resume_point
        official = bytes((i * 7) & 0xFF for i in range(20_000))
        theirs = bytes((i * 13 + 5) & 0xFF for i in range(22_000))
        for done, expected in ((0, 0), (2, 2), (5, 5), (40, 5)):
            partition = self.factory(official, theirs, done)
            self.assertEqual(restore_resume_point(self.digests(partition), official, None), expected, done)

    def unproven_state(self, commit_table=None, commit=True):
        """A restore with --recovery-unproven, the commit's record naming
        another startup firmware than any this checkout has."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        fixture = MigrationFixture(Path(directory.name))
        fixture.enable_recovery()
        fixture.evidence_dir.mkdir(parents=True, exist_ok=True)
        if commit:
            fixture.write_commit_evidence(sha256_bytes(b"1.2.0's own startup firmware"))
            if commit_table:
                report = json.loads((fixture.evidence_dir / "commit-report.json").read_text())
                report["target_table_sha256"] = commit_table
                (fixture.evidence_dir / "commit-report.json").write_text(json.dumps(report))
        return MigrationState(fixture.manifest_path, fixture.manifest, "restore",
                              "http://192.168.1.10:8089", fixture.evidence_dir, 7200,
                              recovery_unproven=True), fixture

    def test_unproven_needs_no_record_of_the_firmware_it_writes_over(self):
        state, fixture = self.unproven_state()
        self.assertEqual(state.safeboot, b"")
        self.assertIn('fetch_text("/restore-from")', state.render_berry().decode())
        # Any sectors after the official ones: carried on after those.
        partition = self.factory(fixture.safeboot, bytes(range(256)) * 90, 2)
        send_chunks(state, "factory-sha", b"".join(self.digests(partition)))
        self.assertEqual(state.restore_from(), 2)

    def test_unproven_without_a_commit_report_the_plugs_check_stands(self):
        state, _ = self.unproven_state(commit=False)
        self.assertIsNone(state.commit_evidence)

    def test_unproven_still_holds_the_table_to_the_commits_record(self):
        with self.assertRaisesRegex(ValueError, "different partition table"):
            self.unproven_state(commit_table="00" * 32)

    def test_unproven_is_for_the_restore_only(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            fixture.enable_recovery()
            with self.assertRaisesRegex(ValueError, "only the restore"):
                MigrationState(fixture.manifest_path, fixture.manifest, "stage",
                               "http://192.168.1.10:8089", fixture.evidence_dir, 7200,
                               recovery_unproven=True)

    def test_the_plug_is_told_where_to_carry_on(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            recovery = fixture.enable_recovery()
            fixture.evidence_dir.mkdir(parents=True, exist_ok=True)
            fixture.write_commit_evidence(sha256_bytes(recovery))
            state = fixture.state("restore")
            rendered = state.render_berry().decode()
            self.assertIn('fetch_text("/restore-from")', rendered)
            self.assertIn("var factory_sectors = 208", rendered)
            self.assertNotIn("no longer contains the recovery Safeboot", rendered)
            raw = b"".join(self.digests(self.factory(fixture.safeboot, recovery, 1)))
            send_chunks(state, "factory-sha", raw)
            self.assertEqual(state.restore_from(), 1)

    def test_restore_accepts_commit_evidence_of_any_age(self):
        # A plug left on the recovery Safeboot must still be finishable later:
        # the restore closure re-checks the live plug before it writes.
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            recovery = fixture.enable_recovery()
            fixture.evidence_dir.mkdir(parents=True, exist_ok=True)
            fixture.write_commit_evidence(sha256_bytes(recovery))
            path = fixture.evidence_dir / "commit-report.json"
            evidence = json.loads(path.read_text())
            evidence["created_utc"] = (
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
            ).isoformat()
            path.write_text(json.dumps(evidence), encoding="utf-8")
            state = fixture.state("restore")
            self.assertEqual(state.commit_evidence["status"], "PASS")

    def restore_with_commit_evidence(self, **changes):
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            recovery = fixture.enable_recovery()
            fixture.evidence_dir.mkdir(parents=True, exist_ok=True)
            fixture.write_commit_evidence(sha256_bytes(recovery))
            path = fixture.evidence_dir / "commit-report.json"
            evidence = json.loads(path.read_text())
            evidence.update(changes)
            path.write_text(json.dumps(evidence), encoding="utf-8")
            return fixture.state("restore")

    def test_a_lost_commit_report_falls_back_to_the_staging_record(self):
        # The commit reports as the plug restarts; the emulated plug cut off
        # then left no report, but the image the commit copied was staged here.
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            recovery = fixture.enable_recovery()
            nvs_hash = sha256_bytes(canonical_nvs())
            fixture.write_preflight_evidence(nvs_hash)
            fixture.write_stage_evidence(nvs_hash)
            path = fixture.evidence_dir / "stage-report.json"
            evidence = json.loads(path.read_text())
            evidence["staged_sha256"] = sha256_bytes(recovery)
            path.write_text(json.dumps(evidence), encoding="utf-8")
            self.assertIsNone(fixture.state("restore").commit_evidence)
            evidence["staged_sha256"] = "0" * 64
            path.write_text(json.dumps(evidence), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "neither commit nor stage evidence"):
                fixture.state("restore")

    def test_restore_accepts_a_bundle_changed_since_the_commit(self):
        # Set up for other Wi-Fi since, say: the manifest differs, but the
        # table and the recovery Safeboot the restore relies on do not.
        state = self.restore_with_commit_evidence(manifest_sha256="0" * 64)
        self.assertEqual(state.commit_evidence["status"], "PASS")

    def test_restore_refuses_a_commit_of_another_table(self):
        with self.assertRaisesRegex(ValueError, "different partition table"):
            self.restore_with_commit_evidence(target_table_sha256="0" * 64)

    def test_restore_refuses_a_commit_of_another_recovery_safeboot(self):
        with self.assertRaisesRegex(ValueError, "does not match recovery Safeboot"):
            self.restore_with_commit_evidence(safeboot_sha256="0" * 64)

    def staging(self, directory):
        fixture = MigrationFixture(Path(directory))
        nvs_hash = sha256_bytes(canonical_nvs())
        fixture.write_preflight_evidence(nvs_hash)
        return fixture, fixture.state("stage")

    def test_a_failure_from_the_plug_ends_the_phase_with_its_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, state = self.staging(directory)
            state.receive_failure(
                "stage",
                f"session={state.session}\nreason=migration_refused: GET /chunk/safeboot/2 "
                "returned HTTP -1 after 290 attempts\n".encode(),
            )
            self.assertTrue(state.complete)
            self.assertEqual(state.result_status, "FAIL")
            self.assertIn("HTTP -1", state.failure_reason)
            evidence = json.loads((fixture.evidence_dir / "stage-report.json").read_text())
            self.assertEqual(evidence["status"], "FAIL")
            self.assertIn("HTTP -1", evidence["reason"])

    def test_a_failure_from_another_session_or_phase_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            _, state = self.staging(directory)
            with self.assertRaisesRegex(ValueError, "session differs"):
                state.receive_failure("stage", b"session=other\nreason=x\n")
            with self.assertRaisesRegex(ValueError, "phase differs"):
                state.receive_failure("commit", f"session={state.session}\nreason=x\n".encode())
            self.assertFalse(state.complete)

    def test_a_report_already_made_is_kept(self):
        # A commit that failed while writing reports FAIL itself, with whether
        # it rolled back; the wrapper's reason after it must not replace that.
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            nvs_hash = sha256_bytes(canonical_nvs())
            fixture.write_preflight_evidence(nvs_hash)
            fixture.write_stage_evidence(nvs_hash)
            state = fixture.state("commit")
            state.receive_report("commit", (
                f"session={state.session}\nstatus=FAIL\nrollback=true\ncurrent_ota=1\n"
                f"target_table_sha256={sha256_bytes(fixture.target_table)}\n"
                f"safeboot_sha256={sha256_bytes(fixture.safeboot)}\n").encode())
            state.receive_failure("commit", f"session={state.session}\nreason=later\n".encode())
            evidence = json.loads((fixture.evidence_dir / "commit-report.json").read_text())
            self.assertEqual(evidence["rollback"], "true")
            self.assertNotIn("reason", evidence)

    def test_waiting_stops_when_the_plug_falls_silent(self):
        from unittest import mock
        import tools.serve_safeboot_migration as server_module
        with tempfile.TemporaryDirectory() as directory:
            _, state = self.staging(directory)
            clock = iter(range(0, 10_000, 30))
            with mock.patch.object(server_module.time, "monotonic", side_effect=lambda: next(clock)):
                server = mock.Mock()
                self.assertEqual(server_module.wait_for_result(server, state, 900, 360), "silent")

    def test_a_plug_still_talking_is_given_the_whole_wait(self):
        from unittest import mock
        import tools.serve_safeboot_migration as server_module
        with tempfile.TemporaryDirectory() as directory:
            _, state = self.staging(directory)
            clock = iter(range(0, 10_000, 30))
            server = mock.Mock()
            # Every request is the plug asking for something: never silent.
            server.handle_request.side_effect = lambda: setattr(state, "last_heard", now[0])
            now = [0]

            def tick():
                now[0] = next(clock)
                return now[0]
            with mock.patch.object(server_module.time, "monotonic", side_effect=tick):
                self.assertEqual(server_module.wait_for_result(server, state, 900, 360), "timeout")

    def test_every_phase_reports_its_failures_and_keeps_its_retrying_short(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            nvs_hash = sha256_bytes(canonical_nvs())
            fixture.write_preflight_evidence(nvs_hash)
            fixture.write_stage_evidence(nvs_hash)
            rendered = {phase: fixture.state(phase).render_berry().decode()
                        for phase in ("preflight", "stage", "commit")}
            recovery = fixture.enable_recovery()
            fixture.write_commit_evidence(sha256_bytes(recovery))
            rendered["restore"] = fixture.state("restore").render_berry().decode()
            for phase, source in rendered.items():
                self.assertIn(f'"/fail/{phase}"', source, phase)
                self.assertIn("report_failure(", source, phase)
                # 20s of retrying in all, for the whole phase: busy for 70-90s,
                # a real S60TPG stops answering for good (its energy meter's
                # readings pile up).  The tool waits out a router restart.
                self.assertIn("retry_spent + tasmota.millis() - self.since >= 20000", source, phase)
                self.assertNotIn("tasmota.millis(300000)", source, phase)
                self.assertIn("window.over()", source, phase)
                self.assertIn("window.done()", source, phase)
                # Not set_timeouts(): a connect timeout above the default 2s left
                # the emulated plug's web server dead once a phase gave up
                # (the HTTP timeout alone did not); the default 2s is enough.
                self.assertNotIn("set_timeouts", source, phase)
                # No request with the Wi-Fi down: webclient can crash then
                # (Tasmota #18831), and a router restart takes it down.
                lines = source.splitlines()
                for number, line in enumerate(lines):
                    if "webclient()" in line or ("get_body(" in line and "def get_body" not in line):
                        above = "\n".join(lines[max(0, number - 2):number])
                        self.assertIn('if tasmota.wifi("up")', above, f"{phase} line {number + 1}")
                self.assertIn("the plug's Wi-Fi went down and was not back", source, phase)
            # Stage, commit and restore read their downloads by hand, giving
            # up on one that goes silent: webclient's get_string() waits for
            # ever on a reply that stops part way (arendst/Tasmota#25139).
            for phase in ("stage", "commit", "restore"):
                source = rendered[phase]
                code = [line for line in source.splitlines() if not line.lstrip().startswith("#")]
                self.assertFalse([line for line in code if "get_string(" in line], phase)
                self.assertIn("quiet = tasmota.millis(5000)", source, phase)
                self.assertEqual(source.count("tcpclient()"), 1, phase)
                inside = source[source.index("def get_body(path)"):]
                self.assertIn("tcpclient()", inside[:inside.index("\nend\n")], phase)
            # Commit and restore still hand back their armed closure by name.
            self.assertTrue(rendered["commit"].rstrip().endswith("return commit"))
            self.assertTrue(rendered["restore"].rstrip().endswith("return restore"))

    def test_stage_still_refuses_stale_preflight_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            fixture.write_preflight_evidence(sha256_bytes(canonical_nvs()))
            path = fixture.evidence_dir / "preflight-report.json"
            evidence = json.loads(path.read_text())
            evidence["created_utc"] = (
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)
            ).isoformat()
            path.write_text(json.dumps(evidence), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not fresh"):
                fixture.state("stage")

    def test_a_request_abandoned_part_way_does_not_stall_the_server(self):
        # The plug's network went mid-POST in the emulator: the server sat on
        # the half-read body, and never got back to its idle check.
        import socket
        import time as clock
        from unittest import mock
        import tools.serve_safeboot_migration as server_module
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            state = fixture.state("preflight")
            with mock.patch.object(server_module.MigrationHandler, "timeout", 0.5):
                server = MigrationServer(("127.0.0.1", 0), "127.0.0.1", state)
                server.timeout = 0.2
                try:
                    stalled = socket.create_connection(server.server_address, timeout=2)
                    stalled.sendall(b"POST /capture/table/0 HTTP/1.1\r\nHost: x\r\n"
                                    b"Content-Length: 8192\r\n\r\n" + b"00" * 100)
                    started = clock.monotonic()
                    with mock.patch("builtins.print") as printed:
                        server.handle_request()
                        # Back at once: the half-read request waits in its own thread...
                        self.assertLess(clock.monotonic() - started, 1)
                        # ...and is still dropped once its timeout passes.
                        while printed.call_args is None and clock.monotonic() - started < 5:
                            clock.sleep(0.05)
                    self.assertIsNotNone(printed.call_args, "the stalled request was never dropped")
                    self.assertRegex(printed.call_args.args[0], "timed out|request dropped")
                    stalled.close()
                finally:
                    server.server_close()

    def test_a_request_cut_off_by_an_outage_does_not_hold_up_the_next(self):
        # After a network outage the plug's abandoned requests are still open
        # here, each waited on for up to MigrationHandler.timeout. The plug's
        # next request must be answered meanwhile, not after them: it gives up
        # on a reply within 5 seconds (emulator, ESPHome run with an outage).
        import http.client
        import socket
        import time as clock
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            state = fixture.state("preflight")
            server = MigrationServer(("127.0.0.1", 0), "127.0.0.1", state)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            stalled = socket.create_connection(server.server_address, timeout=2)
            try:
                stalled.sendall(b"POST /capture/table/0 HTTP/1.1\r\nHost: x\r\n"
                                b"Content-Length: 8192\r\n\r\n" + b"00" * 100)
                clock.sleep(0.2)  # taken up by the server before the next arrives
                started = clock.monotonic()
                connection = http.client.HTTPConnection(*server.server_address, timeout=4)
                connection.request("GET", "/berry/preflight.be")
                self.assertEqual(connection.getresponse().status, 200)
                self.assertLess(clock.monotonic() - started, 2)
                connection.close()
            finally:
                stalled.close()
                server.shutdown()
                server.server_close()

    def test_http_server_exposes_only_the_selected_phase_to_the_selected_device(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            state = fixture.state("preflight")
            server = MigrationServer(("127.0.0.1", 0), "127.0.0.1", state)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_address[1], timeout=2
                )
                connection.request("GET", "/berry/preflight.be")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), state.render_berry())
                connection.close()

                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_address[1], timeout=2
                )
                connection.request("GET", "/target-table")
                response = connection.getresponse()
                self.assertEqual(response.status, 404)
                response.read()
                connection.close()

                server.device_ip = "192.0.2.55"
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_address[1], timeout=2
                )
                connection.request("GET", "/berry/preflight.be")
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()


class ChunkBodyTests(unittest.TestCase):
    """Each chunk goes to the plug with its SHA-256, which the plug checks
    before writing it - asking again for one damaged on its way."""

    def test_the_hash_follows_the_hex(self):
        import hashlib
        from tools.serve_safeboot_migration import chunk_body
        chunk = bytes(range(256)) * 16
        body = chunk_body(chunk).decode("ascii")
        self.assertEqual(body[:-64], chunk.hex())
        self.assertEqual(body[-64:], hashlib.sha256(chunk).hexdigest())

    def test_every_phase_that_fetches_chunks_checks_them_and_asks_again(self):
        for phase in ("stage", "restore", "plan"):
            source = (Path(__file__).resolve().parents[1] / f"repartition/berry/{phase}.be.tmpl").read_text()
            fetch = source[source.index("def fetch_chunk"):]
            fetch = fetch[:fetch.index("\nend\n")]
            with self.subTest(phase=phase):
                self.assertIn("expected_length * 2 + 64", source)
                self.assertIn("digest(data) != sent", source)
                # A damaged chunk is a failed try, inside the retry loop.
                self.assertLess(fetch.index("chunk_data(reply[1]"), fetch.index("window.over()"))
                self.assertIn("came damaged", fetch)


class PlanPhaseTests(unittest.TestCase):
    """The plan phase: whatever tools/autoflash/plans.py planned, carried out by
    one Berry program, every write read back and compared byte for byte."""

    IMAGE = b"\xe9" + bytes((index * 13 + 5) & 0xFF for index in range(10_000))
    BOOTLOADER = esphome_files.published_bootloader()

    def state(self, fixture, plan=None):
        return MigrationState(fixture.manifest_path, fixture.manifest, "plan",
                              "http://192.168.1.10:8089", fixture.evidence_dir, 7200,
                              plan=plan or plans.esphome(self.IMAGE))

    def report(self, state, **changes):
        fields = {"session": state.session, "status": "PASS", "plan": state.plan.name,
                  **state.plan.expected()}
        fields.update(changes)
        return "".join(f"{key}={value}\n" for key, value in fields.items()).encode()

    def test_its_berry_is_rendered_whole(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = plans.leave_tasmota(self.IMAGE, esphome_files.sonoff_table(), self.BOOTLOADER)
            rendered = self.state(MigrationFixture(Path(directory)), plan).render_berry().decode()
        self.assertNotRegex(rendered, r"@[A-Z][A-Z0-9_]+@")
        for blob in ("bootloader", "mover", "table"):
            self.assertIn(f'"blob":"{blob}"', rendered)
        self.assertIn(plans.TASMOTA_TABLE_SHA256, rendered)   # the layout it starts from
        self.assertIn(plans.SONOFF_TABLE_SHA256, rendered)    # and the one it ends with
        self.assertIn("/fail/plan", rendered)
        # A whole write is checked in full before its first byte is written.
        whole = rendered[rendered.index("def write_whole"):rendered.index("def run_plan")]
        self.assertLess(whole.index("SHA-256 differs"), whole.index("flash.write("))

    def test_a_copy_is_reported_as_copied_and_its_bytes_never_reach_this_computer(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            fixture.evidence_dir.mkdir(parents=True)
            state = self.state(fixture, plans.sonoff_layout_back(esphome_files.sonoff_table()))
            rendered = state.render_berry().decode()
            self.assertIn("def copy_region", rendered)
            # Run again once its table is written, it must not copy the old
            # settings back over what the plug has saved since.
            self.assertIn("if !table_written copy_region(step) end", rendered)
            state.receive_report("plan", self.report(state))
            self.assertEqual((state.complete, state.result_status), (True, "PASS"))
            evidence = json.loads((fixture.evidence_dir / "plan-sonoff_layout-report.json").read_text())
            self.assertEqual(evidence["steps"][0], {"copy": "0x1F9000", "to": "0x009000", "bytes": 0x17000})

    def test_it_serves_each_piece_and_passes_on_an_exact_read_back(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            fixture.evidence_dir.mkdir(parents=True)
            state = self.state(fixture)
            served = b"".join(state.plan_chunk("esphome", i) for i in range(3))
            self.assertEqual(served, self.IMAGE)
            with self.assertRaises(ValueError):
                state.plan_chunk("esphome", 3)
            with self.assertRaises(ValueError):
                state.plan_chunk("nothing", 0)
            # As Berry writes them: hashes in capitals.
            state.receive_report("plan", self.report(state, step0=state.plan.expected()["step0"].upper()))
            self.assertEqual((state.complete, state.result_status), (True, "PASS"))
            evidence = (fixture.evidence_dir / "plan-esphome-report.json").read_text()
            self.assertEqual(json.loads(evidence)["steps"][1], {"erase": "0x01D000", "bytes": 0x2000})
            # The image holds the Wi-Fi password: none of its bytes are kept.
            self.assertNotIn(self.IMAGE[:64].hex(), evidence)
            self.assertEqual(sorted(p.name for p in fixture.evidence_dir.iterdir()),
                             ["plan-esphome-report.json"])

    def test_nothing_it_writes_is_sent_back(self):
        # Each write's hash, read back on the plug, is in its report; the
        # bytes no longer cross the network a second time.
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(MigrationFixture(Path(directory)))
            self.assertNotIn("/capture/", state.render_berry().decode())
            with self.assertRaisesRegex(ValueError, "unavailable"):
                send_chunks(state, "esphome", self.IMAGE)

    def test_a_write_reported_one_byte_off_is_refused(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(MigrationFixture(Path(directory)))
            wrong = bytearray(self.IMAGE)
            wrong[5000] ^= 1
            with self.assertRaises(ValueError):
                state.receive_report("plan", self.report(state, step0=hashlib.sha256(wrong).hexdigest()))
            self.assertFalse(state.complete)

    def test_a_report_that_differs_from_the_plan_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(MigrationFixture(Path(directory)))
            with self.assertRaises(ValueError):
                state.receive_report("plan", self.report(state, step1="kept"))
            with self.assertRaises(ValueError):
                state.receive_report("plan", self.report(state, step0="0" * 64))
            with self.assertRaises(ValueError):
                state.receive_report("plan", self.report(state, plan="leave_tasmota"))
            self.assertFalse(state.complete)

    def test_a_failure_is_kept_under_the_plans_name(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            fixture.evidence_dir.mkdir(parents=True)
            state = self.state(fixture)
            state.receive_failure("plan", f"session={state.session}\nreason=the table differs\n".encode())
            report = json.loads((fixture.evidence_dir / "plan-esphome-report.json").read_text())
            self.assertEqual((report["status"], report["reason"]), ("FAIL", "the table differs"))

    def test_it_needs_a_plan_and_other_phases_take_none(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = MigrationFixture(Path(directory))
            with self.assertRaisesRegex(ValueError, "plan-from-stdin"):
                MigrationState(fixture.manifest_path, fixture.manifest, "plan",
                               "http://192.168.1.10:8089", fixture.evidence_dir, 7200)
            with self.assertRaisesRegex(ValueError, "takes no plan"):
                MigrationState(fixture.manifest_path, fixture.manifest, "preflight",
                               "http://192.168.1.10:8089", fixture.evidence_dir, 7200,
                               plan=plans.esphome(self.IMAGE))
