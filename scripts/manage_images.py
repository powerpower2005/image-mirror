"""수동 워크플로에서 감시 대상을 추가·수정·삭제합니다."""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
from pathlib import Path

import yaml

from mirror import (
    IDENTIFIER, ROOT, MirrorError, atomic_write, emit, load_inputs,
    render_status, require, validate_inputs,
)


DEFAULT_INCLUDE = [r"\d+\.\d+\.\d+", r"\d+\.\d+\.\d+-alpine"]


def parse_patterns(value: str, default: list[str]) -> list[str]:
    if not value.strip():
        return list(default)
    try:
        patterns = json.loads(value)
    except ValueError:
        raise MirrorError("태그 필터는 JSON 배열로 입력해 주세요. 예: [\".*\"]") from None
    require(isinstance(patterns, list) and all(isinstance(p, str) for p in patterns),
            "태그 필터는 문자열로 이루어진 JSON 배열이어야 합니다.")
    return patterns


def change_images(images: list[dict], state: dict, operation: str, identifier: str,
                  source: str = "", destination: str = "", enabled: str = "유지",
                  include: str = "", exclude: str = "") -> list[dict]:
    require(operation in ("추가", "수정", "삭제"), "작업은 추가·수정·삭제 중에서 선택해 주세요.")
    require(IDENTIFIER.fullmatch(identifier) is not None, "올바른 감시 식별자를 입력해 주세요.")
    require(enabled in ("유지", "사용", "중지"), "사용 여부는 유지·사용·중지 중에서 선택해 주세요.")
    updated = copy.deepcopy(images)
    existing = next((image for image in updated if image["id"] == identifier), None)
    if operation == "삭제":
        require(existing is not None, "삭제할 감시 식별자가 등록되어 있지 않습니다.")
        require(not any(value.strip() for value in (source, destination, include, exclude)) and enabled == "유지",
                "삭제할 때는 식별자만 입력하고 나머지 항목은 기본값으로 두세요.")
        updated.remove(existing)
    elif operation == "추가":
        require(existing is None, "이미 등록된 식별자입니다. 수정 작업을 선택해 주세요.")
        require(bool(source) and bool(destination), "추가할 때는 원본 이미지와 대상 저장소 이름이 필요합니다.")
        updated.append({"id": identifier, "source": source, "destination": destination,
            "enabled": enabled != "중지", "include": parse_patterns(include, DEFAULT_INCLUDE),
            "exclude": parse_patterns(exclude, [])})
    else:
        require(existing is not None, "수정할 감시 식별자가 등록되어 있지 않습니다.")
        require((not source or source == existing["source"])
                and (not destination or destination == existing["destination"]),
                "같은 식별자의 원본과 대상은 바꿀 수 없습니다. 새 식별자로 추가해 주세요.")
        if enabled != "유지":
            existing["enabled"] = enabled == "사용"
        existing["include"] = parse_patterns(include, existing["include"])
        existing["exclude"] = parse_patterns(exclude, existing["exclude"])
    validate_inputs({"images": updated}, state)
    return updated


def run(config_path: Path, state_path: Path, readme_path: Path, *, operation: str,
        identifier: str, source: str = "", destination: str = "", enabled: str = "유지",
        include: str = "", exclude: str = "", preview: bool = False) -> None:
    images, state = load_inputs(config_path, state_path)
    updated = change_images(images, state, operation, identifier.strip(), source.strip(),
                            destination.strip(), enabled, include, exclude)
    old_config = config_path.read_text(encoding="utf-8")
    old_readme = readme_path.read_text(encoding="utf-8")
    # 내용이 같으면 기존 주석과 서식을 포함하여 파일을 그대로 유지합니다.
    new_config = old_config if images == updated else yaml.safe_dump(
        {"images": updated}, allow_unicode=True, sort_keys=False, width=120)
    new_readme = render_status(old_readme, updated, state)
    if preview:
        emit(identifier.strip(), "미리보기입니다. 설정·기록·안내 파일을 변경하지 않습니다.")
        for label, before, after in (("config/images.yaml", old_config, new_config),
                                     ("README.md", old_readme, new_readme)):
            for line in difflib.unified_diff(before.splitlines(), after.splitlines(),
                                             fromfile=label + " (변경 전)", tofile=label + " (변경 후)", lineterm=""):
                print(line)
        return
    if new_config != old_config:
        atomic_write(config_path, new_config)
    if new_readme != old_readme:
        atomic_write(readme_path, new_readme)
    messages = {"추가": "감시 대상을 추가했습니다. 다음 미러링 실행에서 기준선만 저장합니다.",
                "수정": "감시 설정을 수정했습니다. 기존 기준선과 처리 기록은 유지합니다.",
                "삭제": "감시 설정에서 제거했습니다. 기존 기준선·처리 기록·대상 이미지는 유지합니다."}
    emit(identifier.strip(), messages[operation])


def main() -> int:
    parser = argparse.ArgumentParser(description="감시 대상을 추가·수정·삭제합니다.")
    parser.add_argument("--config", type=Path, default=ROOT / "config/images.yaml", help="감시 설정 파일")
    parser.add_argument("--state", type=Path, default=ROOT / "state/images.json", help="기존 처리 기록 파일")
    parser.add_argument("--readme", type=Path, default=ROOT / "README.md", help="현황을 갱신할 안내 파일")
    parser.add_argument("--operation", default=os.environ.get("INPUT_OPERATION", ""), help="추가·수정·삭제")
    parser.add_argument("--id", dest="identifier", default=os.environ.get("INPUT_ID", ""), help="감시 식별자")
    parser.add_argument("--source", default=os.environ.get("INPUT_SOURCE", ""), help="추가할 공개 원본 이미지")
    parser.add_argument("--destination", default=os.environ.get("INPUT_DESTINATION", ""), help="추가할 대상 저장소 이름")
    parser.add_argument("--enabled", default=os.environ.get("INPUT_ENABLED", "유지"), help="유지·사용·중지")
    parser.add_argument("--include", default=os.environ.get("INPUT_INCLUDE", ""), help="포함 필터의 JSON 배열")
    parser.add_argument("--exclude", default=os.environ.get("INPUT_EXCLUDE", ""), help="제외 필터의 JSON 배열")
    parser.add_argument("--preview", action="store_true", default=os.environ.get("INPUT_PREVIEW", "false") == "true",
                        help="파일을 바꾸지 않고 변경 내용을 확인합니다.")
    args = parser.parse_args()
    try:
        run(args.config, args.state, args.readme, operation=args.operation, identifier=args.identifier,
            source=args.source, destination=args.destination, enabled=args.enabled,
            include=args.include, exclude=args.exclude, preview=args.preview)
    except MirrorError as error:
        emit("감시 대상 관리", str(error), "error")
        return 1
    except (OSError, UnicodeError):
        emit("감시 대상 관리", "파일을 처리하지 못했습니다. 파일과 접근 권한을 확인해 주세요.", "error")
        return 1
    except Exception:
        emit("감시 대상 관리", "예상하지 못한 오류로 변경을 중단했습니다.", "error")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
