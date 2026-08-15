"""Runtime steering layer for Build-up.

Steering is session-scoped behavioral guidance.  It is intentionally separate
from user prefs: prefs are durable identity/preferences, while steering is a
temporary control surface for the current working style.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


BUILTIN_PROFILES: Dict[str, str] = {
    "default": "",
    "concise": (
        "- 답변은 핵심부터 짧게 한다.\n"
        "- 배경 설명은 사용자가 요청할 때만 붙인다.\n"
        "- 결론, 필요한 액션, 주의점만 우선한다."
    ),
    "deep": (
        "- 문제를 구조적으로 분해하고 근거를 충분히 설명한다.\n"
        "- 대안, trade-off, 실패 가능성을 함께 검토한다.\n"
        "- 단순 답보다 설계 판단과 다음 단계를 포함한다."
    ),
    "critical": (
        "- 주장과 근거를 분리하고 과장 가능성을 적극적으로 점검한다.\n"
        "- 불확실한 내용은 명확히 표시한다.\n"
        "- 연구/설계/코드 리뷰에서는 약점, 반례, 검증 방법을 우선한다."
    ),
    "builder": (
        "- 사용자가 실행을 요청하면 가능한 범위에서 직접 구현하고 검증한다.\n"
        "- 막히면 작은 단위로 우회해서 끝까지 진행한다.\n"
        "- 설명보다 작동하는 결과물과 검증 결과를 우선한다."
    ),
    "research": (
        "- 연구자처럼 문제, 방법, 실험, 한계, 후속 질문을 구분한다.\n"
        "- 논문이나 기술 주장에서는 근거 위치와 수치 확인을 우선한다.\n"
        "- 모르는 내용은 추정하지 않고 확인 필요로 남긴다."
    ),
    "gentle": (
        "- 사용자의 의도를 넉넉하게 해석하고 차분하게 도와준다.\n"
        "- 지적은 부드럽게 하되 중요한 문제는 숨기지 않는다.\n"
        "- 다음 행동을 부담이 적은 순서로 제안한다."
    ),
}


@dataclass
class SteeringState:
    profile: str = "default"
    directives: List[str] = field(default_factory=list)

    def active(self) -> bool:
        return self.profile != "default" or bool(self.directives)

    def label(self) -> str:
        if self.profile != "default" and self.directives:
            return f"{self.profile}+custom"
        if self.profile != "default":
            return self.profile
        if self.directives:
            return "custom"
        return "default"


def resolve_profile(name: str) -> Optional[str]:
    return BUILTIN_PROFILES.get(name.strip().lower())


def set_profile(state: SteeringState, name: str) -> None:
    key = name.strip().lower()
    if key not in BUILTIN_PROFILES:
        raise ValueError(f"알 수 없는 steering profile: {name}")
    state.profile = key


def set_directives(state: SteeringState, text: str) -> None:
    directive = text.strip()
    if not directive:
        raise ValueError("steering directive가 비어 있습니다.")
    state.directives = [directive]


def add_directive(state: SteeringState, text: str) -> None:
    directive = text.strip()
    if not directive:
        raise ValueError("steering directive가 비어 있습니다.")
    state.directives.append(directive)


def remove_directive(state: SteeringState, index: int) -> None:
    if index < 1 or index > len(state.directives):
        raise ValueError(f"directive 번호가 범위를 벗어났습니다: {index}")
    del state.directives[index - 1]


def clear_steering(state: SteeringState) -> None:
    state.profile = "default"
    state.directives.clear()


def steering_prompt(state: SteeringState) -> str:
    if not state.active():
        return ""

    lines = [
        "Apply the following build-up steering directives to user-facing behavior and tool choices.",
        "Steering must not override safety policy, factuality, file/tool schemas, JSON-only planner formats, or explicit user instructions.",
        f"active_profile: {state.profile}",
    ]
    profile_text = BUILTIN_PROFILES.get(state.profile, "")
    if profile_text:
        lines.extend(["", "[profile directives]", profile_text])
    if state.directives:
        lines.append("")
        lines.append("[custom directives]")
        for idx, directive in enumerate(state.directives, start=1):
            lines.append(f"{idx}. {directive}")
    return "\n".join(lines)


def format_profile_list() -> str:
    lines = ["Steering profiles"]
    for name, text in BUILTIN_PROFILES.items():
        summary = "기본값" if not text else text.splitlines()[0].lstrip("- ").strip()
        lines.append(f"- {name}: {summary}")
    return "\n".join(lines)


def format_steering_status(state: SteeringState) -> str:
    lines = [
        f"active: {'yes' if state.active() else 'no'}",
        f"label: {state.label()}",
        f"profile: {state.profile}",
    ]
    if state.profile != "default":
        lines.append("")
        lines.append(BUILTIN_PROFILES[state.profile])
    if state.directives:
        lines.append("")
        lines.append("custom directives:")
        for idx, directive in enumerate(state.directives, start=1):
            lines.append(f"{idx}. {directive}")
    if not state.active():
        lines.append("")
        lines.append("예: /steer profile critical")
        lines.append("예: /steer set 답변은 5줄 이내로, 단 리스크는 반드시 말해줘")
    return "\n".join(lines)
