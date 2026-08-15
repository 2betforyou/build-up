"""Filesystem store for append-only Knowledge Vault ledgers and compiled pages."""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from buildup.config import BuildupConfig
from buildup.research.models import now_iso
from buildup.sandbox import require_knowledge_write


KNOWLEDGE_SCHEMA_VERSION = 1
PAGE_CATEGORIES = (
    "concepts",
    "methods",
    "models",
    "datasets",
    "papers",
    "claims",
    "contradictions",
    "open-questions",
    "synthesis",
)
LEDGER_FILES = ("sources.jsonl", "claims.jsonl", "log.jsonl")
MAX_LEDGER_LINE_BYTES = 2 * 1024 * 1024
MAX_LEDGER_ROWS = 200_000

SCHEMA_YAML = """# Build-up Knowledge Vault schema. Generated; do not edit in place.
schema_version: 1
storage:
  source_of_truth: sealed-deep-research-artifacts
  ledgers: append-only
  pages: deterministic-derived-views
  wiki_pages_are_evidence: false
claim_statuses:
  - draft
  - supported
  - verified
  - disputed
  - stale
  - rejected
automation:
  auto_ingest: true
  auto_compile: true
  auto_lint: true
  auto_verify: never
  auto_destructive_edit: false
  auto_cross_vault: false
required_provenance:
  - research_source_id
  - source_version_id
  - artifact_path
  - url
  - normalized_url
  - content_sha256
  - research_evidence_id
  - passage_sha256
optional_original_pdf_provenance:
  - raw_artifact_path
  - raw_sha256
  - raw_bytes
"""


class KnowledgeStoreError(ValueError):
    pass


def validate_vault_id(vault_id: str) -> str:
    from buildup.paths import validate_job_id

    value = validate_job_id(vault_id)
    if len(value) > 100:
        raise ValueError("vault ID가 너무 깁니다 (최대 100자).")
    return value


