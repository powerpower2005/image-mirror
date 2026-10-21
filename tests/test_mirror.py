"""외부 저장소에 쓰지 않고 미러링 정책과 비밀정보 출력을 검증합니다."""

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mirror", ROOT / "scripts/mirror.py")
mirror = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mirror)

D1 = "sha256:" + "a" * 64
D2 = "sha256:" + "b" * 64
STAMP = "2026-10-21T03:17:00Z"
IMAGE = {"id": "nginx", "source": "docker.io/library/nginx", "destination": "nginx",
         "enabled": True, "include": [r"\d+\.\d+\.\d+"], "exclude": []}
README = "안내\n" + mirror.START + "\n기존 내용\n" + mirror.END + "\n끝\n"


def entry():
    return {"source": IMAGE["source"], "destination": IMAGE["destination"],
            "initialized_at": STAMP, "last_checked_at": STAMP,
            "baseline_tags": ["1.0.0", "latest"], "mirrored_tags": {}, "skipped_tags": {}}


class FakeCrane:
    def __init__(self, tags=None, existing=None):
        self.listed = tags if tags is not None else ["1.0.0", "1.0.1", "latest"]
        self.existing = existing
        self.copies = []
        self.platform_refs = []
        self.source_error = None
        self.destination_error = None
        self.copy_error = None
        self.list_error = None
        self.after_copy = D1

    def tags(self, source):
        if self.list_error:
            raise self.list_error
        return sorted(set(self.listed))

    def digest(self, reference, allow_missing=False):
        if "docker.io/library/" in reference:
            if self.source_error:
                raise self.source_error
            return D1
        if self.destination_error:
            raise self.destination_error
        return self.existing

    def platforms(self, reference):
        self.platform_refs.append(reference)
        return ["linux/amd64", "linux/arm/v7", "linux/arm64/v8"]

    def copy(self, source, destination):
        self.copies.append((source, destination))
        if self.copy_error:
            raise self.copy_error
        self.existing = self.after_copy


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "images.yaml"
        self.state = self.root / "images.json"
        self.readme = self.root / "README.md"
        self.images = [copy.deepcopy(IMAGE)]
        self.write_inputs({})
        self.check = patch.object(mirror, "check_destination")
        self.ready = self.check.start()
        self.addCleanup(self.check.stop)
        self.output = io.StringIO()

    def write_inputs(self, state):
        self.config.write_text(yaml.safe_dump({"images": self.images}), encoding="utf-8")
        self.state.write_text(json.dumps(state), encoding="utf-8")
        self.readme.write_text(README, encoding="utf-8")

    def execute(self, crane):
        with contextlib.redirect_stdout(self.output), patch.object(mirror, "now", return_value="2026-10-22T03:17:00Z"):
            return mirror.run(self.config, self.state, self.readme, "private-test-namespace", crane)

    def saved(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def test_first_run_records_all_tags_without_copy(self):
        crane = FakeCrane()
        self.assertEqual(self.execute(crane), 0)
        self.assertEqual(self.saved()["nginx"]["baseline_tags"], crane.listed)
        self.assertFalse(crane.copies)
        self.ready.assert_not_called()
        self.assertIn("복사는 하지 않습니다", self.output.getvalue())

    def test_new_baseline_remembers_app_prefix(self):
        self.assertEqual(self.execute(FakeCrane()), 0)
        self.assertEqual(self.saved()["nginx"]["tag_prefix"], "nginx-")

    def test_two_apps_share_ovdr_with_distinct_prefixed_tags(self):
        self.images = [{**IMAGE, "destination": "ovdr"},
                       {**IMAGE, "id": "redis", "source": "docker.io/library/redis", "destination": "ovdr"}]
        self.write_inputs({})
        crane = FakeCrane(tags=["1.0.0"])
        self.assertEqual(self.execute(crane), 0)
        self.assertFalse(crane.copies)
        crane.listed = ["1.0.0", "1.0.1"]
        destinations = {}
        def digest(reference, allow_missing=False):
            if reference.startswith("docker.io/library/"):
                return D1
            return destinations.get(reference)
        def copied(source, destination):
            crane.copies.append((source, destination))
            destinations[destination] = D1
        crane.digest = digest
        crane.copy = copied
        self.assertEqual(self.execute(crane), 0)
        self.assertEqual([destination for _, destination in crane.copies], [
            "docker.io/private-test-namespace/ovdr:nginx-1.0.1",
            "docker.io/private-test-namespace/ovdr:redis-1.0.1"])
        for identifier in ("nginx", "redis"):
            self.assertIn("1.0.1", self.saved()[identifier]["mirrored_tags"])
            self.assertIn(f"ovdr:{identifier}-1.0.1", self.readme.read_text(encoding="utf-8"))

    def test_too_long_prefixed_tag_is_not_copied(self):
        self.images[0]["include"] = []
        previous = {**entry(), "tag_prefix": "nginx-"}
        self.write_inputs({"nginx": previous})
        crane = FakeCrane(tags=["1" * 128])
        self.assertEqual(self.execute(crane), 1)
        self.assertFalse(crane.copies)
        self.assertFalse(self.saved()["nginx"]["mirrored_tags"])

    def test_empty_watch_list_does_not_query_or_change_archived_state(self):
        self.images = []
        self.write_inputs({"nginx": entry()})
        before = self.state.read_bytes()
        crane = FakeCrane()
        crane.list_error = AssertionError("감시 대상이 없으면 조회하지 않습니다.")
        self.assertEqual(self.execute(crane), 0)
        self.assertEqual(self.state.read_bytes(), before)
        self.assertIn("현재 감시 대상이 없습니다", self.readme.read_text(encoding="utf-8"))

    def test_new_tag_is_pinned_and_verified(self):
        self.write_inputs({"nginx": entry()})
        crane = FakeCrane()
        self.assertEqual(self.execute(crane), 0)
        self.assertEqual(crane.copies, [(IMAGE["source"] + "@" + D1,
                                       "docker.io/private-test-namespace/nginx:1.0.1")])
        record = self.saved()["nginx"]["mirrored_tags"]["1.0.1"]
        self.assertTrue(record["verified"])
        self.assertEqual(record["digest"], D1)
        self.assertEqual(crane.platform_refs, [IMAGE["source"] + "@" + D1])

    def test_same_destination_digest_recovers_without_copy(self):
        self.write_inputs({"nginx": entry()})
        crane = FakeCrane(existing=D1)
        self.assertEqual(self.execute(crane), 0)
        self.assertFalse(crane.copies)
        self.ready.assert_not_called()
        self.assertIn("1.0.1", self.saved()["nginx"]["mirrored_tags"])

    def test_conflict_is_permanently_skipped_without_copy(self):
        self.write_inputs({"nginx": entry()})
        crane = FakeCrane(existing=D2)
        self.assertEqual(self.execute(crane), 0)
        self.assertFalse(crane.copies)
        self.assertEqual(self.saved()["nginx"]["skipped_tags"]["1.0.1"]["destination_digest"], D2)
        self.assertIn("::warning::", self.output.getvalue())
        crane.existing = None
        self.assertEqual(self.execute(crane), 0)
        self.assertFalse(crane.copies)

    def test_destination_errors_never_trigger_copy_or_terminal_record(self):
        for kind in ("auth", "rate", "server", "timeout", "unknown"):
            with self.subTest(kind=kind):
                self.write_inputs({"nginx": entry()})
                crane = FakeCrane()
                crane.destination_error = mirror.CraneError(kind)
                self.assertEqual(self.execute(crane), 1)
                self.assertFalse(crane.copies)
                self.assertFalse(self.saved()["nginx"]["mirrored_tags"])
                self.assertFalse(self.saved()["nginx"]["skipped_tags"])

    def test_failed_copy_is_retried_next_run(self):
        self.write_inputs({"nginx": entry()})
        crane = FakeCrane()
        crane.copy_error = mirror.CraneError("timeout")
        self.assertEqual(self.execute(crane), 1)
        self.assertFalse(self.saved()["nginx"]["mirrored_tags"])
        crane.copy_error = None
        self.assertEqual(self.execute(crane), 0)
        self.assertEqual(len(crane.copies), 2)

    def test_mismatch_after_successful_copy_is_failure_not_skip(self):
        self.write_inputs({"nginx": entry()})
        crane = FakeCrane()
        crane.after_copy = D2
        self.assertEqual(self.execute(crane), 1)
        self.assertFalse(self.saved()["nginx"]["mirrored_tags"])
        self.assertFalse(self.saved()["nginx"]["skipped_tags"])

    def test_failed_verification_does_not_record_success(self):
        self.write_inputs({"nginx": entry()})
        crane = FakeCrane()
        def copied(source, destination):
            crane.copies.append((source, destination))
            crane.destination_error = mirror.CraneError("rate")
        crane.copy = copied
        self.assertEqual(self.execute(crane), 1)
        self.assertFalse(self.saved()["nginx"]["mirrored_tags"])

    def test_racing_identical_push_recovers_after_copy_error(self):
        self.write_inputs({"nginx": entry()})
        crane = FakeCrane()
        def raced(source, destination):
            crane.existing = D1
            raise mirror.CraneError("unknown")
        crane.copy = raced
        self.assertEqual(self.execute(crane), 0)
        self.assertIn("1.0.1", self.saved()["nginx"]["mirrored_tags"])

    def test_racing_different_push_becomes_conflict(self):
        self.write_inputs({"nginx": entry()})
        crane = FakeCrane()
        def raced(source, destination):
            crane.existing = D2
            raise mirror.CraneError("unknown")
        crane.copy = raced
        self.assertEqual(self.execute(crane), 0)
        self.assertIn("1.0.1", self.saved()["nginx"]["skipped_tags"])

    def test_no_copy_when_destination_policy_is_unconfirmed(self):
        self.write_inputs({"nginx": entry()})
        self.ready.side_effect = mirror.MirrorError("태그 변경 불가 설정을 확인해 주세요.")
        crane = FakeCrane()
        self.assertEqual(self.execute(crane), 1)
        self.assertFalse(crane.copies)
        self.assertFalse(self.saved()["nginx"]["mirrored_tags"])

    def test_failed_initial_listing_does_not_initialize(self):
        crane = FakeCrane()
        crane.list_error = mirror.CraneError("auth")
        self.assertEqual(self.execute(crane), 1)
        self.assertEqual(self.saved(), {})

    def test_failed_listing_preserves_last_checked_time(self):
        self.write_inputs({"nginx": entry()})
        crane = FakeCrane()
        crane.list_error = mirror.CraneError("rate")
        self.assertEqual(self.execute(crane), 1)
        self.assertEqual(self.saved()["nginx"]["last_checked_at"], STAMP)

    def test_successful_listing_updates_time_even_with_tag_error(self):
        self.write_inputs({"nginx": entry()})
        crane = FakeCrane()
        crane.source_error = mirror.CraneError("auth")
        self.assertEqual(self.execute(crane), 1)
        self.assertEqual(self.saved()["nginx"]["last_checked_at"], "2026-10-22T03:17:00Z")

    def test_new_image_only_gets_baseline_and_old_image_copies(self):
        self.images.append({**IMAGE, "id": "redis", "destination": "redis", "source": "docker.io/library/redis"})
        self.write_inputs({"nginx": entry()})
        crane = FakeCrane()
        self.assertEqual(self.execute(crane), 0)
        self.assertEqual(len(crane.copies), 1)
        self.assertFalse(self.saved()["redis"]["mirrored_tags"])

    def test_one_failed_image_does_not_stop_the_next_image(self):
        self.images.append({**IMAGE, "id": "redis", "destination": "redis", "source": "docker.io/library/redis"})
        self.write_inputs({})
        crane = FakeCrane()
        def tags(source):
            if source.endswith("/nginx"):
                raise mirror.CraneError("rate")
            return ["1.0.0", "latest"]
        crane.tags = tags
        self.assertEqual(self.execute(crane), 1)
        self.assertNotIn("nginx", self.saved())
        self.assertIn("redis", self.saved())
        self.assertFalse(crane.copies)

    def test_widening_filter_does_not_copy_baseline(self):
        self.images[0]["include"] = []
        previous = entry()
        previous["baseline_tags"] += ["1.0.1", "alpine"]
        self.write_inputs({"nginx": previous})
        crane = FakeCrane(tags=["latest", "alpine", "1.0.1"])
        self.assertEqual(self.execute(crane), 0)
        self.assertFalse(crane.copies)

    def test_disabled_image_is_not_queried(self):
        self.images[0]["enabled"] = False
        self.write_inputs({"nginx": entry()})
        crane = FakeCrane()
        crane.list_error = AssertionError("조회하면 안 됩니다.")
        self.assertEqual(self.execute(crane), 0)
        self.assertEqual(self.saved()["nginx"]["last_checked_at"], STAMP)

    def test_already_mirrored_tag_is_not_refreshed(self):
        previous = entry()
        previous["mirrored_tags"]["1.0.1"] = {"digest": D2, "verified": True,
            "platforms": ["linux/amd64"], "mirrored_at": STAMP}
        self.write_inputs({"nginx": previous})
        crane = FakeCrane()
        self.assertEqual(self.execute(crane), 0)
        self.assertFalse(crane.copies)
        self.assertEqual(self.saved()["nginx"]["mirrored_tags"]["1.0.1"]["digest"], D2)

    def test_source_and_destination_changes_are_rejected(self):
        for field, value in (("source", "docker.io/library/redis"), ("destination", "other")):
            with self.subTest(field=field):
                self.images = [{**IMAGE, field: value}]
                self.write_inputs({"nginx": entry()})
                with self.assertRaises(mirror.MirrorError):
                    self.execute(FakeCrane())

    def test_invalid_configurations_are_rejected(self):
        variants = [
            [IMAGE, IMAGE],
            [{**IMAGE, "include": ["["]}],
            [{**IMAGE, "enabled": "true"}],
            [{**IMAGE, "source": "https://user:password@registry/image"}],
            [{**IMAGE, "destination": "namespace/nginx"}],
        ]
        for images in variants:
            with self.subTest(images=images):
                self.images = images
                self.write_inputs({})
                with self.assertRaises(mirror.MirrorError):
                    self.execute(FakeCrane())

    def test_corrupt_missing_and_duplicate_state_are_rejected(self):
        for text in ("{", "[]", '{"nginx":{},"nginx":{}}', '{"nginx": {}}'):
            self.state.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(mirror.MirrorError):
                self.execute(FakeCrane())
            self.assertEqual(self.state.read_text(encoding="utf-8"), text)
        self.state.unlink()
        with self.assertRaises(mirror.MirrorError):
            self.execute(FakeCrane())

    def test_readme_markers_are_checked_before_external_work(self):
        self.readme.write_text("표시 영역 없음", encoding="utf-8")
        crane = FakeCrane()
        crane.list_error = AssertionError("외부 작업을 시작하면 안 됩니다.")
        with self.assertRaises(mirror.MirrorError):
            self.execute(crane)

    def test_outputs_and_files_do_not_contain_destination_namespace(self):
        self.write_inputs({"nginx": entry()})
        self.assertEqual(self.execute(FakeCrane()), 0)
        for text in (self.output.getvalue(), self.state.read_text(encoding="utf-8"), self.readme.read_text(encoding="utf-8")):
            self.assertNotIn("private-test-namespace", text)
        self.assertIn("✅ 검증됨", self.readme.read_text(encoding="utf-8"))
        self.assertNotIn(D1, self.readme.read_text(encoding="utf-8"))

    def test_state_is_saved_before_next_tag_starts(self):
        self.write_inputs({"nginx": entry()})
        crane = FakeCrane(tags=["1.0.1", "1.0.2"])
        original = crane.digest
        def digest(reference, allow_missing=False):
            if reference == IMAGE["source"] + ":1.0.2":
                self.assertIn("1.0.1", self.saved()["nginx"]["mirrored_tags"])
                raise mirror.CraneError("rate")
            return original(reference, allow_missing)
        crane.digest = digest
        self.assertEqual(self.execute(crane), 1)
        self.assertIn("1.0.1", self.saved()["nginx"]["mirrored_tags"])


class ToolTests(unittest.TestCase):
    def test_filters_use_full_match_and_exclusion_wins(self):
        image = {**IMAGE, "exclude": [r"1\.0\.1"]}
        self.assertEqual(mirror.candidates(image, entry(), ["1.0.1", "1.0.2", "v1.0.3", "1.0.4-extra"]), ["1.0.2"])

    def test_empty_include_allows_all_nonexcluded_tags(self):
        self.assertEqual(mirror.candidates({**IMAGE, "include": []}, entry(), ["latest", "alpine"]), ["alpine"])

    def test_errors_are_classified_without_exposing_raw_output(self):
        examples = {"MANIFEST_UNKNOWN: missing": "missing", "unexpected status code 404 Not Found": "missing",
            "unexpected status code 401 Unauthorized": "auth", "DENIED: request denied": "auth",
            "unexpected status code 403 Forbidden": "auth", "429 Too Many Requests": "rate",
            "unexpected status code 503 Service Unavailable": "server", "executable not found": "unknown",
            "HEAD: MANIFEST_UNKNOWN; GET: unexpected status code 401": "auth",
            "HEAD: unexpected status code 404\nError: dial tcp: timeout": "unknown"}
        for raw, expected in examples.items():
            with self.subTest(raw=raw):
                self.assertEqual(mirror.classify_error(raw), expected)

    def test_only_explicit_missing_returns_none(self):
        crane = mirror.Crane()
        for kind in ("auth", "rate", "unknown"):
            with patch.object(crane, "run", side_effect=mirror.CraneError(kind)):
                with self.assertRaises(mirror.CraneError):
                    crane.digest("docker.io/example/image:1", allow_missing=True)
        with patch.object(crane, "run", side_effect=mirror.CraneError("missing")):
            self.assertIsNone(crane.digest("docker.io/example/image:1", allow_missing=True))

    def test_crane_captures_output_and_uses_timeout(self):
        raw = "secret-namespace token-secret https://private.invalid"
        result = subprocess.CompletedProcess([], 1, "", "DENIED " + raw)
        with patch.object(mirror.subprocess, "run", return_value=result) as command:
            with self.assertRaises(mirror.CraneError) as error:
                mirror.Crane().run("digest", "secret-namespace/image:1")
        self.assertNotIn(raw, str(error.exception))
        self.assertTrue(command.call_args.kwargs["capture_output"])
        self.assertGreater(command.call_args.kwargs["timeout"], 0)

    def test_timeout_is_safe(self):
        with patch.object(mirror.subprocess, "run", side_effect=subprocess.TimeoutExpired("secret-token", 1)):
            with self.assertRaises(mirror.CraneError) as error:
                mirror.Crane().run("ls", "source")
        self.assertNotIn("secret-token", str(error.exception))

    def test_platforms_keep_variants_and_hide_attestation_only_in_display(self):
        manifest = {"manifests": [{"platform": {"os": "linux", "architecture": "amd64"}},
            {"platform": {"os": "linux", "architecture": "arm", "variant": "v7"}},
            {"platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
            {"platform": {"os": "unknown", "architecture": "unknown"}}]}
        crane = mirror.Crane()
        with patch.object(crane, "document", return_value=manifest):
            self.assertEqual(crane.platforms("source@" + D1), ["linux/amd64", "linux/arm/v7", "linux/arm64/v8"])
        with patch.object(crane, "run", return_value="") as command:
            crane.copy("source@" + D1, "target:tag")
        self.assertEqual(command.call_args.args, ("copy", "--no-clobber", "source@" + D1, "target:tag"))

    def test_single_platform_uses_config_and_variant(self):
        crane = mirror.Crane()
        with patch.object(crane, "document", side_effect=[{"schemaVersion": 2},
            {"os": "linux", "architecture": "arm", "variant": "v7"}]):
            self.assertEqual(crane.platforms("source@" + D1), ["linux/arm/v7"])

    def test_nested_index_is_traversed(self):
        crane = mirror.Crane()
        with patch.object(crane, "document", side_effect=[{"manifests": [{"digest": D2}]},
            {"manifests": [{"platform": {"os": "linux", "architecture": "amd64"}}]}]):
            self.assertEqual(crane.platforms("source@" + D1), ["linux/amd64"])

    def test_emit_redacts_secrets_and_escapes_annotations(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"DOCKERHUB_NAMESPACE": "private-name", "DOCKERHUB_TOKEN": "private-token"}), contextlib.redirect_stdout(output):
            mirror.emit("시험", "private-name private-token\n::error::주입", "warning")
        self.assertNotIn("private-name", output.getvalue())
        self.assertNotIn("private-token", output.getvalue())
        self.assertIn("%0A", output.getvalue())
        self.assertEqual(output.getvalue().count("\n"), 1)

    def test_markdown_content_cannot_break_table_or_insert_html(self):
        self.assertEqual(mirror.markdown("<script>|`\n"), "&lt;script&gt;&#124;&#96; ")

    def test_atomic_write_does_not_damage_original_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("원본", encoding="utf-8")
            with patch.object(mirror.os, "replace", side_effect=OSError("쓰기 실패")):
                with self.assertRaises(OSError):
                    mirror.atomic_write(path, "새 내용")
            self.assertEqual(path.read_text(encoding="utf-8"), "원본")
            self.assertEqual(list(Path(directory).iterdir()), [path])


class DestinationPolicyTests(unittest.TestCase):
    def response(self, payload):
        return contextlib.closing(io.StringIO(json.dumps(payload)))

    def test_public_immutable_destination_is_accepted(self):
        for rule in (".*", "^.*$"):
            with self.subTest(rule=rule), patch.object(mirror, "urlopen", return_value=self.response({
                "is_private": False, "immutable_tags_settings": {"enabled": True, "rules": [rule]}})):
                mirror.check_destination("private-name", "nginx")

    def test_mutable_private_partial_and_unknown_policies_are_rejected(self):
        payloads = [{}, {"is_private": True}, {"is_private": False},
            {"is_private": False, "immutable_tags_settings": {"enabled": False, "rules": [".*"]}},
            {"is_private": False, "immutable_tags_settings": {"enabled": True, "rules": ["v.*"]}},
            {"is_private": False, "immutable_tags_settings": {"enabled": True, "rules": []}}]
        for payload in payloads:
            with self.subTest(payload=payload), patch.object(mirror, "urlopen", return_value=self.response(payload)):
                with self.assertRaises(mirror.MirrorError):
                    mirror.check_destination("private-name", "nginx")

    def test_http_errors_do_not_leak_namespace(self):
        for code in (401, 403, 404, 429, 500):
            error = HTTPError("https://hub.docker.com/private-name", code, "private-name", {}, None)
            with self.subTest(code=code), patch.object(mirror, "urlopen", side_effect=error):
                with self.assertRaises(mirror.MirrorError) as caught:
                    mirror.check_destination("private-name", "nginx")
                self.assertNotIn("private-name", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
