"""Offline tests for RAM-only secrets loaded from an Ansible vault (unlock.py)."""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fakesys  # noqa: E402
from homelab_dash import apps, config, unlock  # noqa: E402
from homelab_dash.util import CheckError, read_secret  # noqa: E402

VAULT = """---
# decrypted output of: ansible-vault view homelab.yml
pihole_password: "p@ss: with # hash and \\"quotes\\""
jellyfin_api_key: 0123456789abcdef   # trailing comment
plex_token: 'it''s-a-token'
unrelated_db_password: hunter2
nested:
  pihole_password: not-this-one
list_key:
  - a
"""


class FakeStdin(io.StringIO):
    def isatty(self):
        return False


class TestParse(unittest.TestCase):
    def test_flat_yaml(self):
        found, others = unlock.parse_vault(VAULT, {"pihole_password", "jellyfin_api_key", "plex_token"})
        self.assertEqual(found["pihole_password"], 'p@ss: with # hash and "quotes"')
        self.assertEqual(found["jellyfin_api_key"], "0123456789abcdef")
        self.assertEqual(found["plex_token"], "it's-a-token")
        self.assertEqual(others, 3)  # unrelated_db_password, nested, list_key

    def test_complex_values_refused_without_leaking(self):
        for text in ("pihole_password: |\n  secretblock\n", "pihole_password: [a, b]\n",
                     'pihole_password: "unterminated secret\n', "pihole_password:\n"):
            with self.assertRaises(unlock.UnlockError) as cm:
                unlock.parse_vault(text, {"pihole_password"})
            self.assertNotIn("secret", str(cm.exception).replace("secrets", ""))

    def test_map(self):
        self.assertEqual(unlock.parse_map("a=x.pass b=y.key"), {"a": "x.pass", "b": "y.key"})
        for bad in ("a", "a=../etc/passwd", "a=.hidden", "a=sub/dir"):
            with self.assertRaises(unlock.UnlockError):
                unlock.parse_map(bad)

    def test_fs_type(self):
        d = tempfile.mkdtemp()
        mounts = os.path.join(d, "mounts")
        with open(mounts, "w") as fh:
            fh.write("/dev/sda1 / ext4 rw 0 0\ntmpfs /run tmpfs rw 0 0\n")
        self.assertEqual(unlock.fs_type("/run/homelab-dash-secrets", mounts), "tmpfs")
        self.assertEqual(unlock.fs_type("/etc/homelab-dash", mounts), "ext4")
        self.assertEqual(unlock.fs_type("/runx", mounts), "ext4")
        with self.assertRaises(unlock.UnlockError) as cm:
            unlock.prepare_dir("/etc/homelab-dash/x", mounts_file=mounts)
        self.assertIn("not RAM", str(cm.exception))
        shutil.rmtree(d)

    def test_swap_warning(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "swaps")
        with open(p, "w") as fh:
            fh.write("Filename Type Size Used Priority\n/dev/zram0 partition 1 0 100\n")
        self.assertIsNone(unlock.swap_warning(p))
        with open(p, "a") as fh:
            fh.write("/swap.img file 2 0 -2\n")
        self.assertIn("/swap.img", unlock.swap_warning(p))
        shutil.rmtree(d)


class TestUnlockFlow(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.dir = os.path.join(self.base, "ram")
        self.cfg = os.path.join(self.base, "c.ini")
        with open(self.cfg, "w") as fh:
            fh.write("[vault]\ndir = %s\nmap = pihole_password=pihole.pass "
                     "jellyfin_api_key=jellyfin.key plex_token=plex.token emby_key=emby.key\n" % self.dir)

    def tearDown(self):
        shutil.rmtree(self.base)

    def run_main(self, args, text=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = unlock.main(["--config", self.cfg] + args, stdin=FakeStdin(text), check_fs=False)
        return code, out.getvalue(), err.getvalue()

    def test_unlock_status_lock(self):
        code, out, err = self.run_main([], VAULT)
        self.assertEqual(code, 0, err)
        for secret in ("p@ss", "0123456789abcdef", "it's-a-token", "hunter2"):
            self.assertNotIn(secret, out + err)
        self.assertIn("not in this vault: emby_key", out)
        self.assertEqual(os.stat(self.dir).st_mode & 0o777, 0o700)
        p = os.path.join(self.dir, "pihole.pass")
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        self.assertEqual(read_secret(p), 'p@ss: with # hash and "quotes"')
        self.assertEqual(sorted(os.listdir(self.dir)), ["jellyfin.key", "pihole.pass", "plex.token"])

        code, out, _ = self.run_main(["--status"])
        self.assertIn("NOT loaded", out)  # emby
        self.assertEqual(out.count("loaded\n") - out.count("NOT loaded\n"), 3)

        code, out, _ = self.run_main(["--lock"])
        self.assertEqual(os.listdir(self.dir), [])
        with self.assertRaises(CheckError) as cm:
            read_secret(p)
        self.assertIn("secret file missing", str(cm.exception))

    def test_no_mapped_keys(self):
        code, _, err = self.run_main([], "other: x\n")
        self.assertEqual(code, 1)
        self.assertIn("none of the mapped keys", err)

    def test_refuses_tty(self):
        class Tty(FakeStdin):
            def isatty(self):
                return True
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = unlock.main(["--config", self.cfg], stdin=Tty(""), check_fs=False)
        self.assertEqual(code, 1)
        self.assertIn("pipe the decrypted vault", err.getvalue())

    def test_locked_message_for_run_paths(self):
        with self.assertRaises(CheckError) as cm:
            read_secret("/run/homelab-dash-secrets/does-not-exist.pass")
        self.assertIn("secrets are locked", str(cm.exception))


class TestPiholeSessionInRam(unittest.TestCase):
    def test_session_file_used_and_not_in_state(self):
        base = tempfile.mkdtemp()
        sec = fakesys.write_secrets(base)
        srv = fakesys.start_app("pihole6")
        sid_file = os.path.join(base, "secrets", "pihole.sid")
        cfg = config.Config(None)
        cfg.cp.read_dict({"pihole": {"url": fakesys.url_of(srv), "password_file": sec["pihole.pass"],
                                     "session_file": sid_file}})
        state = {"pihole": {"pihole_sid": "old-sid-on-disk"}}
        apps.pihole(cfg, state)
        self.assertNotIn("pihole", state)                  # nothing for state.json
        self.assertEqual(os.stat(sid_file).st_mode & 0o777, 0o600)
        apps.pihole(cfg, state)
        apps.pihole(cfg, state)
        self.assertEqual(srv.state["logins"], 1)           # session re-used from RAM file
        os.unlink(sid_file)                                # e.g. after --lock / reboot
        apps.pihole(cfg, state)
        self.assertEqual(srv.state["logins"], 2)
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(base)


if __name__ == "__main__":
    unittest.main()