class KnowledgeVaultStore:
    def __init__(self, vault_id: str, cfg: BuildupConfig):
        self.vault_id = validate_vault_id(vault_id)
        self.cfg = cfg
        if cfg.knowledge_dir.is_symlink() or cfg.knowledge_vaults_dir.is_symlink():
            raise KnowledgeStoreError("knowledge root directories cannot be symlinks")
        candidate = cfg.knowledge_vaults_dir / self.vault_id
        if candidate.is_symlink():
            raise KnowledgeStoreError("knowledge vault root cannot be a symlink")
        self.root = require_knowledge_write(
            candidate,
            cfg,
            context="knowledge vault",
        )

    @property
    def schema_path(self) -> Path:
        return self.root / "schema.yaml"

    @property
    def metadata_path(self) -> Path:
        return self.root / "vault.json"

    @property
    def sources_path(self) -> Path:
        return self.root / "sources.jsonl"

    @property
    def claims_path(self) -> Path:
        return self.root / "claims.jsonl"

    @property
    def log_path(self) -> Path:
        return self.root / "log.jsonl"

    @property
    def proposals_dir(self) -> Path:
        return self.root / "proposals"

    @property
    def pages_dir(self) -> Path:
        return self.root / "pages"

    def initialize(self, *, title: str = "") -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._ensure_directory(self.proposals_dir)
        self._ensure_directory(self.pages_dir)
        for category in PAGE_CATEGORIES:
            self._ensure_directory(self.pages_dir / category)
        if not self.schema_path.exists():
            self.atomic_text(self.schema_path, SCHEMA_YAML)
        if not self.metadata_path.exists():
            self.atomic_json(self.metadata_path, {
                "schema_version": KNOWLEDGE_SCHEMA_VERSION,
                "vault_id": self.vault_id,
                "title": title.strip() or self.vault_id,
                "created_at": now_iso(),
                "storage": "append-only-ledgers+compiled-markdown",
            })
        for filename in LEDGER_FILES:
            path = self.root / filename
            if not path.exists():
                self.atomic_text(path, "")
        index = self.root / "index.md"
        if not index.exists():
            self.atomic_text(
                index,
                f"# Knowledge Vault — {self.vault_id}\n\n아직 반영된 지식이 없습니다.\n",
            )

    def lease(self) -> "KnowledgeLease":
        return KnowledgeLease(self.root / ".vault.lock", self.cfg)

    def read_sources(self) -> List[Dict[str, Any]]:
        return self.read_jsonl(self.sources_path)

    def read_claim_events(self) -> List[Dict[str, Any]]:
        return self.read_jsonl(self.claims_path)

    def read_log(self) -> List[Dict[str, Any]]:
        return self.read_jsonl(self.log_path)

    def read_jsonl(self, path: Path) -> List[Dict[str, Any]]:
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise KnowledgeStoreError(f"unsafe knowledge ledger path: {path}")
        if not path.is_file():
            return []
        rows: List[Dict[str, Any]] = []
        with path.open("rb") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if line_number > MAX_LEDGER_ROWS:
                    raise KnowledgeStoreError(f"ledger row limit exceeded: {path}")
                if len(raw) > MAX_LEDGER_LINE_BYTES:
                    raise KnowledgeStoreError(
                        f"ledger line is too large: {path}:{line_number}"
                    )
                if not raw.strip():
                    continue
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise KnowledgeStoreError(
                        f"invalid JSONL: {path}:{line_number}"
                    ) from exc
                if not isinstance(value, dict):
                    raise KnowledgeStoreError(
                        f"JSONL row must be an object: {path}:{line_number}"
                    )
                rows.append(value)
        return rows

    def append_sources(self, rows: Iterable[Mapping[str, Any]]) -> None:
        self._append_jsonl(self.sources_path, rows)

    def append_claim_events(self, rows: Iterable[Mapping[str, Any]]) -> None:
        self._append_jsonl(self.claims_path, rows)

    def append_log(self, event: str, payload: Mapping[str, Any]) -> None:
        self._append_jsonl(self.log_path, ({
            "at": now_iso(),
            "event": event,
            **dict(payload),
        },))

    def write_proposal(self, proposal: Mapping[str, Any]) -> Path:
        proposal_id = str(proposal.get("proposal_id") or "")
        if not proposal_id or not proposal_id.isalnum():
            raise KnowledgeStoreError("invalid proposal ID")
        path = self.proposals_dir / f"{proposal_id}.json"
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing != dict(proposal):
                raise KnowledgeStoreError(
                    f"immutable proposal collision: {proposal_id}"
                )
            return path
        self.atomic_json(path, dict(proposal))
        return path

    def read_proposals(self) -> List[Dict[str, Any]]:
        proposals: List[Dict[str, Any]] = []
        if not self.proposals_dir.exists():
            return proposals
        if (
            self.proposals_dir.is_symlink()
            or not self.proposals_dir.resolve().is_relative_to(self.root.resolve())
        ):
            raise KnowledgeStoreError("unsafe knowledge proposals directory")
        for path in sorted(self.proposals_dir.glob("*.json")):
            if path.is_symlink():
                raise KnowledgeStoreError(f"proposal cannot be a symlink: {path}")
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise KnowledgeStoreError(f"invalid proposal JSON: {path}") from exc
            if not isinstance(payload, dict):
                raise KnowledgeStoreError(f"proposal must be a JSON object: {path}")
            if str(payload.get("proposal_id") or "") != path.stem:
                raise KnowledgeStoreError(f"proposal filename/identity mismatch: {path}")
            proposals.append(payload)
        return proposals

    def write_compiled(self, files: Mapping[str, str]) -> None:
        """Replace only recoverable Markdown views, never a ledger or proposal."""
        expected: set[Path] = set()
        for relative in files:
            rel = Path(relative)
            if rel.is_absolute() or ".." in rel.parts:
                raise KnowledgeStoreError(f"unsafe compiled path: {relative}")
            target = (self.root / rel).resolve()
            if target != (self.root / "index.md").resolve() and not target.is_relative_to(
                self.pages_dir.resolve()
            ):
                raise KnowledgeStoreError(f"compiled path outside page views: {relative}")
            expected.add(target)

        # Remove only generated Markdown files in known category directories.
        # The append-only ledgers make this fully reproducible.
        for category in PAGE_CATEGORIES:
            category_dir = self.pages_dir / category
            for existing in category_dir.glob("*.md"):
                resolved = existing.resolve()
                if existing.is_symlink() or not resolved.is_relative_to(category_dir.resolve()):
                    raise KnowledgeStoreError(f"unsafe generated page: {existing}")
                if resolved not in expected:
                    existing.unlink()
        for relative, content in files.items():
            self.atomic_text(self.root / relative, content.rstrip() + "\n")

    def atomic_json(self, path: Path, value: Any) -> None:
        self.atomic_text(
            path,
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def atomic_text(self, path: Path, text: str) -> None:
        target = require_knowledge_write(path, self.cfg, context="knowledge atomic write")
        if not target.is_relative_to(self.root.resolve()):
            raise KnowledgeStoreError(f"knowledge write escaped its vault: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _append_jsonl(
        self,
        path: Path,
        rows: Iterable[Mapping[str, Any]],
    ) -> None:
        target = require_knowledge_write(path, self.cfg, context="knowledge append")
        if not target.is_relative_to(self.root.resolve()):
            raise KnowledgeStoreError(f"knowledge append escaped its vault: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        serialized: List[str] = []
        for row in rows:
            line = json.dumps(dict(row), ensure_ascii=False, sort_keys=True)
            if len(line.encode("utf-8")) > MAX_LEDGER_LINE_BYTES:
                raise KnowledgeStoreError("knowledge ledger row exceeds size limit")
            serialized.append(line + "\n")
        if not serialized:
            return
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.writelines(serialized)
            handle.flush()
            os.fsync(handle.fileno())

    def _ensure_directory(self, path: Path) -> None:
        if path.is_symlink():
            raise KnowledgeStoreError(f"knowledge directory cannot be a symlink: {path}")
        path.mkdir(parents=True, exist_ok=True)
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise KnowledgeStoreError(f"knowledge directory escaped its vault: {path}")


class KnowledgeLease(AbstractContextManager["KnowledgeLease"]):
    """Small process lease used for vaults and the global binding ledger."""

    def __init__(self, path: Path, cfg: BuildupConfig):
        self.path = require_knowledge_write(path, cfg, context="knowledge lease")
        self.token = uuid.uuid4().hex
        self.acquired = False

    def __enter__(self) -> "KnowledgeLease":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                if not self._owner_alive():
                    self.path.unlink(missing_ok=True)
                    continue
                time.sleep(0.05)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({
                    "pid": os.getpid(),
                    "token": self.token,
                    "created_at": now_iso(),
                }, handle)
                handle.flush()
                os.fsync(handle.fileno())
            self.acquired = True
            return self
        raise TimeoutError(f"knowledge lease timeout: {self.path}")

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if not self.acquired:
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("token") == self.token:
                self.path.unlink(missing_ok=True)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        self.acquired = False

    def _owner_alive(self) -> bool:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            pid = int(payload.get("pid"))
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            try:
                return time.time() - self.path.stat().st_mtime < 30
            except OSError:
                return False
