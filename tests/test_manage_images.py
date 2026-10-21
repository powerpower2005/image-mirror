"""감시 설정 변경이 기준선·완료 기록·이미지를 건드리지 않는지 확인합니다."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import manage_images as manage
from mirror import MirrorError, START, END, render_status


IMAGE = {"id": "nginx", "source": "docker.io/library/nginx", "destination": "nginx",
         "enabled": True, "include": [r"\d+\.\d+\.\d+"], "exclude": ["latest"]}
STAMP = "2026-10-21T03:17:00Z"
STATE = {"nginx": {"source": IMAGE["source"], "destination": IMAGE["destination"],
    "initialized_at": STAMP, "last_checked_at": STAMP, "baseline_tags": ["1.0.0"],
    "mirrored_tags": {"1.0.1": {"digest": "sha256:" + "a" * 64, "verified": True,
                              "platforms": ["linux/amd64"], "mirrored_at": STAMP}},
    "skipped_tags": {}}}


class ChangeImagesTests(unittest.TestCase):
    def setUp(self):
        self.images = [copy.deepcopy(IMAGE)]
        self.state = copy.deepcopy(STATE)

    def change(self, operation, identifier="nginx", **kwargs):
        return manage.change_images(self.images, self.state, operation, identifier, **kwargs)

    def test_add_uses_default_filters_and_defers_baseline(self):
        updated = self.change("추가", "redis", source="docker.io/library/redis", destination="redis")
        self.assertEqual(updated[1]["include"], manage.DEFAULT_INCLUDE)
        self.assertTrue(updated[1]["enabled"])
        self.assertEqual(updated[1]["exclude"], [])
        self.assertNotIn("redis", self.state)
        self.assertEqual(self.images, [IMAGE])

    def test_add_can_start_disabled_and_allow_all_tags(self):
        updated = self.change("추가", "redis", source="docker.io/library/redis", destination="redis",
                              enabled="중지", include="[]")
        self.assertFalse(updated[1]["enabled"])
        self.assertEqual(updated[1]["include"], [])

    def test_add_defaults_to_ovdr_and_allows_multiple_apps(self):
        self.images = [{**IMAGE, "destination": "ovdr"}]
        self.state = {}
        updated = self.change("추가", "redis", source="docker.io/library/redis")
        self.assertEqual([image["destination"] for image in updated], ["ovdr", "ovdr"])

    def test_update_preserves_omitted_fields(self):
        updated = self.change("수정", enabled="중지")
        self.assertEqual(updated[0], {**IMAGE, "enabled": False})
        self.assertEqual(self.state, STATE)

    def test_update_and_clear_filters(self):
        updated = self.change("수정", include='[".*-alpine"]', exclude="[]")
        self.assertEqual(updated[0]["include"], [".*-alpine"])
        self.assertEqual(updated[0]["exclude"], [])

    def test_update_can_resume_monitoring(self):
        self.images[0]["enabled"] = False
        self.assertTrue(self.change("수정", enabled="사용")[0]["enabled"])

    def test_delete_preserves_all_state(self):
        self.assertEqual(self.change("삭제"), [])
        self.assertEqual(self.state, STATE)
        self.assertEqual(self.images, [IMAGE])

    def test_readding_deleted_id_resumes_existing_baseline(self):
        self.images = []
        self.assertEqual(len(self.change("추가", source=IMAGE["source"], destination=IMAGE["destination"])), 1)
        self.assertEqual(self.state, STATE)

    def test_reusing_historical_id_for_different_source_is_rejected(self):
        self.images = []
        with self.assertRaises(MirrorError):
            self.change("추가", source="docker.io/library/redis", destination="redis")

    def test_source_and_destination_changes_require_new_id(self):
        for kwargs in ({"source": "docker.io/library/redis"}, {"destination": "other"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MirrorError):
                self.change("수정", **kwargs)

    def test_duplicate_add_and_unknown_update_or_delete_are_rejected(self):
        for operation, identifier in (("추가", "nginx"), ("수정", "missing"), ("삭제", "missing")):
            with self.subTest(operation=operation), self.assertRaises(MirrorError):
                self.change(operation, identifier)

    def test_invalid_destination_and_credentials_are_rejected(self):
        for kwargs in ({"source": "docker.io/library/redis", "destination": "namespace/ovdr"},
                       {"source": "https://user:token@registry/image", "destination": "redis"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MirrorError):
                self.change("추가", "redis", **kwargs)

    def test_invalid_filters_fail_without_mutation(self):
        for value in ('["["]', '".*"', '[1]', '[bad]', '["\\d"]'):
            with self.subTest(value=value), self.assertRaises(MirrorError):
                self.change("수정", include=value)
            self.assertEqual(self.images, [IMAGE])
            self.assertEqual(self.state, STATE)

    def test_delete_rejects_unrelated_change_inputs(self):
        for kwargs in ({"source": IMAGE["source"]}, {"enabled": "중지"}, {"include": "[]"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MirrorError):
                self.change("삭제", **kwargs)

    def test_json_backslashes_are_preserved(self):
        pattern = r"\d+\.\d+\.\d+"
        updated = self.change("수정", include=json.dumps([pattern]))
        self.assertEqual(updated[0]["include"], [pattern])


class ManagementFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.config = root / "images.yaml"
        self.state = root / "images.json"
        self.readme = root / "README.md"
        self.config.write_text(yaml.safe_dump({"images": [IMAGE]}, sort_keys=False), encoding="utf-8")
        self.state.write_text(json.dumps(STATE, indent=2), encoding="utf-8")
        self.readme.write_text(render_status("안내\n" + START + "\n" + END, [IMAGE], STATE), encoding="utf-8")

    def files(self):
        return tuple(p.read_bytes() for p in (self.config, self.state, self.readme))

    def execute(self, operation, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            manage.run(self.config, self.state, self.readme, operation=operation, identifier="nginx", **kwargs)

    def test_preview_shows_diff_without_writing_any_file(self):
        before = self.files()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            manage.run(self.config, self.state, self.readme, operation="삭제", identifier="nginx", preview=True)
        self.assertEqual(self.files(), before)
        self.assertIn("변경 전", output.getvalue())
        self.assertIn("미리보기", output.getvalue())

    def test_delete_updates_configuration_and_readme_but_retains_history(self):
        state_before = self.state.read_bytes()
        self.execute("삭제")
        self.assertEqual(yaml.safe_load(self.config.read_text(encoding="utf-8")), {"images": []})
        self.assertEqual(self.state.read_bytes(), state_before)
        readme = self.readme.read_text(encoding="utf-8")
        self.assertNotIn("| nginx |", readme)
        self.assertIn("1.0.1", readme)
        self.assertIn("✅ 검증됨", readme)

    def test_noop_update_does_not_rewrite_files_or_comments(self):
        self.config.write_text("# 기존 주석\n" + self.config.read_text(encoding="utf-8"), encoding="utf-8")
        before = self.files()
        self.execute("수정")
        self.assertEqual(self.files(), before)

    def test_invalid_update_does_not_change_any_file(self):
        before = self.files()
        with self.assertRaises(MirrorError):
            self.execute("수정", include='["["]')
        self.assertEqual(self.files(), before)

    def test_corrupt_readme_prevents_configuration_changes(self):
        self.readme.write_text("표시 영역 없음", encoding="utf-8")
        before = self.files()
        with self.assertRaises(MirrorError):
            self.execute("삭제")
        self.assertEqual(self.files(), before)

    def test_cli_receives_environment_inputs_without_shell_interpolation(self):
        values = {"INPUT_OPERATION": "수정", "INPUT_ID": "nginx", "INPUT_ENABLED": "중지",
                  "INPUT_INCLUDE": json.dumps([r"\d+\.\d+\.\d+"]), "INPUT_PREVIEW": "true"}
        with (patch.dict(os.environ, values), patch.object(manage, "run") as run,
              patch.object(sys, "argv", ["manage_images.py"])):
            self.assertEqual(manage.main(), 0)
        self.assertEqual(run.call_args.kwargs["enabled"], "중지")
        self.assertTrue(run.call_args.kwargs["preview"])


if __name__ == "__main__":
    unittest.main()
