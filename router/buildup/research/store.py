"""Atomic checkpoints, audit artifacts, and a single-writer lease."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Dict, Iterable, List

from buildup.config import BuildupConfig
from buildup.sandbox import require_write

from .citations import passage_sha256, verify_exact_passage
from .models import ResearchState, TaskStatus, now_iso, stable_id


STATE_FILE = "state.json"
METADATA_FILE = "metadata.json"
SEALED_ARTIFACTS = (
    STATE_FILE,
    METADATA_FILE,
    "events.jsonl",
    "00-sources.json",
    "01-contract.json",
    "02-plan.json",
    "03-sources.jsonl",
    "04-evidence.jsonl",
    "05-claims.jsonl",
    "05-gaps.json",
    "06-report.md",
    "07-study-guide.md",
    "08-citation-audit.json",
    "09-claim-audit.json",
)
_SOURCE_ID_RE = re.compile(r"^S[0-9A-F]{12}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ResearchStore:
    """Owns all durable files for one research run."""

    def __init__(self, run_dir: Path, job_id: str, cfg: BuildupConfig):
        self.run_dir = require_write(run_dir, job_id, cfg, context="research run")
        self.job_id = job_id
        self.cfg = cfg

    def initialize(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.append_event("run_created", {})

    def load(self) -> ResearchState:
        path = self.run_dir / STATE_FILE
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"재개할 checkpoint가 없습니다: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"research checkpoint가 손상되었습니다: {path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"research checkpoint 형식이 잘못되었습니다: {path}")
        state = ResearchState.from_dict(payload)
        if state.job_id != self.job_id or state.run_id != self.run_dir.name:
            raise ValueError("research checkpoint identity가 실행 경로와 일치하지 않습니다.")
        validate_checkpoint_state(state)
        raw_failures = _raw_source_failures(state, self.run_dir)
        if raw_failures:
            raise ValueError(
                "research raw-source invariant validation failed: "
                + ", ".join(raw_failures)
            )
        return state

    def checkpoint(self, state: ResearchState, *, event: str = "checkpoint") -> None:
        state.touch()
        self._atomic_json(self.run_dir / STATE_FILE, state.to_dict())
        self._write_materialized_artifacts(state)
        self._atomic_json(self.run_dir / METADATA_FILE, self._metadata(state))
        self.append_event(event, {
            "phase": state.phase,
            "status": state.status,
            "round": state.round,
            "sources": len(state.sources),
            "evidence": len(state.evidence),
            "claims": len(state.claims),
            "coverage": state.coverage,
            "searches": state.budget.searches_used,
            "model_calls": state.budget.model_calls_used,
        })

    def record_invalid_model_response(self, phase: str, raw: str) -> Path:
        safe_phase = "".join(char for char in phase if char.isalnum() or char in "-_")[:40]
        path = self.run_dir / f".invalid-{safe_phase or 'model'}-response.txt"
        self._atomic_text(path, raw)
        return path

    def write_report(self, report: str) -> Path:
        path = self.run_dir / "06-report.md"
        self._atomic_text(path, report.rstrip() + "\n")
        return path

    def write_study_guide(self, guide: str) -> Path:
        path = self.run_dir / "07-study-guide.md"
        self._atomic_text(path, guide.rstrip() + "\n")
        return path

    def archive_pdf_source(self, source_id: str, raw: bytes) -> Path:
        """Persist the exact PDF bytes inside this run before the run is sealed."""
        if not _SOURCE_ID_RE.fullmatch(source_id):
            raise ValueError(f"invalid source ID for raw PDF archive: {source_id}")
        if not raw or len(raw) > self.cfg.max_pdf_bytes:
            raise ValueError("raw PDF archive is empty or exceeds the configured size limit")
        if not raw.startswith(b"%PDF-"):
            raise ValueError("raw PDF archive does not have a PDF signature")
        path = self.run_dir / "raw-sources" / f"{source_id}.pdf"
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("raw PDF archive path cannot be a symlink")
        if path.is_file():
            existing = path.read_bytes()
            if existing != raw:
                raise ValueError(f"immutable raw PDF collision: {source_id}")
            return path
        self._atomic_bytes(path, raw)
        return path

    def write_evaluation(self, payload: Dict[str, Any]) -> Path:
        path = self.run_dir / "10-evaluation.json"
        self._atomic_json(path, payload)
        return path

    def write_manifest(self, state: ResearchState) -> Path:
        """Seal immutable research artifacts with size and SHA-256 metadata."""
        files: Dict[str, Dict[str, Any]] = {}
        raw_artifacts = _raw_artifacts_from_state(state)
        raw_files = _raw_files_on_disk(self.run_dir)
        if raw_files != set(raw_artifacts):
            unexpected = sorted(raw_files.symmetric_difference(raw_artifacts))
            raise ValueError(
                "raw source artifact set does not match state: " + ", ".join(unexpected)
            )
        for name in (*SEALED_ARTIFACTS, *sorted(raw_artifacts)):
            path = self.run_dir / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"manifest에 봉인할 artifact가 없습니다: {path}")
            raw = path.read_bytes()
            expected = raw_artifacts.get(name)
            if expected and (
                len(raw) != expected["bytes"]
                or hashlib.sha256(raw).hexdigest() != expected["sha256"]
            ):
                raise ValueError(f"raw source metadata가 실제 파일과 다릅니다: {path}")
            files[name] = {
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        payload = {
            "schema_version": 2,
            "run_id": state.run_id,
            "sealed_at": now_iso(),
            "files": files,
        }
        path = self.run_dir / "manifest.json"
        self._atomic_json(path, payload)
        return path

    def manifest_failures(self, state: ResearchState) -> List[str]:
        return verify_manifest(self.run_dir, expected_run_id=state.run_id)

    def append_event(self, event: str, payload: Dict[str, Any]) -> None:
        path = require_write(
            self.run_dir / "events.jsonl", self.job_id, self.cfg, context="research event"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": now_iso(), "event": event, **payload}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def lease(self) -> "ResearchRunLease":
        return ResearchRunLease(self.run_dir / ".run.lock", self.job_id, self.cfg)

    def _write_materialized_artifacts(self, state: ResearchState) -> None:
        if state.contract:
            self._atomic_json(self.run_dir / "01-contract.json", state.contract.__dict__)
        self._atomic_json(self.run_dir / "02-plan.json", {
            "depth": state.depth,
            "round": state.round,
            "policy": {
                "target_coverage": state.target_coverage,
                "initial_task_limit": state.initial_task_limit,
                "queries_per_task": state.queries_per_task,
                "max_results_per_search": state.max_results_per_search,
            },
            "tasks": [item.__dict__ for item in state.tasks],
            "budget": state.budget.__dict__,
        })
        self._atomic_jsonl(self.run_dir / "03-sources.jsonl", (item.__dict__ for item in state.sources))
        # Compatibility snapshot for earlier Build-up releases.
        self._atomic_json(self.run_dir / "00-sources.json", [item.__dict__ for item in state.sources])
        self._atomic_jsonl(self.run_dir / "04-evidence.jsonl", (item.__dict__ for item in state.evidence))
        self._atomic_jsonl(self.run_dir / "05-claims.jsonl", (item.__dict__ for item in state.claims))
        self._atomic_json(self.run_dir / "05-gaps.json", [item.__dict__ for item in state.gaps])
        if state.report:
            self._atomic_text(self.run_dir / "06-report.md", state.report.rstrip() + "\n")
        if state.citation_audit:
            self._atomic_json(self.run_dir / "08-citation-audit.json", state.citation_audit)
        if state.claim_audit:
            self._atomic_json(self.run_dir / "09-claim-audit.json", state.claim_audit)

    def _metadata(self, state: ResearchState) -> Dict[str, Any]:
        return {
            "schema_version": state.schema_version,
            "run_id": state.run_id,
            "job_id": state.job_id,
            "query": state.query,
            "depth": state.depth,
            "status": state.status,
            "phase": state.phase,
            "created_at": state.created_at,
            "updated_at": state.updated_at,
            "round": state.round,
            "stop_reason": state.stop_reason,
            "coverage": state.coverage,
            "target_coverage": state.target_coverage,
            "max_results_per_search": state.max_results_per_search,
            "engine": ", ".join(state.provider_names) or "unavailable",
            "providers": state.provider_names,
            "source_count": len(state.sources),
            "evidence_count": len(state.evidence),
            "claim_count": len(state.claims),
            "gap_count": len([item for item in state.gaps if item.status == "open"]),
            "model_calls": state.budget.model_calls_used,
            "writer_attempts": state.writer_attempts,
            "writer_corrections_used": state.writer_corrections_used,
            "search_requests": state.budget.searches_used,
            "budget": state.budget.__dict__,
            "citation_stats": state.citation_audit,
            "claim_audit": state.claim_audit,
            "session_id": state.session_id,
            "workspace_key": state.workspace_key,
            "errors": state.errors,
            "artifacts": {
                "contract": "01-contract.json",
                "plan": "02-plan.json",
                "sources": "03-sources.jsonl",
                "evidence": "04-evidence.jsonl",
                "claims": "05-claims.jsonl",
                "gaps": "05-gaps.json",
                "report": "06-report.md",
                "study_guide": "07-study-guide.md",
                "citation_audit": "08-citation-audit.json",
                "claim_audit": "09-claim-audit.json",
                "raw_sources": "raw-sources/",
                "checkpoint": STATE_FILE,
                "events": "events.jsonl",
                "manifest": "manifest.json",
            },
        }

    def _atomic_json(self, path: Path, payload: Any) -> None:
        self._atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    def _atomic_jsonl(self, path: Path, rows: Iterable[Dict[str, Any]]) -> None:
        text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
        self._atomic_text(path, text)

    def _atomic_text(self, path: Path, text: str) -> None:
        path = require_write(path, self.job_id, self.cfg, context="research artifact")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def _atomic_bytes(self, path: Path, raw: bytes) -> None:
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("research raw-source path cannot be a symlink")
        path = require_write(path, self.job_id, self.cfg, context="research raw source")
        if not path.is_relative_to(self.run_dir.resolve()):
            raise ValueError("research raw-source path escaped its run")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise


class ResearchRunLease(AbstractContextManager["ResearchRunLease"]):
    """Fail-fast process lease that prevents two writers resuming one run."""

    def __init__(self, path: Path, job_id: str, cfg: BuildupConfig):
        self.path = require_write(path, job_id, cfg, context="research lease")
        self.token = uuid.uuid4().hex
        self.acquired = False

    def __enter__(self) -> "ResearchRunLease":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                if self._owner_is_alive():
                    raise RuntimeError(f"이미 다른 프로세스가 이 research run을 실행 중입니다: {self.path.parent}")
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "token": self.token, "created_at": now_iso()}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            self.acquired = True
            return self
        raise RuntimeError(f"research run lease를 얻지 못했습니다: {self.path.parent}")

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

    def _owner_is_alive(self) -> bool:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            pid = int(payload.get("pid"))
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            # A very recent incomplete lock should not be stolen.
            try:
                return time.time() - self.path.stat().st_mtime < 30
            except OSError:
                return False


def _raw_artifacts_from_state(state: ResearchState) -> Dict[str, Dict[str, Any]]:
    artifacts: Dict[str, Dict[str, Any]] = {}
    for source in state.sources:
        metadata = source.metadata if isinstance(source.metadata, dict) else {}
        relative = str(metadata.get("raw_artifact") or "")
        if not relative:
            continue
        expected_relative = f"raw-sources/{source.id}.pdf"
        digest = str(metadata.get("raw_sha256") or "")
        try:
            size = int(metadata.get("raw_bytes"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid raw PDF size metadata: {source.id}") from exc
        if (
            relative != expected_relative
            or not _SOURCE_ID_RE.fullmatch(source.id)
            or not _SHA256_RE.fullmatch(digest)
            or size <= 0
        ):
            raise ValueError(f"invalid raw PDF provenance metadata: {source.id}")
        if relative in artifacts:
            raise ValueError(f"duplicate raw PDF artifact: {relative}")
        artifacts[relative] = {"sha256": digest, "bytes": size}
    return artifacts


def _raw_source_failures(state: ResearchState, run_dir: Path) -> List[str]:
    try:
        artifacts = _raw_artifacts_from_state(state)
    except ValueError as exc:
        return [str(exc)]
    failures: List[str] = [
        f"unexpected:{item}"
        for item in sorted(_raw_files_on_disk(run_dir).symmetric_difference(artifacts))
    ]
    root = run_dir.resolve()
    for relative, expected in artifacts.items():
        path = run_dir / relative
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(root)
        ):
            failures.append(relative)
            continue
        raw = path.read_bytes()
        if (
            len(raw) != expected["bytes"]
            or hashlib.sha256(raw).hexdigest() != expected["sha256"]
            or not raw.startswith(b"%PDF-")
        ):
            failures.append(relative)
    return failures


def _raw_files_on_disk(run_dir: Path) -> set[str]:
    directory = run_dir / "raw-sources"
    if not directory.exists():
        return set()
    if directory.is_symlink() or not directory.is_dir():
        return {"raw-sources:<unsafe-directory>"}
    root = run_dir.resolve()
    files: set[str] = set()
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            files.add(f"raw-sources:<unsafe>:{path.name}")
        else:
            files.add(str(path.relative_to(run_dir)))
    return files


def verify_manifest(run_dir: Path, *, expected_run_id: str = "") -> List[str]:
    """Verify the exact sealed artifact set without trusting manifest paths."""
    path = run_dir / "manifest.json"
    if not path.is_file():
        return ["manifest.json"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 2:
            return ["manifest.json:schema_version"]
        if expected_run_id and payload.get("run_id") != expected_run_id:
            return ["manifest.json:run_id"]
        files = payload.get("files")
        if not isinstance(files, dict):
            return ["manifest.json:files"]
        state_payload = json.loads((run_dir / STATE_FILE).read_text(encoding="utf-8"))
        if not isinstance(state_payload, dict):
            return ["state.json:raw-sources"]
        state = ResearchState.from_dict(state_payload)
        raw_artifacts = _raw_artifacts_from_state(state)
        expected_names = {*SEALED_ARTIFACTS, *raw_artifacts}
        actual_names = {str(name) for name in files}
        failures = [
            f"manifest.json:missing:{name}"
            for name in sorted(expected_names - actual_names)
        ]
        failures.extend(
            f"manifest.json:unexpected:{name}"
            for name in sorted(actual_names - expected_names)
        )
        raw_files = _raw_files_on_disk(run_dir)
        failures.extend(
            f"manifest.json:raw-source-set:{name}"
            for name in sorted(raw_files.symmetric_difference(raw_artifacts))
        )
        for name in sorted(expected_names):
            expected = files.get(name)
            target = run_dir / name
            if (
                target.is_symlink()
                or not target.is_file()
                or not target.resolve().is_relative_to(run_dir.resolve())
                or not isinstance(expected, dict)
            ):
                failures.append(name)
                continue
            raw = target.read_bytes()
            if (
                int(expected.get("bytes") or -1) != len(raw)
                or str(expected.get("sha256") or "")
                != hashlib.sha256(raw).hexdigest()
            ):
                failures.append(name)
            raw_expected = raw_artifacts.get(name)
            if raw_expected and (
                len(raw) != raw_expected["bytes"]
                or hashlib.sha256(raw).hexdigest() != raw_expected["sha256"]
                or not raw.startswith(b"%PDF-")
            ):
                failures.append(f"{name}:source-metadata")
        return failures
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return ["manifest.json"]


def validate_checkpoint_state(state: ResearchState) -> None:
    """Fail closed when a resumable checkpoint violates ledger invariants."""
    failures = checkpoint_invariant_failures(state)
    if failures:
        raise ValueError(
            "research checkpoint invariant validation failed: "
            + ", ".join(failures)
        )


def checkpoint_invariant_failures(state: ResearchState) -> List[str]:
    """Return deterministic structural failures for resume and offline eval."""
    failures: List[str] = []
    if not state.run_id or not state.job_id or not state.query.strip():
        failures.append("identity/query")
    if not math.isfinite(state.coverage) or not 0.0 <= state.coverage <= 1.0:
        failures.append("coverage")
    if not math.isfinite(state.target_coverage) or not 0.0 <= state.target_coverage <= 1.0:
        failures.append("target_coverage")
    if len(state.tasks) > state.budget.max_tasks:
        failures.append("task budget")
    if len(state.sources) > state.budget.max_sources:
        failures.append("source budget")
    if state.writer_corrections_used > state.writer_attempts:
        failures.append("writer counters")

    task_ids = _unique_ids("task", (item.id for item in state.tasks), failures)
    source_ids = _unique_ids("source", (item.id for item in state.sources), failures)
    evidence_ids = _unique_ids("evidence", (item.id for item in state.evidence), failures)
    _unique_ids("claim", (item.id for item in state.claims), failures)
    _unique_ids("gap", (item.id for item in state.gaps), failures)

    source_by_id = {item.id: item for item in state.sources}
    evidence_by_id = {item.id: item for item in state.evidence}
    for source in state.sources:
        if not source.id or not source.normalized_url or not source.content:
            failures.append(f"source:{source.id or '<empty>'}:required")
        if source.id != stable_id("S", source.normalized_url):
            failures.append(f"source:{source.id or '<empty>'}:stable-id")
        if source.content_sha256 != hashlib.sha256(
            source.content.encode("utf-8")
        ).hexdigest():
            failures.append(f"source:{source.id or '<empty>'}:sha256")

    for evidence in state.evidence:
        source = source_by_id.get(evidence.source_id)
        if evidence.task_id not in task_ids:
            failures.append(f"evidence:{evidence.id}:task")
        if source is None:
            failures.append(f"evidence:{evidence.id}:source")
            continue
        exact = verify_exact_passage(source.content, evidence.passage)
        if (
            not evidence.verified_exact
            or not exact.valid
            or evidence.passage_sha256 != passage_sha256(evidence.passage)
        ):
            failures.append(f"evidence:{evidence.id}:exact-passage")
        if exact.valid and evidence.id != stable_id(
            "E", evidence.source_id, exact.normalized_passage, length=14
        ):
            failures.append(f"evidence:{evidence.id}:stable-id")

    for task in state.tasks:
        if any(item not in source_ids for item in task.source_ids):
            failures.append(f"task:{task.id}:source-reference")
        if any(item not in evidence_ids for item in task.evidence_ids):
            failures.append(f"task:{task.id}:evidence-reference")
        if task.status == TaskStatus.COMPLETED.value and not task.evidence_ids:
            failures.append(f"task:{task.id}:completed-without-evidence")

    for claim in state.claims:
        if claim.id != stable_id("C", claim.text, length=14):
            failures.append(f"claim:{claim.id}:stable-id")
        supporting = [evidence_by_id.get(item) for item in claim.evidence_ids]
        contradicting = [
            evidence_by_id.get(item) for item in claim.contradicting_evidence_ids
        ]
        if any(item is None or item.stance != "supports" for item in supporting):
            failures.append(f"claim:{claim.id}:support-reference")
        if any(item is None or item.stance != "contradicts" for item in contradicting):
            failures.append(f"claim:{claim.id}:contradiction-reference")
        if claim.status == "unsupported" and claim.evidence_ids:
            failures.append(f"claim:{claim.id}:unsupported-with-evidence")
        if claim.status in {"supported", "contested"} and not claim.evidence_ids:
            failures.append(f"claim:{claim.id}:supported-without-evidence")
        if claim.status == "contested" and not claim.contradicting_evidence_ids:
            failures.append(f"claim:{claim.id}:contested-without-contradiction")

    # Unknown used-claim IDs are a draft quality failure, not structural
    # corruption: the bounded writer correction must remain able to repair it.
    return list(dict.fromkeys(failures))


def _unique_ids(name: str, values: Iterable[str], failures: List[str]) -> set[str]:
    rows = list(values)
    if any(not item for item in rows):
        failures.append(f"{name}:empty-id")
    if len(rows) != len(set(rows)):
        failures.append(f"{name}:duplicate-id")
    return set(rows)
