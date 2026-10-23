"""Offline environment validation and CLI checks; no authentication or cache access."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from diadoc_lib import auth, cli
from diadoc_lib.common import ClientError


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.env = {key: "private-value-" + str(i) for i, key in enumerate(auth.ENV_KEYS)}

    def check_cli(self, env, args, expected_code):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch.object(cli, "Client") as client, \
                patch.object(cli, "HttpTransport") as transport, \
                redirect_stdout(out), redirect_stderr(err):
            before = dict(os.environ)
            self.assertEqual(cli.main(args), expected_code)
            self.assertEqual(dict(os.environ), before)
            client.assert_not_called()
            transport.assert_not_called()
        self.assertEqual(err.getvalue(), "")
        for value in self.env.values():
            self.assertNotIn(value, out.getvalue())
        return json.loads(out.getvalue())

    def test_complete_environment_needs_no_files(self):
        with patch.object(Path, "read_text", side_effect=AssertionError("Unexpected file read")):
            self.assertEqual(self.check_cli(self.env, ["check-env"], 0), {"ok": True})
            with patch.dict(os.environ, self.env, clear=True):
                self.assertEqual(auth.validate_env(), {"ok": True})

    def test_missing_empty_and_whitespace_credentials(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(Path, "cwd", return_value=Path(tmp)):
            for key in auth.ENV_KEYS:
                for value in (None, "", "  "):
                    with self.subTest(key=key, value=value):
                        env = dict(self.env)
                        if value is None:
                            del env[key]
                        else:
                            env[key] = value
                        result = self.check_cli(env, ["check-env"], 2)
                        self.assertFalse(result["ok"])
                        self.assertEqual(result["error"], "configuration_error")
                        self.assertIn(key, result["message"])
                        with self.assertRaises(ClientError) as caught:
                            auth.validate_env(env)
                        self.assertEqual(caught.exception.code, "configuration_error")
            result = self.check_cli({}, ["check-env"], 2)
            for key in auth.ENV_KEYS:
                self.assertIn(key, result["message"])

    def test_files_and_environment_priority(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(Path, "cwd", return_value=Path(tmp)):
            default = Path(tmp) / ".env"
            default.write_text("\n".join(f"{key}={value}" for key, value in self.env.items()))
            explicit = Path(tmp) / "custom.env"
            explicit.write_text(default.read_text())
            for args in (["check-env"], ["--env-file", str(explicit), "check-env"]):
                self.assertEqual(self.check_cli({}, args, 0), {"ok": True})
                key = auth.ENV_KEYS[0]
                result = self.check_cli({key: ""}, args, 2)
                self.assertIn(key, result["message"])

    def test_invalid_file_is_reported_without_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "custom.env"
            args = ["--env-file", str(path), "check-env"]
            self.assertEqual(self.check_cli({}, args, 2)["error"], "configuration_error")
            for contents in ("private-value-0", auth.ENV_KEYS[0] + '= "private-value-0'):
                path.write_text(contents)
                self.assertEqual(self.check_cli({}, args, 2)["error"], "configuration_error")
            with self.assertRaises(ClientError) as caught:
                auth.validate_env({}, path)
            self.assertEqual(caught.exception.code, "configuration_error")
