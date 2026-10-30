"""bk-emu.sh hands the container its own process: a signal to the script - a
timeout's, a Ctrl-C - reaches the docker client and so the container, instead
of leaving both running behind a script that has gone."""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "beken" / "emulator" / "bk-emu.sh"
PLUG_NET = ROOT / "tools" / "beken" / "emulator" / "plug-net.sh"


@unittest.skipIf(shutil.which("bash") is None, "no bash")
class ContainerIsExecedTests(unittest.TestCase):
    def test_docker_runs_as_the_script_itself(self):
        function = re.search(r"^container\(\) \{.*?^\}", SCRIPT.read_text(encoding="utf-8"), re.S | re.M).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp, "docker")
            fake.write_text("#!/bin/sh\necho \"docker pid $$: $*\"\n", encoding="utf-8")
            fake.chmod(0o755)
            script = ("set -u\nROOT=%s FIRMWARES=%s/firmwares IMAGE=img S61_DUMP=dump.bin\n%s\n"
                      "echo \"shell pid $$\"\ncontainer python -c pass\necho \"still here\"\n"
                      % (tmp, tmp, function))
            done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                                  env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"}, cwd=tmp)
        self.assertEqual(done.returncode, 0, done.stderr)
        shell, docker = re.search(r"shell pid (\d+)", done.stdout), re.search(r"docker pid (\d+)", done.stdout)
        self.assertEqual(shell.group(1), docker.group(1), done.stdout)
        self.assertNotIn("still here", done.stdout)


@unittest.skipIf(shutil.which("bash") is None, "no bash")
class PlugNetRouterTests(unittest.TestCase):
    """plug-net.sh makes the container the plug's home router: .1 on the
    network, the plug's pool where Docker hands out nothing, and every
    connection the plug makes off the network sent to the stub there."""

    def commands(self, address="198.18.61.130/24", then=None):
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp, "calls")
            state = Path(tmp, "state")
            state.mkdir()
            record = '#!/bin/sh\necho "$(basename "$0") $*" >> %s\n' % calls
            fakes = {
                "ip": record + "case \"$*\" in\n"
                      "  '-4 -o address show dev eth0') echo '2: eth0    inet %s brd x scope global eth0';;\n"
                      "esac\n" % address,
                "iptables": record, "dnsmasq": record, "setpriv": record, "chown": record,
                # The stub makes its CA, which plug-net.sh waits for.
                "python3": record + "touch %s/ca.crt %s/leaf.crt %s/leaf.key\n" % (state, state, state),
            }
            for name, text in fakes.items():
                Path(tmp, name).write_text(text, encoding="utf-8")
                Path(tmp, name).chmod(0o755)
            done = subprocess.run(["bash", str(PLUG_NET)], capture_output=True, text=True, timeout=60,
                                  env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}",
                                       "PLUG_NET_RUN": str(Path(tmp, "run")), "PLUG_STATE": str(state),
                                       "HOST_UID": "1000", "HOST_GID": "1000"})
            if then is not None:
                Path(tmp, "run", "leases").write_text("1 12:34:56:ab:cd:ef 198.18.61.50 plug *\n", encoding="utf-8")
                calls.write_text("", encoding="utf-8")
                subprocess.run(["sh", str(Path(tmp, "run", "router")), *then], capture_output=True, timeout=30,
                               env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"})
                self.leases = Path(tmp, "run", "leases").read_text(encoding="utf-8")
            return done, calls.read_text(encoding="utf-8") if calls.exists() else ""

    def test_the_router_starts_again_forgetting_the_plugs_address(self):
        # As a router may, when a device restarts: the fault suite's readdress.
        done, calls = self.commands(then=["55"])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("--dhcp-range=198.18.61.55,198.18.61.59,", calls)
        self.assertEqual(self.leases, "")

    def test_the_container_is_the_plugs_router_and_cloud(self):
        done, calls = self.commands()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("ip address add 198.18.61.1/24 dev br0", calls)
        dnsmasq = next(line for line in calls.splitlines() if line.startswith("dnsmasq "))
        self.assertIn("--dhcp-range=198.18.61.50,198.18.61.59,", dnsmasq)
        self.assertIn("--dhcp-option=option:router,198.18.61.1", dnsmasq)
        self.assertIn("--address=/#/198.18.61.1", dnsmasq)
        self.assertIn("-s 198.18.61.48/28 ! -d 198.18.61.0/24 -j DNAT --to-destination 198.18.61.1", calls)
        self.assertIn("--bind 198.18.61.1", calls)

    def test_a_network_not_a_24_is_refused(self):
        done, calls = self.commands("10.0.0.5/16")
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("br0", calls)

if __name__ == "__main__":
    unittest.main()
