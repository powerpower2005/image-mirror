"""새로 관측한 공개 이미지 태그를 다이제스트 그대로 미러링합니다."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml


ROOT = Path(__file__).resolve().parents[1]
START = "<!-- MIRROR-STATUS:START -->"
END = "<!-- MIRROR-STATUS:END -->"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
REPOSITORY = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")
COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
SOURCE = re.compile(r"[a-z0-9][a-z0-9.-]*(?::[0-9]+)?/" + COMPONENT + r"(?:/" + COMPONENT + r")*")
PLATFORM = re.compile(r"[a-z0-9_.-]+/[a-z0-9_.-]+(?:/[a-z0-9_.-]+)?")


class MirrorError(Exception):
    """외부 명령 원문을 포함하지 않는 공개 가능한 오류입니다."""


class CraneError(MirrorError):
    def __init__(self, kind: str):
        self.kind = kind
        messages = {
            "missing": "태그 또는 원본 저장소가 존재하지 않습니다.",
            "auth": "인증 또는 접근 권한을 확인해 주세요.",
            "rate": "레지스트리의 요청 한도에 도달했습니다.",
            "server": "레지스트리 서버에 오류가 발생했습니다.",
            "timeout": "레지스트리 작업 시간이 초과되었습니다.",
            "unknown": "이미지 도구 실행에 실패했습니다. 인증과 연결 상태를 확인해 주세요.",
        }
        super().__init__(messages[kind])


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit(scope: str, message: str, level: str | None = None) -> None:
    text = f"[{scope}] {message}"
    for key in ("DOCKERHUB_NAMESPACE", "DOCKERHUB_USERNAME", "DOCKERHUB_TOKEN"):
        secret = os.environ.get(key)
        if secret:
            text = text.replace(secret, "***")
    if level:
        text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::{level}::{text}", flush=True)
    else:
        print(text, flush=True)


def atomic_write(path: Path, text: str) -> None:
    # 같은 파일시스템에서 교체하여 쓰기 도중의 손상을 방지합니다.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_state(path: Path, state: dict) -> None:
    atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise MirrorError(message)


def valid_tag(value) -> bool:
    return isinstance(value, str) and TAG.fullmatch(value) is not None


def valid_digest(value) -> bool:
    return isinstance(value, str) and DIGEST.fullmatch(value) is not None


def valid_time(value) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        return True
    except ValueError:
        return False


def valid_source(value) -> bool:
    return isinstance(value, str) and SOURCE.fullmatch(value) is not None


def valid_repository(value) -> bool:
    return isinstance(value, str) and 2 <= len(value) <= 255 and REPOSITORY.fullmatch(value) is not None


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "상태 파일에 중복된 항목이 있습니다.")
        result[key] = value
    return result


def load_inputs(config_path: Path, state_path: Path) -> tuple[list[dict], dict]:
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        state = json.loads(state_path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except (OSError, ValueError, yaml.YAMLError):
        raise MirrorError("설정 또는 상태 파일을 읽을 수 없습니다. 파일과 형식을 확인해 주세요.") from None
    return validate_inputs(config, state), state


def validate_inputs(config: dict, state: dict) -> list[dict]:
    """미러링 실행과 감시 대상 관리에서 같은 검증 규칙을 사용합니다."""
    require(isinstance(config, dict) and set(config) == {"images"}, "설정에는 images 목록이 필요합니다.")
    images = config["images"]
    require(isinstance(images, list), "감시 대상은 목록이어야 합니다.")
    ids, destinations = set(), set()
    for image in images:
        require(isinstance(image, dict) and set(image) == {
            "id", "source", "destination", "enabled", "include", "exclude"
        }, "감시 대상의 필수 항목과 항목 이름을 확인해 주세요.")
        identifier = image["id"]
        require(isinstance(identifier, str) and IDENTIFIER.fullmatch(identifier) is not None,
                "감시 식별자는 영문 소문자·숫자·밑줄·하이픈으로 작성해 주세요.")
        require(identifier not in ids, "감시 식별자가 중복되었습니다.")
        ids.add(identifier)
        require(valid_source(image["source"]), "원본에는 인증정보나 태그 없이 레지스트리와 이미지 경로를 적어 주세요.")
        require(valid_repository(image["destination"]), "대상에는 올바른 Docker Hub 저장소 이름만 적어 주세요.")
        require(image["destination"] not in destinations, "대상 저장소가 중복되었습니다.")
        destinations.add(image["destination"])
        require(type(image["enabled"]) is bool, "사용 여부는 true 또는 false여야 합니다.")
        for field in ("include", "exclude"):
            patterns = image[field]
            require(isinstance(patterns, list) and all(isinstance(p, str) for p in patterns),
                    "태그 필터는 문자열 목록이어야 합니다.")
            try:
                for pattern in patterns:
                    re.compile(pattern)
            except re.error:
                raise MirrorError("태그 필터의 정규식이 올바르지 않습니다.") from None
    validate_state(state)
    for image in images:
        previous = state.get(image["id"])
        if previous is not None:
            require(all(previous[key] == image[key] for key in ("source", "destination")),
                    "기존 식별자의 원본 또는 대상이 변경되었습니다. 새 식별자로 등록해 주세요.")
    return images


def validate_state(state: dict) -> None:
    require(isinstance(state, dict), "상태 파일의 최상위 항목은 객체여야 합니다.")
    fields = {"source", "destination", "initialized_at", "last_checked_at",
              "baseline_tags", "mirrored_tags", "skipped_tags"}
    for identifier, entry in state.items():
        require(isinstance(identifier, str) and IDENTIFIER.fullmatch(identifier) is not None,
                "상태 파일의 감시 식별자가 올바르지 않습니다.")
        require(isinstance(entry, dict) and set(entry) == fields, "상태 파일의 이미지 기록 구조가 올바르지 않습니다.")
        require(valid_source(entry["source"]) and valid_repository(entry["destination"]),
                "상태 파일의 원본 또는 대상이 올바르지 않습니다.")
        require(valid_time(entry["initialized_at"]) and valid_time(entry["last_checked_at"]),
                "상태 파일의 확인 시각이 올바르지 않습니다.")
        baseline, mirrored, skipped = (entry[k] for k in ("baseline_tags", "mirrored_tags", "skipped_tags"))
        require(isinstance(baseline, list) and all(valid_tag(t) for t in baseline), "기준선 태그가 올바르지 않습니다.")
        require(len(baseline) == len(set(baseline)), "기준선 태그가 중복되었습니다.")
        require(isinstance(mirrored, dict) and isinstance(skipped, dict), "처리 이력은 객체여야 합니다.")
        require(all(valid_tag(t) for t in [*mirrored, *skipped]), "처리 이력의 태그가 올바르지 않습니다.")
        require(not (set(baseline) & (set(mirrored) | set(skipped)) or set(mirrored) & set(skipped)),
                "기준선과 처리 이력에 같은 태그가 중복되었습니다.")
        for record in mirrored.values():
            require(isinstance(record, dict) and set(record) == {"digest", "verified", "platforms", "mirrored_at"},
                    "미러링 기록 구조가 올바르지 않습니다.")
            platforms = record["platforms"]
            require(valid_digest(record["digest"]) and record["verified"] is True
                    and valid_time(record["mirrored_at"]), "미러링 검증 기록이 올바르지 않습니다.")
            require(isinstance(platforms, list) and bool(platforms)
                    and all(isinstance(p, str) and PLATFORM.fullmatch(p) for p in platforms),
                    "미러링 플랫폼 기록이 올바르지 않습니다.")
        for record in skipped.values():
            require(isinstance(record, dict) and set(record) == {
                "reason", "source_digest", "destination_digest", "detected_at"
            }, "건너뜀 기록 구조가 올바르지 않습니다.")
            require(record["reason"] == "destination_digest_mismatch"
                    and valid_digest(record["source_digest"]) and valid_digest(record["destination_digest"])
                    and record["source_digest"] != record["destination_digest"]
                    and valid_time(record["detected_at"]), "건너뜀 기록이 올바르지 않습니다.")


def candidates(image: dict, entry: dict, tags: list[str]) -> list[str]:
    known = set(entry["baseline_tags"]) | set(entry["mirrored_tags"]) | set(entry["skipped_tags"])
    return sorted(tag for tag in set(tags) if tag not in known
                  and (not image["include"] or any(re.fullmatch(p, tag) for p in image["include"]))
                  and not any(re.fullmatch(p, tag) for p in image["exclude"]))


def classify_error(output: str) -> str:
    # HEAD 실패 후 GET으로 재시도한 출력에도 권한·호출 제한 오류를 우선합니다.
    if re.search(r"\b(?:UNAUTHORIZED|DENIED|FORBIDDEN)\b|(?:status(?: code)?[: ]+|HTTP[/\d. ]+)(?:401|403)\b", output, re.I):
        return "auth"
    if re.search(r"\b(?:TOOMANYREQUESTS|429)\b", output, re.I):
        return "rate"
    if re.search(r"(?:status(?: code)?[: ]+|HTTP[/\d. ]+)5\d\d\b", output, re.I):
        return "server"
    # 단순한 비정상 종료나 임의의 'not found' 문구는 태그 부재로 취급하지 않습니다.
    terminal = output.strip().splitlines()[-1] if output.strip() else ""
    if re.search(r"\b(?:MANIFEST_UNKNOWN|NAME_UNKNOWN)\b|(?:unexpected status code[: ]+)404\b", terminal, re.I):
        return "missing"
    return "unknown"


class Crane:
    def run(self, *args: str, timeout: int = 180) -> str:
        try:
            result = subprocess.run(["crane", *args], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise CraneError("timeout") from None
        except OSError:
            raise MirrorError("이미지 도구를 실행할 수 없습니다. crane 설치를 확인해 주세요.") from None
        if result.returncode:
            raise CraneError(classify_error(result.stderr))
        return result.stdout.strip()

    def tags(self, source: str) -> list[str]:
        tags = self.run("ls", source).splitlines()
        require(all(valid_tag(t) for t in tags), "원본의 태그 목록 응답이 올바르지 않습니다.")
        return sorted(set(tags))

    def digest(self, reference: str, allow_missing: bool = False) -> str | None:
        try:
            digest = self.run("digest", reference)
        except CraneError as error:
            if allow_missing and error.kind == "missing":
                return None
            raise
        require(valid_digest(digest), "레지스트리의 다이제스트 응답이 올바르지 않습니다.")
        return digest

    def copy(self, source: str, destination: str) -> None:
        self.run("copy", "--no-clobber", source, destination, timeout=1800)

    def document(self, command: str, reference: str) -> dict:
        try:
            document = json.loads(self.run(command, reference))
        except ValueError:
            raise MirrorError("이미지 메타데이터 응답이 올바르지 않습니다.") from None
        require(isinstance(document, dict), "이미지 메타데이터는 객체여야 합니다.")
        return document

    def platforms(self, reference: str, depth: int = 0) -> list[str]:
        require(depth < 8, "이미지 색인의 중첩 깊이가 너무 큽니다.")
        manifest = self.document("manifest", reference)
        if "manifests" not in manifest:
            return [platform_name(self.document("config", reference))]
        descriptors = manifest["manifests"]
        require(isinstance(descriptors, list), "이미지 색인의 플랫폼 목록이 올바르지 않습니다.")
        platforms = set()
        for descriptor in descriptors:
            require(isinstance(descriptor, dict), "이미지 색인 항목이 올바르지 않습니다.")
            platform = descriptor.get("platform", {})
            annotations = descriptor.get("annotations", {})
            require(isinstance(platform, dict) and isinstance(annotations, dict), "이미지 색인 항목이 올바르지 않습니다.")
            if (annotations.get("vnd.docker.reference.type") == "attestation-manifest"
                    or platform.get("os") == "unknown" or platform.get("architecture") == "unknown"):
                continue
            if platform.get("os") and platform.get("architecture"):
                platforms.add(platform_name(platform))
            else:
                digest = descriptor.get("digest")
                require(valid_digest(digest), "하위 이미지의 다이제스트가 올바르지 않습니다.")
                child = reference.split("@", 1)[0] + "@" + digest
                platforms.update(self.platforms(child, depth + 1))
        require(bool(platforms), "실행 가능한 이미지 플랫폼을 확인할 수 없습니다.")
        return sorted(platforms)


def platform_name(platform: dict) -> str:
    parts = [platform.get("os"), platform.get("architecture")]
    if platform.get("variant"):
        parts.append(platform["variant"])
    require(all(isinstance(p, str) and p != "unknown" for p in parts), "이미지 플랫폼 정보가 올바르지 않습니다.")
    name = "/".join(parts)
    require(PLATFORM.fullmatch(name) is not None, "이미지 플랫폼 이름이 올바르지 않습니다.")
    return name


def check_destination(namespace: str, repository: str) -> None:
    # 공개 메타데이터만 조회합니다. 계정명과 응답 본문은 출력·저장하지 않습니다.
    url = f"https://hub.docker.com/v2/namespaces/{namespace}/repositories/{repository}"
    try:
        with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=30) as response:
            info = json.load(response)
    except HTTPError as error:
        if error.code == 404:
            raise MirrorError("대상 공개 저장소를 찾을 수 없습니다. 저장소를 미리 생성해 주세요.") from None
        raise MirrorError("대상 저장소 설정 조회에 실패했습니다. 다음 실행에서 다시 시도합니다.") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise MirrorError("대상 저장소 설정 조회에 실패했습니다. 다음 실행에서 다시 시도합니다.") from None
    require(isinstance(info, dict) and info.get("is_private") is False, "대상 저장소의 공개 설정을 확인할 수 없습니다.")
    settings = info.get("immutable_tags_settings", {})
    require(isinstance(settings, dict) and settings.get("enabled") is True
            and isinstance(settings.get("rules"), list)
            and any(rule in (".*", "^.*$") for rule in settings["rules"]),
            "대상 저장소에서 모든 태그의 변경 불가 설정을 켜 주세요. 전체 일치 규칙은 .* 입니다.")


def markdown(value) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;").replace("\n", " ").replace("\r", " ").replace("`", "&#96;")


def render_status(readme: str, images: list[dict], state: dict) -> str:
    require(readme.count(START) == 1 and readme.count(END) == 1
            and readme.index(START) < readme.index(END), "README의 상태 영역 표시를 확인해 주세요.")
    rows = ["", "### 감시 대상", "", "시각은 모두 UTC입니다. 검증은 처리 당시의 다이제스트 일치를 의미합니다.", "",
            "| 식별자 | 원본 이미지 | 대상 저장소 | 사용 여부 | 태그 필터 | 기준선 태그 수 | 등록 시각 | 마지막 확인 |",
            "|---|---|---|---|---|---:|---|---|"]
    for image in images:
        entry = state.get(image["id"], {})
        filters = "포함: " + (", ".join(image["include"]) or "전체") + "; 제외: " + (", ".join(image["exclude"]) or "없음")
        cells = [image["id"], image["source"], image["destination"], "사용" if image["enabled"] else "중지", filters,
                 len(entry["baseline_tags"]) if entry else "미등록", entry.get("initialized_at", "—"), entry.get("last_checked_at", "—")]
        rows.append("| " + " | ".join(markdown(c) for c in cells) + " |")
    rows.extend(["", "### 처리 이력", "", "| 원본 이미지 | 태그 | 다이제스트 | 대상 | 플랫폼 | 처리 시각 | 상태 |",
                 "|---|---|---|---|---|---|---|"])
    history = []
    for identifier, entry in state.items():
        for tag, record in entry["mirrored_tags"].items():
            history.append((record["mirrored_at"], identifier, tag, [entry["source"], tag, record["digest"].split(":")[1][:12],
                f"{entry['destination']}:{tag}", ", ".join(record["platforms"]), record["mirrored_at"], "✅ 검증됨"]))
        for tag, record in entry["skipped_tags"].items():
            digest = "원본 " + record["source_digest"].split(":")[1][:12] + " / 대상 " + record["destination_digest"].split(":")[1][:12]
            history.append((record["detected_at"], identifier, tag, [entry["source"], tag, digest,
                f"{entry['destination']}:{tag}", "—", record["detected_at"], "⚠️ 대상 내용 충돌로 건너뜀"]))
    for _, _, _, cells in sorted(history, reverse=True):
        rows.append("| " + " | ".join(markdown(c) for c in cells) + " |")
    if not history:
        rows.extend(["", "아직 처리 이력이 없습니다."])
    rows.append("")
    before, remainder = readme.split(START)
    _, after = remainder.split(END)
    return before + START + "\n" + "\n".join(rows) + END + after


def process_tag(image: dict, entry: dict, tag: str, namespace: str, crane: Crane) -> None:
    destination = f"docker.io/{namespace}/{image['destination']}:{tag}"
    digest = crane.digest(f"{image['source']}:{tag}")
    pinned = f"{image['source']}@{digest}"
    existing = crane.digest(destination, allow_missing=True)
    copy_error = None
    copied = False
    if existing is None:
        # 권한이나 통신 오류를 태그 부재로 오해하지 않고, 불변 정책도 복사 직전에 확인합니다.
        check_destination(namespace, image["destination"])
        platforms = crane.platforms(pinned)
        try:
            crane.copy(pinned, destination)
            copied = True
        except MirrorError as error:
            copy_error = error
        existing = crane.digest(destination, allow_missing=True)
        if existing is None:
            raise copy_error or MirrorError("복사 후 대상 태그를 확인할 수 없습니다.")
    elif existing == digest:
        platforms = crane.platforms(pinned)
    if existing != digest:
        if copied:
            raise MirrorError("복사 후 다이제스트가 일치하지 않습니다. 성공 기록을 남기지 않습니다.")
        entry["skipped_tags"][tag] = {
            "reason": "destination_digest_mismatch", "source_digest": digest,
            "destination_digest": existing, "detected_at": now(),
        }
        emit(f"{image['id']}:{tag}", "대상 태그의 내용이 달라 건너뜁니다.", "warning")
        return
    entry["mirrored_tags"][tag] = {"digest": digest, "verified": True,
        "platforms": platforms, "mirrored_at": now()}
    emit(f"{image['id']}:{tag}", "원본과 대상의 다이제스트 일치를 확인했습니다.")


def run(config_path: Path, state_path: Path, readme_path: Path, namespace: str, crane: Crane | None = None) -> int:
    require(isinstance(namespace, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", namespace) is not None,
            "Docker Hub 비밀 설정과 대상 이름 공간을 확인해 주세요.")
    images, state = load_inputs(config_path, state_path)
    readme = readme_path.read_text(encoding="utf-8")
    render_status(readme, images, state)  # 외부 작업을 시작하기 전에 표시 영역부터 검증합니다.
    crane = crane or Crane()
    failures = 0
    try:
        for image in images:
            if not image["enabled"]:
                continue
            try:
                tags = crane.tags(image["source"])
                checked_at = now()
                if image["id"] not in state:
                    state[image["id"]] = {"source": image["source"], "destination": image["destination"],
                        "initialized_at": checked_at, "last_checked_at": checked_at,
                        "baseline_tags": tags, "mirrored_tags": {}, "skipped_tags": {}}
                    save_state(state_path, state)
                    emit(image["id"], f"최초 기준선 태그 {len(tags)}개를 저장했습니다. 복사는 하지 않습니다.")
                    continue
                entry = state[image["id"]]
                entry["last_checked_at"] = checked_at
                save_state(state_path, state)
                pending = candidates(image, entry, tags)
                emit(image["id"], f"새 미러링 후보는 {len(pending)}개입니다.")
                for tag in pending:
                    try:
                        process_tag(image, entry, tag, namespace, crane)
                    except MirrorError as error:
                        failures += 1
                        emit(f"{image['id']}:{tag}", str(error) + " 다음 실행에서 다시 시도합니다.", "error")
                    save_state(state_path, state)
            except MirrorError as error:
                failures += 1
                emit(image["id"], str(error), "error")
    finally:
        atomic_write(readme_path, render_status(readme, images, state))
    emit("미러링", f"확인을 마쳤습니다. 처리 오류는 {failures}건입니다.")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="새 이미지 태그를 모든 아키텍처와 함께 미러링합니다.")
    parser.add_argument("--config", type=Path, default=ROOT / "config/images.yaml", help="감시 설정 파일")
    parser.add_argument("--state", type=Path, default=ROOT / "state/images.json", help="처리 기록 파일")
    parser.add_argument("--readme", type=Path, default=ROOT / "README.md", help="현황을 갱신할 안내 파일")
    args = parser.parse_args()
    try:
        return run(args.config, args.state, args.readme, os.environ.get("DOCKERHUB_NAMESPACE", ""))
    except (MirrorError, OSError, UnicodeError):
        error = sys.exc_info()[1]
        emit("미러링", str(error) if isinstance(error, MirrorError) else "파일 입출력에 실패했습니다. 저장소 파일과 권한을 확인해 주세요.", "error")
        return 1
    except Exception:
        # 원시 예외·명령·HTTP 응답에는 비밀값이 있을 수 있어 그대로 출력하지 않습니다.
        emit("미러링", "예상하지 못한 오류가 발생했습니다. 기록된 진행 상황은 다음 실행에서 확인합니다.", "error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
