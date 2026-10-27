"""Plans: what the plug is to write, checked before anything is sent."""
import struct
import unittest

from tools.autoflash import esphome, plans

SECTOR = plans.SECTOR


def stock_flash(stock_slot=0, app_size=0x163874):
    """A 4 MiB flash laid out as Sonoff's, its firmware in `stock_slot`."""
    from tools.autoflash.imprint import parse_esp_image  # noqa: F401 - its format is what is built
    flash = bytearray(b"\xff" * 0x400000)
    flash[0x8000:0x9000] = esphome.sonoff_table()
    flash[0x9000:0x1D000] = bytes(range(256)) * (0x14000 // 256)
    flash[0x1F000:0x20000] = b"\x07" * SECTOR
    for slot, project in ((stock_slot, b"esp32_fw"), (1 - stock_slot, b"autoflash_bridge")):
        at = plans.SONOFF_OTA[slot]
        flash[at:at + 4] = b"\xe9\x01\x02\x2f"
        flash[at + 80:at + 112] = project.ljust(32, b"\0")  # as an app description pads it
    return bytes(flash), app_size


class PlanTests(unittest.TestCase):
    def test_every_plan_stays_inside_the_time_a_plug_may_be_busy(self):
        image = esphome.published_image()
        for plan in (plans.bootloader(esphome.published_bootloader()), plans.esphome(image),
                     plans.leave_tasmota(esphome.mover_for("Home", "pw"), esphome.sonoff_table(),
                                         esphome.published_bootloader())):
            self.assertLessEqual(plan.busy_estimate(), plans.BUSY_LIMIT, plan.name)

    def test_a_plan_travels_whole_and_says_the_same_on_arrival(self):
        plan = plans.leave_tasmota(esphome.mover_for("Home", "pw"), esphome.sonoff_table(),
                                   esphome.published_bootloader())
        again = plans.Plan.from_bundle(plan.bundle())
        self.assertEqual(again.expected(), plan.expected())
        self.assertEqual(again.final_table_sha256(), plans.SONOFF_TABLE_SHA256)
        self.assertEqual(again.table_sha256, plans.TASMOTA_TABLE_SHA256)
        with self.assertRaises(plans.PlanError):
            plans.Plan.from_bundle(plan.bundle()[:-1])  # cut short

    def test_leaving_tasmota_writes_in_front_of_it_and_the_table_last(self):
        plan = plans.leave_tasmota(esphome.mover_for("Home", "pw"), esphome.sonoff_table(),
                                   esphome.published_bootloader())
        ends = [end for _, end, _ in plan.regions()]
        self.assertLessEqual(max(ends[:3]), plans.TASMOTA_APP0)  # never where Tasmota runs
        self.assertEqual(plan.steps[-1], plans.Write("table", plans.TABLE_ADDRESS, whole=True))
        self.assertEqual(plan.running, 0)
        with self.assertRaisesRegex(plans.PlanError, "fit"):
            plans.leave_tasmota(b"\xe9" * 0xC1000, esphome.sonoff_table(), esphome.published_bootloader())
        with self.assertRaisesRegex(plans.PlanError, "Sonoff's partition table"):
            plans.leave_tasmota(b"\xe9" * 1000, b"\0" * SECTOR, esphome.published_bootloader())

    def test_putting_sonoffs_layout_back_copies_the_settings_home_then_the_table(self):
        plan = plans.sonoff_layout_back(esphome.sonoff_table())
        self.assertEqual((plan.table_sha256, plan.running, plan.restart),
                         (plans.INTERIM_TABLE_SHA256, 1, True))
        self.assertEqual(plan.steps, [plans.Copy(plans.MOVED_LOW_OFFSET, plans.LOW_REGIONS_OFFSET,
                                                 plans.LOW_REGIONS_SIZE),
                                      plans.Write("table", plans.TABLE_ADDRESS, whole=True)])
        self.assertEqual(plan.final_table_sha256(), plans.SONOFF_TABLE_SHA256)
        # The settings are the plug's own: what was copied cannot be known here.
        self.assertEqual(plan.expected()["step0"], "copied")
        self.assertLessEqual(plan.busy_estimate(), plans.BUSY_LIMIT)
        again = plans.Plan.from_bundle(plan.bundle())
        self.assertEqual((again.steps, again.expected()), (plan.steps, plan.expected()))
        self.assertIn('"op":"copy"', plan.berry())
        with self.assertRaisesRegex(plans.PlanError, "Sonoff's partition table"):
            plans.sonoff_layout_back(b"\0" * SECTOR)

    def test_a_copy_from_where_the_plan_writes_is_refused(self):
        plan = plans.Plan("x", "x", plans.SONOFF_TABLE_SHA256, 1,
                          steps=[plans.Copy(0x20000, 0x9000, SECTOR), plans.Erase(0x20000, SECTOR)])
        with self.assertRaisesRegex(plans.PlanError, "copy is from a region the plan writes"):
            plan.check()
        plan = plans.Plan("x", "x", plans.SONOFF_TABLE_SHA256, 1, steps=[plans.Copy(0x20000, 0x9000, 100)])
        with self.assertRaisesRegex(plans.PlanError, "sector-aligned"):
            plan.check()

    def test_overlapping_writes_and_too_much_at_once_are_refused(self):
        plan = plans.Plan("x", "x", plans.SONOFF_TABLE_SHA256, 1,
                          steps=[plans.Write("a", 0x20000), plans.Erase(0x20000, SECTOR)],
                          blobs={"a": b"\xe9" * 100})
        with self.assertRaisesRegex(plans.PlanError, "overlap"):
            plan.check()
        plan = plans.Plan("x", "x", plans.SONOFF_TABLE_SHA256, 1,
                          steps=[plans.Write("a", 0x20000)], blobs={"a": b"\xe9" * 0x1F0000})
        with self.assertRaisesRegex(plans.PlanError, "split it"):
            plan.check()
        plan = plans.Plan("x", "x", plans.SONOFF_TABLE_SHA256, 1,
                          steps=[plans.Write("a", 0x20001)], blobs={"a": b"\xe9"})
        with self.assertRaisesRegex(plans.PlanError, "aligned"):
            plan.check()

    def test_the_boot_selection_is_espidfs_own(self):
        # As in Sonoff's own flash: sequence 11 starts ota_0, its CRC 0x28fe78fe.
        entry = plans.otadata_selecting(1)
        sequence, _, state, crc = struct.unpack_from("<I20sII", entry)
        self.assertEqual((sequence - 1) % 2, 1)
        self.assertEqual(state, 0xFFFFFFFF)
        import binascii
        self.assertEqual(binascii.crc32(struct.pack("<I", 11), 0xFFFFFFFF) & 0xFFFFFFFF, 0x28FE78FE)
        self.assertEqual(crc, binascii.crc32(struct.pack("<I", sequence), 0xFFFFFFFF) & 0xFFFFFFFF)
        self.assertEqual(len(entry), plans.OTADATA_SIZE)
        self.assertEqual(set(entry[SECTOR:]), {0xFF})  # the other entry invalid


class BackToSonoffTests(unittest.TestCase):
    def plans_for(self, stock_slot, running, app_size=0x163874):
        flash, size = stock_flash(stock_slot, app_size)
        from unittest import mock
        with mock.patch.object(plans, "app_length", return_value=size):
            return plans.back_to_sonoff(flash, running=running)

    def test_sonoffs_firmware_is_split_and_selected_last(self):
        first, second = self.plans_for(stock_slot=0, running=1)
        self.assertFalse(first.restart)
        self.assertTrue(second.restart)
        writes = [s for s in first.steps + second.steps if isinstance(s, plans.Write)]
        self.assertEqual(writes[0].address, plans.SONOFF_OTA[0])
        self.assertEqual(writes[1].address, plans.SONOFF_OTA[0] + writes[0].length)
        # ota_0 is chosen by clearing the boot selection, as the last step.
        self.assertEqual(second.steps[-1], plans.Erase(plans.SONOFF_OTADATA, plans.OTADATA_SIZE))
        for plan in (first, second):
            self.assertLessEqual(plan.busy_estimate(), plans.BUSY_LIMIT)

    def test_firmware_kept_in_ota_1_gets_a_boot_selection_written(self):
        _, second = self.plans_for(stock_slot=1, running=0)
        self.assertEqual(second.steps[-1], plans.Write("otadata", plans.SONOFF_OTADATA, whole=True))
        self.assertEqual(second.blobs["otadata"], plans.otadata_selecting(1))

    def test_it_never_writes_the_slot_the_plug_runs_from(self):
        with self.assertRaisesRegex(plans.PlanError, "running from"):
            self.plans_for(stock_slot=1, running=1)

    def test_a_backup_of_something_else_is_refused(self):
        flash, _ = stock_flash()
        with self.assertRaisesRegex(plans.PlanError, "not a backup"):
            plans.back_to_sonoff(flash[:-1], running=1)
        broken = bytearray(flash)
        broken[0x8000] ^= 1
        with self.assertRaisesRegex(plans.PlanError, "not a backup"):
            plans.back_to_sonoff(bytes(broken), running=1)


class PlanCheckTests(unittest.TestCase):
    """Plan.check, the last word before a plan reaches the plug: each refusal
    for its own reason, and each limit allowed up to it and refused past it."""

    FLASH = 0x400000

    @staticmethod
    def plan(*steps, name="x", **blobs):
        return plans.Plan(name, "x", plans.SONOFF_TABLE_SHA256, 1, steps=list(steps), blobs=blobs)

    def refused(self, plan, reason):
        with self.assertRaises(plans.PlanError) as refusal:
            plan.check()
        self.assertEqual(str(refusal.exception), reason)

    def allowed(self, plan):
        self.assertIs(plan.check(), plan)

    def test_a_name_that_is_not_a_plain_word(self):
        self.refused(self.plan(name="two words"), "plan name 'two words' is not a plain word")

    def test_a_write_of_data_it_does_not_have(self):
        self.refused(self.plan(plans.Write("b", 0x20000), a=b"\xe9"), "x: no data called b")

    def test_a_write_off_a_sector(self):
        data = b"\xe9" * (2 * SECTOR)
        self.refused(self.plan(plans.Write("a", 0x20001), a=data), "x: a is not sector-aligned")
        self.refused(self.plan(plans.Write("a", 0x20000, start=1), a=data), "x: a is not sector-aligned")
        self.allowed(self.plan(plans.Write("a", 0x20000, start=SECTOR), a=data))

    def test_a_write_of_nothing_or_past_its_data(self):
        data = b"\xe9" * (SECTOR + 100)
        self.refused(self.plan(plans.Write("a", 0x20000, length=0), a=data), "x: a slice is outside it")
        self.allowed(self.plan(plans.Write("a", 0x20000, length=1), a=data))
        self.refused(self.plan(plans.Write("a", 0x20000, start=SECTOR, length=101), a=data),
                     "x: a slice is outside it")
        self.allowed(self.plan(plans.Write("a", 0x20000, start=SECTOR, length=100), a=data))

    def test_a_whole_write_held_in_the_plugs_memory(self):
        self.allowed(self.plan(plans.Write("a", 0x20000, whole=True), a=b"\xe9" * plans.WHOLE_LIMIT))
        self.refused(self.plan(plans.Write("a", 0x20000, whole=True), a=b"\xe9" * (plans.WHOLE_LIMIT + 1)),
                     "x: a is too big to write whole")

    def test_an_erase_off_a_sector_or_of_nothing(self):
        for erase in (plans.Erase(0x20001, SECTOR), plans.Erase(0x20000, SECTOR + 1), plans.Erase(0x20000, 0)):
            self.refused(self.plan(erase), "x: an erase is not sector-aligned")
        self.allowed(self.plan(plans.Erase(0x20000, SECTOR)))

    def test_a_copy_off_a_sector_or_of_nothing(self):
        for copy in (plans.Copy(0x20001, 0x30000, SECTOR), plans.Copy(0x20000, 0x30001, SECTOR),
                     plans.Copy(0x20000, 0x30000, SECTOR + 1), plans.Copy(0x20000, 0x30000, 0)):
            self.refused(self.plan(copy), "x: a copy is not sector-aligned")
        self.allowed(self.plan(plans.Copy(0x20000, 0x30000, SECTOR)))

    def test_a_step_of_no_known_kind(self):
        self.refused(self.plan("erase it"), "x: unknown step 'erase it'")

    def test_steps_that_overlap(self):
        erase, write = plans.Erase(0x20000, 2 * SECTOR), plans.Write("a", 0x21000)
        self.refused(self.plan(erase, write, a=b"\xe9"), f"x: {erase} and {write} overlap")
        self.allowed(self.plan(plans.Erase(0x20000, SECTOR), write, a=b"\xe9"))

    def test_a_step_outside_the_flash(self):
        self.allowed(self.plan(plans.Write("a", self.FLASH - SECTOR), a=b"\xe9" * SECTOR))
        for step in (plans.Erase(self.FLASH, SECTOR), plans.Erase(-SECTOR, SECTOR)):
            self.refused(self.plan(step), "x: a step is outside the 4 MiB flash")

    def test_a_copy_from_outside_the_flash(self):
        self.allowed(self.plan(plans.Copy(self.FLASH - SECTOR, 0x20000, SECTOR)))
        self.refused(self.plan(plans.Copy(self.FLASH, 0x20000, SECTOR)),
                     "x: a copy is from outside the 4 MiB flash")

    def test_a_copy_from_beside_a_region_written_is_allowed(self):
        # Only a copy from where the plan writes is refused
        # (test_a_copy_from_where_the_plan_writes_is_refused): one next to it is not.
        self.allowed(self.plan(plans.Copy(0x20000, 0x9000, SECTOR), plans.Erase(0x21000, SECTOR)))
        self.allowed(self.plan(plans.Copy(0x21000, 0x9000, SECTOR), plans.Erase(0x20000, SECTOR)))

    def test_a_plan_keeping_the_plug_busy_too_long(self):
        from unittest import mock
        plan = self.plan(plans.Write("a", 0x20000), a=b"\xe9" * 0x100000)
        with mock.patch.object(plans, "BUSY_LIMIT", plan.busy_estimate()):
            self.allowed(plan)
        with mock.patch.object(plans, "BUSY_LIMIT", plan.busy_estimate() - 0.01):
            with self.assertRaisesRegex(plans.PlanError, "keeps the plug busy past .* s; split it"):
                plan.check()


if __name__ == "__main__":
    unittest.main()
