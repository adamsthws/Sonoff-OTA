import copy
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tools.repo import cloudflare_settings as cs

ZONE = "/zones/z1"
ENTRYPOINT = f"{ZONE}/rulesets/phases/{cs.REDIRECT_PHASE}/entrypoint"


class FakeCloudflare:
    """Cloudflare's API for one zone, as tools/repo/cloudflare_settings.py sees it."""

    def __init__(self):
        self.settings = {"always_use_https": "off", "min_tls_version": "1.0"}
        self.redirects = None
        self.sent = []

    def __call__(self, path, method="GET", body=None, ok_missing=False):
        if method != "GET":
            self.sent.append((method, path, body))
            if path == ENTRYPOINT:
                # Cloudflare gives each rule its id and version, and keeps the rest as sent.
                self.redirects = [{"id": "r1", "version": "1", **copy.deepcopy(rule)} for rule in body["rules"]]
            else:
                self.settings[path.rsplit("/", 1)[1]] = body["value"]
            return None
        if path == f"/zones?name={cs.ZONE}":
            return [{"id": "z1", "name": cs.ZONE}]
        if path.startswith(f"{ZONE}/settings/"):
            return {"id": path.rsplit("/", 1)[1], "value": self.settings[path.rsplit("/", 1)[1]]}
        if path == ENTRYPOINT:
            assert ok_missing
            return None if self.redirects is None else {"phase": cs.REDIRECT_PHASE, "rules": self.redirects}
        raise AssertionError(f"unexpected {method} {path}")


def run(fake, *args):
    out = io.StringIO()
    with mock.patch.object(cs, "api", fake), redirect_stdout(out):
        code = cs.main(list(args))
    return code, out.getvalue()


class CloudflareSettingsTests(unittest.TestCase):
    def test_check_only_says_what_differs(self):
        fake = FakeCloudflare()
        code, out = run(fake)
        self.assertEqual((code, fake.sent), (1, []))
        self.assertIn("always_use_https: is off, should be on", out)
        self.assertIn("redirects: are [], should be ['www-to-apex']", out)

    def test_apply_sets_them_and_then_sends_nothing(self):
        fake = FakeCloudflare()
        self.assertEqual(run(fake, "apply")[0], 0)
        self.assertEqual(fake.settings, cs.SETTINGS)
        self.assertEqual([cs.comparable(rule) for rule in fake.redirects], cs.REDIRECTS)
        fake.sent.clear()
        code, out = run(fake, "apply")
        self.assertEqual((code, fake.sent), (0, []))
        self.assertIn("every setting is as tools/repo/cloudflare_settings.py says", out)

    def test_a_changed_redirect_is_put_back(self):
        fake = FakeCloudflare()
        run(fake, "apply")
        fake.redirects[0]["action_parameters"]["from_value"]["status_code"] = 302
        fake.sent.clear()
        run(fake, "apply")
        self.assertEqual(fake.sent, [("PUT", ENTRYPOINT, {"rules": cs.REDIRECTS})])

    def test_www_goes_to_the_domain_wrangler_serves(self):
        config = (Path(__file__).resolve().parents[2] / "website/wrangler.jsonc").read_text(encoding="utf-8")
        for host in (cs.ZONE, f"www.{cs.ZONE}"):
            self.assertIn(f'{{ "pattern": "{host}", "custom_domain": true }}', config)
        self.assertEqual(cs.REDIRECTS[0]["expression"], f'(http.host eq "www.{cs.ZONE}")')

    def test_the_token_comes_from_the_environment_or_env(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            with mock.patch.object(cs.credentials, "ENV_FILE", env_file), \
                    mock.patch.dict("os.environ", {"CLOUDFLARE_API_TOKEN": ""}):
                with self.assertRaisesRegex(cs.SettingsError, "no CLOUDFLARE_API_TOKEN"):
                    cs.token()
                env_file.write_text("CLOUDFLARE_API_TOKEN=from-the-file\n", encoding="utf-8")
                self.assertEqual(cs.token(), "from-the-file")
                with mock.patch.dict("os.environ", {"CLOUDFLARE_API_TOKEN": "from-the-environment"}):
                    self.assertEqual(cs.token(), "from-the-environment")
                    # The file is not even read: a line it cannot parse is no matter.
                    env_file.write_text("not a setting\n", encoding="utf-8")
                    self.assertEqual(cs.token(), "from-the-environment")


if __name__ == "__main__":
    unittest.main()
