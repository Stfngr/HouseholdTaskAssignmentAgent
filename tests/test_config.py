"""Configuration tests use only the standard library and fake credentials."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import Mock, patch

from household_agent.config import (
    WEEKDAYS, Config, ConfigError, DashboardConfig, Resident, Settings, Task,
    config_snapshot, load_config, load_credentials, load_dashboard_config,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "config").mkdir()
        self.settings = {
            "residents": [
                {"id": "alice", "name": "Alice", "role": "adult", "off_days": []}
            ]
        }
        self.tasks = {
            "tasks": [
                {
                    "id": "dishes", "name": "Dishes",
                    "allowed_roles": ["child", "adult"], "frequency": "weekly",
                }
            ]
        }

    def write_config(self):
        for filename, data in (("Settings.json", self.settings), ("tasks.json", self.tasks)):
            (self.root / "config" / filename).write_text(json.dumps(data), encoding="utf-8")

    def load(self):
        self.write_config()
        return load_config(self.root)

    def test_defaults_and_models(self):
        config = self.load()
        self.assertEqual(config, Config(
            Settings("06:00", "Europe/Berlin", 1, (Resident("alice", "Alice", "adult", ()),)),
            (Task("dishes", "Dishes", ("child", "adult"), "weekly"),),
        ))
        self.assertIsInstance(config.settings.residents, tuple)
        self.assertIsInstance(config.tasks[0].allowed_roles, tuple)
        self.assertIsInstance(config.settings.residents[0].off_days, tuple)
        for instance, field in (
            (config, "tasks"), (config.settings, "timezone"),
            (config.settings.residents[0], "name"), (config.tasks[0], "name"),
        ):
            with self.subTest(model=type(instance).__name__):
                with self.assertRaises(FrozenInstanceError):
                    setattr(instance, field, None)

    def test_custom_settings(self):
        self.settings.update(daily_execution_time="23:59", timezone="UTC", tasks_per_active_day=3)
        config = self.load()
        self.assertEqual(config.settings.daily_execution_time, "23:59")
        self.assertEqual(config.settings.timezone, "UTC")
        self.assertEqual(config.settings.tasks_per_active_day, 3)
        self.settings["daily_execution_time"] = "00:00"
        self.assertEqual(self.load().settings.daily_execution_time, "00:00")

    def test_empty_pool_and_all_days_off(self):
        self.tasks["tasks"] = []
        self.settings["residents"][0]["off_days"] = list(WEEKDAYS)
        config = self.load()
        self.assertEqual(config.tasks, ())
        self.assertEqual(config.settings.residents[0].off_days, WEEKDAYS)

    def test_invalid_settings_values(self):
        invalid = {
            "daily_execution_time": [None, True, 600, "", "6:00", "24:00", "12:60", "06:00:00", " 06:00", "06:00\n", "\uff10\uff16:00"],
            "timezone": [None, 1, "", " ", "Not/A_Zone", "/etc/passwd", "../UTC"],
            "tasks_per_active_day": [None, True, False, 0, -1, 1.0, "1", []],
            "residents": [None, True, {}, "alice", []],
        }
        original = copy.deepcopy(self.settings)
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.settings = copy.deepcopy(original)
                    self.settings[field] = value
                    with self.assertRaises(ConfigError):
                        self.load()

    def test_unknown_fields_at_every_level(self):
        for location in ("settings", "resident", "tasks", "task"):
            with self.subTest(location=location):
                target = {
                    "settings": self.settings, "resident": self.settings["residents"][0],
                    "tasks": self.tasks, "task": self.tasks["tasks"][0],
                }[location]
                target["typo"] = 1
                with self.assertRaisesRegex(ConfigError, "unknown fields.*typo"):
                    self.load()
                del target["typo"]

    def test_required_fields_at_every_level(self):
        for target, fields in (
            (self.settings, ["residents"]),
            (self.settings["residents"][0], ["id", "name", "role", "off_days"]),
            (self.tasks, ["tasks"]),
            (self.tasks["tasks"][0], ["id", "name", "allowed_roles", "frequency"]),
        ):
            for field in fields:
                with self.subTest(field=field):
                    value = target.pop(field)
                    with self.assertRaisesRegex(ConfigError, "missing fields"):
                        self.load()
                    target[field] = value

    def test_nonempty_string_ids_and_names(self):
        for target in (self.settings["residents"][0], self.tasks["tasks"][0]):
            for field in ("id", "name"):
                original = target[field]
                for value in (None, True, 1, [], {}, "", " \t\n"):
                    with self.subTest(field=field, value=value):
                        target[field] = value
                        with self.assertRaises(ConfigError):
                            self.load()
                target[field] = original

    def test_invalid_resident_fields(self):
        resident = self.settings["residents"][0]
        for field, values in (
            ("role", [None, [], True, "Adult", "parent", ""]),
            ("off_days", [None, "Monday", {}, ["monday"], ["Montag"], ["Monday", "Monday"], [1], [[]]]),
        ):
            original = resident[field]
            for value in values:
                with self.subTest(field=field, value=value):
                    resident[field] = value
                    with self.assertRaises(ConfigError):
                        self.load()
            resident[field] = original

    def test_invalid_task_fields(self):
        task = self.tasks["tasks"][0]
        for field, values in (
            ("allowed_roles", [None, "adult", {}, [], ["child"], ["adult", "adult"], ["adult", "parent"], [True], [[]]]),
            ("frequency", [None, True, [], "daily", "Weekly", ""]),
        ):
            original = task[field]
            for value in values:
                with self.subTest(field=field, value=value):
                    task[field] = value
                    with self.assertRaises(ConfigError):
                        self.load()
            task[field] = original

    def test_valid_roles_and_frequencies(self):
        for roles in (["adult"], ["adult", "child"], ["child", "adult"]):
            for frequency in ("weekly", "monthly"):
                for role in ("adult", "child"):
                    with self.subTest(roles=roles, frequency=frequency, role=role):
                        self.settings["residents"][0]["role"] = role
                        self.tasks["tasks"][0].update(allowed_roles=roles, frequency=frequency)
                        config = self.load()
                        self.assertEqual(config.tasks[0].allowed_roles, tuple(roles))

    def test_duplicate_ids(self):
        for entries in (self.settings["residents"], self.tasks["tasks"]):
            entries.append(copy.deepcopy(entries[0]))
            with self.assertRaisesRegex(ConfigError, "duplicate .* ID"):
                self.load()
            entries.pop()

    def test_id_namespaces_are_independent(self):
        self.tasks["tasks"][0]["id"] = "alice"
        self.assertEqual(self.load().tasks[0].id, "alice")

    def test_wrong_object_and_list_types(self):
        for filename in ("Settings.json", "tasks.json"):
            for value in ([], None, "object", 1, True):
                with self.subTest(filename=filename, value=value):
                    self.write_config()
                    (self.root / "config" / filename).write_text(json.dumps(value), encoding="utf-8")
                    with self.assertRaises(ConfigError):
                        load_config(self.root)
        for entries in (self.settings["residents"], self.tasks["tasks"]):
            original = entries[0]
            for value in ([], None, "object", 1, True):
                entries[0] = value
                with self.assertRaises(ConfigError):
                    self.load()
            entries[0] = original
        for value in (None, {}, "tasks", True):
            self.tasks["tasks"] = value
            with self.assertRaises(ConfigError):
                self.load()

    def test_duplicate_json_keys(self):
        cases = (
            ("Settings.json", '{"residents": [], "residents": []}'),
            ("Settings.json", '{"residents": [{"id": "x", "id": "y"}]}'),
            ("tasks.json", '{"tasks": [], "tasks": []}'),
            ("tasks.json", '{"tasks": [{"name": "x", "name": "y"}]}'),
        )
        for filename, content in cases:
            with self.subTest(filename=filename, content=content):
                self.write_config()
                (self.root / "config" / filename).write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, "Duplicate JSON key"):
                    load_config(self.root)

    def test_invalid_json_and_encoding(self):
        for filename in ("Settings.json", "tasks.json"):
            for content in (b"{", b"\xff", b'{"tasks": NaN}', b'{"tasks": Infinity}', b'{"tasks": -Infinity}'):
                with self.subTest(filename=filename, content=content):
                    self.write_config()
                    (self.root / "config" / filename).write_bytes(content)
                    with self.assertRaisesRegex(ConfigError, filename):
                        load_config(self.root)

    def test_missing_and_unreadable_files(self):
        for filename in ("Settings.json", "tasks.json"):
            with self.subTest(filename=filename):
                self.write_config()
                (self.root / "config" / filename).unlink()
                with self.assertRaisesRegex(ConfigError, filename):
                    load_config(self.root)
        self.write_config()
        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(ConfigError, "Cannot read"):
                load_config(self.root)

    def test_root_is_independent_of_working_directory(self):
        self.write_config()
        script = "from pathlib import Path; from household_agent.config import load_config; load_config(Path(__import__('sys').argv[1]))"
        environment = dict(os.environ, PYTHONPATH=str(PROJECT_ROOT))
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.root)], cwd=self.root,
            env=environment, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_snapshot_is_canonical_and_json_serializable(self):
        self.settings["residents"][0]["off_days"] = ["Sunday", "Monday"]
        self.settings["residents"].append({"id": "bob", "name": "Bob", "role": "child", "off_days": []})
        self.tasks["tasks"].append(dict(self.tasks["tasks"][0], id="vacuum"))
        config = self.load()
        snapshot = config_snapshot(config)
        self.settings["residents"].reverse()
        self.settings["residents"][1]["off_days"].reverse()
        self.tasks["tasks"].reverse()
        for task in self.tasks["tasks"]:
            task["allowed_roles"].reverse()
        self.settings.update(daily_execution_time="06:00", timezone="Europe/Berlin", tasks_per_active_day=1)
        reordered = config_snapshot(self.load())
        self.assertEqual(json.dumps(snapshot), json.dumps(reordered))
        self.assertEqual(json.loads(json.dumps(snapshot)), snapshot)
        self.assertEqual(snapshot["settings"]["residents"][0]["off_days"], ["Monday", "Sunday"])
        snapshot["settings"]["residents"][0]["off_days"].clear()
        self.assertEqual(config.settings.residents[0].off_days, ("Sunday", "Monday"))

    def test_snapshot_captures_all_semantic_changes(self):
        config = self.load()
        baseline = config_snapshot(config)
        resident = config.settings.residents[0]
        task = config.tasks[0]
        changes = [
            replace(config, settings=replace(config.settings, **{field: value}))
            for field, value in (("daily_execution_time", "07:00"), ("timezone", "UTC"), ("tasks_per_active_day", 2))
        ]
        changes.extend(
            replace(config, settings=replace(config.settings, residents=(replace(resident, **{field: value}),)))
            for field, value in (("id", "bob"), ("name", "New name"), ("role", "child"), ("off_days", ("Monday",)))
        )
        changes.extend(
            replace(config, tasks=(replace(task, **{field: value}),))
            for field, value in (("id", "other"), ("name", "New name"), ("allowed_roles", ("adult",)), ("frequency", "monthly"))
        )
        for changed in changes:
            with self.subTest(config=changed):
                self.assertNotEqual(config_snapshot(changed), baseline)

    def test_project_samples(self):
        config = load_config(PROJECT_ROOT)
        self.assertEqual([resident.id for resident in config.settings.residents], ["alice", "bob", "charlie"])
        self.assertEqual(len(config.tasks), 4)
        self.assertEqual(config.tasks[0].name, "Sp\u00fclmaschine ausr\u00e4umen")

    def test_import_and_json_loading_without_external_dependencies(self):
        self.write_config()
        script = (
            "import sys; sys.modules['dotenv'] = None; sys.modules['telegram'] = None; "
            "from pathlib import Path; from household_agent.config import load_config; "
            "load_config(Path(sys.argv[1]))"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-S", "-c", script, str(self.root)], cwd=PROJECT_ROOT,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class CredentialsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("/unused/project")
        self.token = "123456789:synthetic_test_token-not-a-real-secret"
        self.env = {"TELEGRAM_BOT_TOKEN": self.token, "TELEGRAM_CHAT_ID": "-1001234567890"}
        self.loader = Mock()
        module = types.ModuleType("dotenv")
        module.load_dotenv = self.loader
        patcher = patch.dict(sys.modules, {"dotenv": module})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_lazy_dotenv_load_and_negative_chat_id(self):
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(load_credentials(self.root), (self.token, "-1001234567890"))
        self.loader.assert_called_once_with(dotenv_path=self.root / ".env", override=False)

    def test_positive_chat_id(self):
        self.env["TELEGRAM_CHAT_ID"] = "12345"
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(load_credentials(self.root)[1], "12345")

    def test_environment_precedence_and_dotenv_fallback(self):
        def fake_dotenv(*, dotenv_path, override):
            self.assertEqual(dotenv_path, self.root / ".env")
            self.assertFalse(override)
            os.environ.setdefault("TELEGRAM_BOT_TOKEN", self.token)
            os.environ.setdefault("TELEGRAM_CHAT_ID", "-123")
        self.loader.side_effect = fake_dotenv
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "456"}, clear=True):
            self.assertEqual(load_credentials(self.root), (self.token, "456"))

    def test_missing_credentials(self):
        for field in self.env:
            environment = dict(self.env)
            del environment[field]
            with self.subTest(field=field), patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(ConfigError, field):
                    load_credentials(self.root)

    def test_invalid_credentials_and_redaction(self):
        invalid = {
            "TELEGRAM_BOT_TOKEN": ["", " ", "replace_with_bot_token", "invalid-secret", "0:abc", "123:", "123:abc def", "123:abc\n"],
            "TELEGRAM_CHAT_ID": ["", " ", "replace_with_chat_id", "0", "-0", "1.2", "abc", "123\n"],
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    environment = dict(self.env, **{field: value})
                    with patch.dict(os.environ, environment, clear=True):
                        with self.assertRaisesRegex(ConfigError, field) as caught:
                            load_credentials(self.root)
                    self.assertNotIn(self.token, str(caught.exception))
                    if len(value) > 3 and value.strip():
                        self.assertNotIn(value, str(caught.exception))

    def test_dotenv_errors_are_redacted(self):
        self.loader.side_effect = OSError(self.token)
        with self.assertRaises(ConfigError) as caught:
            load_credentials(self.root)
        self.assertNotIn(self.token, str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_missing_dependency_is_config_error(self):
        with patch.dict(sys.modules, {"dotenv": None}):
            with self.assertRaisesRegex(ConfigError, "python-dotenv"):
                load_credentials(self.root)

    def test_dashboard_config_is_optional_and_normalizes_origin(self):
        dashboard_token = "d" * 32
        environment = dict(self.env, DASHBOARD_URL="https://dashboard.example:8443/", DASHBOARD_TOKEN=dashboard_token)
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(load_dashboard_config(self.root), DashboardConfig("https://dashboard.example:8443", dashboard_token))
        with patch.dict(os.environ, self.env, clear=True):
            self.assertIsNone(load_dashboard_config(self.root))

    def test_dashboard_config_requires_pair_and_redacts_invalid_values(self):
        cases = (
            {"DASHBOARD_URL": "https://dashboard.example"},
            {"DASHBOARD_TOKEN": "secret-token"},
            {"DASHBOARD_URL": "https://user:secret@dashboard.example", "DASHBOARD_TOKEN": "secret-token"},
            {"DASHBOARD_URL": "https://dashboard.example/path", "DASHBOARD_TOKEN": "secret-token"},
            {"DASHBOARD_URL": "ftp://dashboard.example", "DASHBOARD_TOKEN": "secret-token"},
            {"DASHBOARD_URL": "https://:443", "DASHBOARD_TOKEN": "secret-token"},
            {"DASHBOARD_URL": "https://dashboard.example", "DASHBOARD_TOKEN": "short"},
            {"DASHBOARD_URL": "https://dashboard.example", "DASHBOARD_TOKEN": " \t"},
        )
        for dashboard in cases:
            with self.subTest(dashboard=dashboard), patch.dict(os.environ, dict(self.env, **dashboard), clear=True):
                with self.assertRaises(ConfigError) as caught:
                    load_dashboard_config(self.root)
                self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
