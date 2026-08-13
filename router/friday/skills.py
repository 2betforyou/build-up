"""Local Agent Skills support for Friday.

Friday treats third-party skills as instruction packages by default.  It reads
SKILL.md files and optional references, but never executes bundled scripts
unless a future permission layer explicitly allows it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from friday.config import FridayConfig


_WORD_RE = re.compile(r"[a-z0-9가-힣][a-z0-9가-힣_-]*", re.I)
_FRONTMATTER_END_RE = re.compile(r"^---\s*$", re.M)
_MAX_INDEX_DESCRIPTION = 1200


_STOP_WORDS = {
    "the", "and", "for", "with", "from", "that", "this", "when", "use",
    "using", "skill", "skills", "agent", "claude", "code", "user", "asks",
    "should", "into", "your", "you", "are", "how", "what", "why",
    "이", "그", "저", "좀", "해줘", "해", "줘", "으로", "에서", "하고",
}


_KOREAN_TRIGGER_BOOSTS: Tuple[Tuple[Tuple[str, ...], Tuple[str, ...], int], ...] = (
    (("코드리뷰", "코드 리뷰", "코드 검토", "소스 검토"), ("code-reviewer", "code review", "pull request", "review checklist"), 34),
    (("아키텍처", "설계", "구조", "시스템 디자인"), ("architect", "architecture", "system-design"), 18),
    (("프론트", "프론트엔드", "react", "next"), ("frontend", "react", "nextjs"), 14),
    (("백엔드", "서버", "api"), ("backend", "api", "server"), 12),
    (("테스트", "qa", "e2e", "playwright"), ("qa", "test", "playwright"), 14),
    (("보안", "취약점", "시큐리티"), ("security", "secops", "red-team", "threat"), 14),
    (("데이터", "분석", "통계"), ("data", "analyst", "statistical"), 10),
    (("제품", "기획", "로드맵", "prd"), ("product", "roadmap", "prd"), 12),
    (("마케팅", "seo", "콘텐츠"), ("marketing", "seo", "content"), 12),
    (("일정", "프로젝트 관리", "pm", "스크럼"), ("project-management", "scrum", "jira"), 10),
    (("문서", "문서화", "보고서"), ("documentation", "writer", "report"), 10),
)


@dataclass(frozen=True)
class SkillInfo:
    """A discovered SKILL.md package."""

    skill_id: str
    name: str
    description: str
    path: Path
    source: str
    relative_path: str
    has_scripts: bool = False
    has_references: bool = False
    has_assets: bool = False
    allowed_tools: str = ""
    metadata: Dict[str, str] | None = None
    source_commit: str = ""

    @property
    def directory(self) -> Path:
        return self.path.parent

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "path": str(self.path),
            "source": self.source,
            "relative_path": self.relative_path,
            "has_scripts": self.has_scripts,
            "has_references": self.has_references,
            "has_assets": self.has_assets,
            "allowed_tools": self.allowed_tools,
            "metadata": dict(self.metadata or {}),
            "source_commit": self.source_commit,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillInfo":
        return cls(
            skill_id=str(data.get("skill_id") or ""),
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            path=Path(str(data.get("path") or "")),
            source=str(data.get("source") or "local"),
            relative_path=str(data.get("relative_path") or ""),
            has_scripts=bool(data.get("has_scripts")),
            has_references=bool(data.get("has_references")),
            has_assets=bool(data.get("has_assets")),
            allowed_tools=str(data.get("allowed_tools") or ""),
            metadata=dict(data.get("metadata") or {}),
            source_commit=str(data.get("source_commit") or ""),
        )


def _skills_index_file(cfg: FridayConfig) -> Path:
    return cfg.base_dir / "data" / "friday" / "skills.index.json"


def _default_skill_sources(cfg: FridayConfig) -> List[Tuple[str, Path]]:
    """Return source roots to scan for SKILL.md files."""
    sources: List[Tuple[str, Path]] = []
    local = cfg.base_dir / "skills"
    if local.exists():
        sources.append(("local", local))

    vendor = cfg.base_dir / "vendor" / "skills"
    if vendor.exists():
        for child in sorted(p for p in vendor.iterdir() if p.is_dir()):
            sources.append((child.name, child))
    builtin = Path(__file__).resolve().parent / "builtin_skills"
    if builtin.exists():
        sources.append(("builtin", builtin))
    return sources


def _read_text(path: Path, max_bytes: int = 600_000) -> str:
    try:
        data = path.read_bytes()[:max_bytes]
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1].strip()
    return value


def _split_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text

    first_line_end = text.find("\n")
    if first_line_end == -1:
        return {}, text

    match = _FRONTMATTER_END_RE.search(text, pos=first_line_end + 1)
    if not match:
        return {}, text

    raw_meta = text[first_line_end + 1:match.start()]
    body = text[match.end():].lstrip()
    metadata: Dict[str, str] = {}
    current_key: Optional[str] = None
    current_value: List[str] = []

    def flush_current() -> None:
        nonlocal current_key, current_value
        if current_key:
            metadata[current_key] = _strip_quotes(" ".join(current_value).strip())
        current_key = None
        current_value = []

    for raw_line in raw_meta.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")) and current_key:
            current_value.append(line.strip())
            continue
        if ":" not in line:
            continue
        flush_current()
        key, value = line.split(":", 1)
        current_key = key.strip()
        current_value = [value.strip()]
    flush_current()
    return metadata, body


def _fallback_description(body: str) -> str:
    paragraphs: List[str] = []
    current: List[str] = []
    in_code = False
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not line or line.startswith("#") or line.startswith("|"):
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        if line.startswith(("-", "*")):
            continue
        current.append(line)
        if len(" ".join(current)) > 160:
            break
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs[0][:_MAX_INDEX_DESCRIPTION] if paragraphs else ""


def _is_hidden_relative(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return any(part.startswith(".") for part in rel.parts)


def _source_commit(root: Path) -> str:
    git_dir = root / ".git"
    head_file = git_dir / "HEAD"
    head = _read_text(head_file, max_bytes=4096).strip()
    if not head:
        return ""
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        return _read_text(git_dir / ref, max_bytes=4096).strip()
    return head


def _skill_id(source: str, root: Path, skill_file: Path, name: str) -> str:
    try:
        rel_dir = skill_file.parent.relative_to(root).as_posix()
    except ValueError:
        rel_dir = skill_file.parent.name
    rel_dir = rel_dir.strip("/") or name
    return f"{source}:{rel_dir}"


def _discover_source(source: str, root: Path) -> List[SkillInfo]:
    commit = _source_commit(root)
    skills: List[SkillInfo] = []
    for skill_file in sorted(root.rglob("SKILL.md")):
        if _is_hidden_relative(skill_file, root):
            continue
        text = _read_text(skill_file)
        if not text:
            continue
        metadata, body = _split_frontmatter(text)
        name = _strip_quotes(metadata.get("name", "")) or skill_file.parent.name
        description = _strip_quotes(metadata.get("description", "")) or _fallback_description(body)
        try:
            rel = skill_file.parent.relative_to(root).as_posix()
        except ValueError:
            rel = skill_file.parent.as_posix()
        skill_dir = skill_file.parent
        skills.append(SkillInfo(
            skill_id=_skill_id(source, root, skill_file, name),
            name=name,
            description=description[:_MAX_INDEX_DESCRIPTION],
            path=skill_file,
            source=source,
            relative_path=rel,
            has_scripts=(skill_dir / "scripts").is_dir(),
            has_references=(skill_dir / "references").is_dir(),
            has_assets=(skill_dir / "assets").is_dir() or (skill_dir / "templates").is_dir(),
            allowed_tools=_strip_quotes(metadata.get("allowed-tools", "")),
            metadata=metadata,
            source_commit=commit,
        ))
    return skills


def discover_skills(cfg: FridayConfig) -> List[SkillInfo]:
    skills: List[SkillInfo] = []
    seen_names: set[str] = set()
    for source, root in _default_skill_sources(cfg):
        for skill in _discover_source(source, root):
            # Source order is override order: local, vendor, then packaged
            # built-ins. A user's skill can intentionally replace a built-in.
            if skill.name in seen_names:
                continue
            seen_names.add(skill.name)
            skills.append(skill)
    return skills


def rebuild_skill_index(cfg: FridayConfig) -> List[SkillInfo]:
    skills = discover_skills(cfg)
    index_file = _skills_index_file(cfg)
    index_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "count": len(skills),
        "skills": [skill.to_dict() for skill in skills],
    }
    index_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return skills


def load_skills(cfg: FridayConfig, refresh: bool = False) -> List[SkillInfo]:
    index_file = _skills_index_file(cfg)
    if not refresh and index_file.exists():
        try:
            payload = json.loads(index_file.read_text(encoding="utf-8"))
            skills = [SkillInfo.from_dict(item) for item in payload.get("skills", [])]
            if (
                payload.get("version") == 2
                and skills
                and all(skill.path.exists() for skill in skills[:20])
            ):
                return skills
        except Exception:
            pass
    return rebuild_skill_index(cfg)


def _tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for token in _WORD_RE.findall(text.lower()):
        parts = re.split(r"[-_]", token)
        for part in parts + [token]:
            part = part.strip()
            if len(part) >= 2 and part not in _STOP_WORDS:
                tokens.add(part)
    return tokens


def _score_skill(query: str, skill: SkillInfo) -> int:
    q = query.lower()
    query_tokens = _tokens(q)
    if not query_tokens:
        return 0

    name_text = skill.name.lower().replace("-", " ")
    desc_text = skill.description.lower()
    path_text = skill.relative_path.lower().replace("/", " ").replace("-", " ")

    name_tokens = _tokens(name_text)
    desc_tokens = _tokens(desc_text)
    path_tokens = _tokens(path_text)

    score = 0
    score += len(query_tokens & name_tokens) * 8
    score += len(query_tokens & desc_tokens) * 3
    score += len(query_tokens & path_tokens) * 2

    compact_query = q.replace(" ", "-")
    if compact_query and (compact_query in skill.name.lower() or compact_query in skill.relative_path.lower()):
        score += 20
    if q and q in desc_text:
        score += 10

    haystack = f"{skill.name} {skill.description} {skill.relative_path}".lower()
    if any(term in q for term in ("딥 리서치", "심층 리서치", "딥리서치")):
        if skill.name == "deep-research":
            score += 80
    if any(term in q for term in ("번역", "번역해줘", "번역해", "한국어로", "초록")):
        if skill.name == "paper-pdf-translation":
            score += 80
    if "코드" in q and any(term in q for term in ("리뷰", "검토")):
        if skill.name == "code-reviewer":
            score += 50
        elif "code review" in haystack or "pull request" in haystack:
            score += 18
    if any(term in q for term in ("아키텍처", "시스템 설계", "구조 설계", "프로젝트 구조")):
        if skill.name == "senior-architect":
            score += 50
        elif "architecture" in haystack or "architect" in haystack:
            score += 14

    for korean_terms, target_terms, boost in _KOREAN_TRIGGER_BOOSTS:
        if any(term in q for term in korean_terms) and any(term in haystack for term in target_terms):
            score += boost

    if skill.name in {"run", "init", "status", "board", "merge", "eval"}:
        score -= 8
    if skill.description.strip().lower() in {"", skill.name.lower()}:
        score -= 4
    return max(score, 0)


def select_skills(
    query: str,
    cfg: FridayConfig,
    *,
    limit: int = 3,
    min_score: int = 8,
) -> List[Tuple[SkillInfo, int]]:
    scored: List[Tuple[SkillInfo, int]] = []
    for skill in load_skills(cfg):
        score = _score_skill(query, skill)
        if score >= min_score:
            scored.append((skill, score))
    scored.sort(key=lambda item: (item[1], len(item[0].description)), reverse=True)

    selected: List[Tuple[SkillInfo, int]] = []
    seen_names: set[str] = set()
    for skill, score in scored:
        if skill.name in seen_names:
            continue
        selected.append((skill, score))
        seen_names.add(skill.name)
        if len(selected) >= limit:
            break
    return selected


def find_skill(identifier: str, cfg: FridayConfig) -> Optional[SkillInfo]:
    needle = identifier.strip().lower()
    if not needle:
        return None
    skills = load_skills(cfg)
    for skill in skills:
        if needle in {skill.skill_id.lower(), skill.name.lower(), skill.relative_path.lower()}:
            return skill
    for skill in skills:
        if needle in skill.skill_id.lower() or needle in skill.relative_path.lower():
            return skill
    for skill in skills:
        if needle in skill.name.lower():
            return skill
    return None


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    suffix = "\n...[truncated]"
    return text[:max(0, max_chars - len(suffix))].rstrip() + suffix


def _skill_body(skill: SkillInfo, max_chars: int) -> str:
    text = _read_text(skill.path)
    _metadata, body = _split_frontmatter(text)
    return _truncate(body.strip(), max_chars)


def build_skills_context(
    query: str,
    cfg: FridayConfig,
    *,
    limit: int = 3,
    max_chars: int = 7000,
) -> Tuple[str, List[Tuple[SkillInfo, int]]]:
    selected = select_skills(query, cfg, limit=limit)
    if not selected:
        return "", []

    lines = [
        "[build-up Skills]",
        "Use these installed Agent Skills as instruction-only guidance for this task.",
        "Do not execute bundled skill scripts or copied command snippets automatically.",
        "If a skill suggests running a script, first explain/preview the action and rely on build-up's normal permission checks.",
    ]
    remaining = max_chars - sum(len(line) + 1 for line in lines)
    per_skill = max(900, remaining // max(1, len(selected)))

    for skill, score in selected:
        body = _skill_body(skill, per_skill)
        section = [
            "",
            f"## Skill: {skill.name}  (score={score})",
            f"id: {skill.skill_id}",
            f"path: {skill.path}",
            f"description: {skill.description}",
        ]
        if skill.has_scripts:
            section.append("note: scripts are present but disabled in build-up's current skill mode.")
        section.append(body)
        lines.extend(section)
    return _truncate("\n".join(lines), max_chars), selected


def format_skill_list(skills: Iterable[SkillInfo], *, max_items: int = 80) -> str:
    items = list(skills)
    if not items:
        return "(no skills installed)"
    lines = [f"installed skills: {len(items)}", ""]
    for idx, skill in enumerate(items[:max_items], start=1):
        flags = []
        if skill.has_scripts:
            flags.append("scripts")
        if skill.has_references:
            flags.append("refs")
        if skill.has_assets:
            flags.append("assets")
        flag_text = f" [{' '.join(flags)}]" if flags else ""
        desc = skill.description.replace("\n", " ").strip()
        if len(desc) > 110:
            desc = desc[:107].rstrip() + "..."
        lines.append(f"{idx:>3}. {skill.name}{flag_text}")
        lines.append(f"     id: {skill.skill_id}")
        if desc:
            lines.append(f"     {desc}")
    if len(items) > max_items:
        lines.append(f"\n... {len(items) - max_items} more. Use `skills search <query>`.")
    return "\n".join(lines)


def format_skill_matches(matches: Iterable[Tuple[SkillInfo, int]]) -> str:
    items = list(matches)
    if not items:
        return "(no matching skills)"
    lines: List[str] = []
    for skill, score in items:
        desc = skill.description.replace("\n", " ").strip()
        if len(desc) > 140:
            desc = desc[:137].rstrip() + "..."
        lines.append(f"- {skill.name}  score={score}")
        lines.append(f"  id: {skill.skill_id}")
        if desc:
            lines.append(f"  {desc}")
    return "\n".join(lines)


def format_skill_detail(skill: SkillInfo, *, include_body: bool = True, max_body_chars: int = 5000) -> str:
    flags = []
    if skill.has_scripts:
        flags.append("scripts-disabled")
    if skill.has_references:
        flags.append("references")
    if skill.has_assets:
        flags.append("assets")
    lines = [
        f"name: {skill.name}",
        f"id: {skill.skill_id}",
        f"source: {skill.source}",
        f"commit: {skill.source_commit or '-'}",
        f"path: {skill.path}",
        f"flags: {', '.join(flags) if flags else '-'}",
        f"description: {skill.description or '-'}",
    ]
    if skill.allowed_tools:
        lines.append(f"allowed-tools: {skill.allowed_tools}")
    if include_body:
        lines.append("\n--- SKILL.md ---")
        lines.append(_skill_body(skill, max_body_chars))
    return "\n".join(lines)


def format_skill_audit(skills: Iterable[SkillInfo]) -> str:
    items = list(skills)
    with_scripts = [s for s in items if s.has_scripts]
    with_refs = [s for s in items if s.has_references]
    by_source: Dict[str, int] = {}
    for skill in items:
        by_source[skill.source] = by_source.get(skill.source, 0) + 1

    lines = [
        f"skills: {len(items)}",
        f"with scripts: {len(with_scripts)} (build-up keeps these disabled)",
        f"with references: {len(with_refs)}",
        "",
        "sources:",
    ]
    for source, count in sorted(by_source.items()):
        lines.append(f"  - {source}: {count}")
    if with_scripts:
        lines.append("\nscript-bearing examples:")
        for skill in with_scripts[:20]:
            lines.append(f"  - {skill.name}: {skill.skill_id}")
        if len(with_scripts) > 20:
            lines.append(f"  ... {len(with_scripts) - 20} more")
    return "\n".join(lines)
