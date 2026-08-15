#!/usr/bin/env python3
"""
Hermes Virtual Bridge

Single callable workflow entrypoint for invoking Armadillo tasks from Hermes,
including phone-triggered flows from the Virtuals app.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Tuple

WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace"))
if not WORKSPACE.exists():
    WORKSPACE = Path.cwd()

JOBS_FILE = WORKSPACE / "hermes_virtual_jobs.json"
CAPABILITIES_FILE = WORKSPACE / "hermes_virtual_capabilities.json"

TARGET_MOBILE_UI = "virtual_mobile_ui"
TARGET_BACKEND = "hermes_backend_trigger"
VALID_TARGETS = {TARGET_MOBILE_UI, TARGET_BACKEND}

DEFAULT_TIMEOUT = int(os.environ.get("HERMES_VIRTUAL_TIMEOUT_SEC", "120"))

# Stable external action names (do not expose raw script names to callers)
ACTION_MAP = {
    "health_check": None,
    "watchdog_sync": "cron_watchdog.py",
    "treasury_cycle": "treasury_engine.py",
    "volume_cycle": "volume_engine.py",
    "learning_cycle": "learning_engine.py",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open() as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(data, f, indent=2)


def short_response(data: Dict[str, Any]) -> Dict[str, Any]:
    """Keep response compact for phone UI."""
    max_text = 500
    compact = dict(data)
    for key in ("stdout", "stderr", "message"):
        value = compact.get(key)
        if isinstance(value, str) and len(value) > max_text:
            compact[key] = value[:max_text] + "..."
    return compact


class HermesVirtualBridge:
    def __init__(self) -> None:
        self.jobs = load_json(JOBS_FILE, {"jobs": {}, "idempotency": {}})
        self.capabilities = load_json(
            CAPABILITIES_FILE,
            {
                "active_version": None,
                "fallback_version": None,
                "versions": {},
                "updated_at": now_iso(),
            },
        )

    def _save_jobs(self) -> None:
        save_json(JOBS_FILE, self.jobs)

    def _save_capabilities(self) -> None:
        self.capabilities["updated_at"] = now_iso()
        save_json(CAPABILITIES_FILE, self.capabilities)

    def _validate_runtime(self) -> Tuple[bool, str]:
        required = ["HERMES_VIRTUAL_RPC_URL"]
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            return False, f"Missing required environment variable(s): {', '.join(missing)}"
        return True, "ok"

    def _resolve_target(self, requested_target: str) -> str:
        target = requested_target or os.environ.get("HERMES_VIRTUAL_TARGET", "").strip()
        if not target:
            raise ValueError(
                "Target confirmation required. Set --target to "
                f"'{TARGET_MOBILE_UI}' or '{TARGET_BACKEND}'."
            )
        if target not in VALID_TARGETS:
            raise ValueError(
                f"Invalid target '{target}'. Valid targets: {', '.join(sorted(VALID_TARGETS))}."
            )
        return target

    def _compute_idempotency_key(self, target: str, action: str, payload: Dict[str, Any]) -> str:
        digest = hashlib.sha256(
            json.dumps({"target": target, "action": action, "payload": payload}, sort_keys=True).encode()
        ).hexdigest()
        return digest

    def _create_job(self, action: str, target: str, payload: Dict[str, Any]) -> str:
        job_id = str(uuid.uuid4())
        self.jobs["jobs"][job_id] = {
            "job_id": job_id,
            "status": "queued",
            "action": action,
            "target": target,
            "payload": payload,
            "result": None,
            "error": None,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        self._save_jobs()
        return job_id

    def _update_job(self, job_id: str, **fields: Any) -> None:
        if job_id not in self.jobs["jobs"]:
            return
        self.jobs["jobs"][job_id].update(fields)
        self.jobs["jobs"][job_id]["updated_at"] = now_iso()
        self._save_jobs()

    def _execute_action(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if action not in ACTION_MAP:
            raise ValueError(f"Unsupported action '{action}'.")

        if action == "health_check":
            return {
                "ok": True,
                "message": "Hermes bridge is ready",
                "workspace": str(WORKSPACE),
                "version": self.capabilities.get("active_version") or "unregistered",
            }

        script_name = ACTION_MAP[action]
        script_path = Path.cwd() / script_name
        if not script_path.exists():
            raise FileNotFoundError(f"Script not found: {script_path}")

        timeout = int(payload.get("timeout_sec") or DEFAULT_TIMEOUT)
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=max(10, min(timeout, 900)),
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }

    def invoke(
        self,
        action: str,
        payload: Dict[str, Any],
        target: str,
        request_id: str,
        async_mode: bool,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        validated_target = self._resolve_target(target)

        runtime_ok, runtime_msg = self._validate_runtime()
        if not runtime_ok:
            return {"ok": False, "status": "error", "error": runtime_msg, "request_id": request_id}

        payload = payload or {}
        if not isinstance(payload, dict):
            return {"ok": False, "status": "error", "error": "Payload must be a JSON object.", "request_id": request_id}

        idem_key = idempotency_key or self._compute_idempotency_key(validated_target, action, payload)
        existing_job_id = self.jobs["idempotency"].get(idem_key)
        if existing_job_id and existing_job_id in self.jobs["jobs"]:
            prior = self.jobs["jobs"][existing_job_id]
            return short_response(
                {
                    "ok": True,
                    "status": "deduplicated",
                    "request_id": request_id,
                    "job_id": existing_job_id,
                    "job_status": prior["status"],
                    "result": prior.get("result"),
                    "error": prior.get("error"),
                }
            )

        job_id = self._create_job(action=action, target=validated_target, payload=payload)
        self.jobs["idempotency"][idem_key] = job_id
        self._save_jobs()

        if async_mode:
            subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "_run-job",
                    "--job-id",
                    job_id,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return {
                "ok": True,
                "status": "accepted",
                "request_id": request_id,
                "job_id": job_id,
                "poll_with": f"python {Path(__file__).name} status --job-id {job_id}",
            }

        self.run_job(job_id)
        record = self.jobs["jobs"][job_id]
        return short_response(
            {
                "ok": record["status"] == "completed",
                "status": record["status"],
                "request_id": request_id,
                "job_id": job_id,
                "result": record.get("result"),
                "error": record.get("error"),
            }
        )

    def run_job(self, job_id: str) -> None:
        job = self.jobs["jobs"].get(job_id)
        if not job:
            return

        self._update_job(job_id, status="running", error=None)
        try:
            result = self._execute_action(job["action"], job.get("payload") or {})
            if result.get("ok"):
                self._update_job(job_id, status="completed", result=short_response(result), error=None)
            else:
                self._update_job(job_id, status="failed", result=short_response(result), error=result.get("stderr") or "Action failed")
        except Exception as exc:
            self._update_job(job_id, status="failed", error=str(exc), result=None)

    def status(self, job_id: str) -> Dict[str, Any]:
        job = self.jobs["jobs"].get(job_id)
        if not job:
            return {"ok": False, "error": f"job_id not found: {job_id}"}
        return short_response({"ok": True, **job})

    def register_capability(self, name: str, version: str, activate: bool) -> Dict[str, Any]:
        version_data = {
            "name": name,
            "version": version,
            "actions": sorted(ACTION_MAP.keys()),
            "targets": sorted(VALID_TARGETS),
            "registered_at": now_iso(),
        }
        self.capabilities["versions"][version] = version_data

        if activate:
            current = self.capabilities.get("active_version")
            if current and current != version:
                self.capabilities["fallback_version"] = current
            self.capabilities["active_version"] = version

        self._save_capabilities()
        return {"ok": True, "active_version": self.capabilities.get("active_version"), "fallback_version": self.capabilities.get("fallback_version")}

    def activate_version(self, version: str) -> Dict[str, Any]:
        if version not in self.capabilities.get("versions", {}):
            return {"ok": False, "error": f"Unknown version: {version}"}
        previous = self.capabilities.get("active_version")
        if previous and previous != version:
            self.capabilities["fallback_version"] = previous
        self.capabilities["active_version"] = version
        self._save_capabilities()
        return {"ok": True, "active_version": version, "fallback_version": self.capabilities.get("fallback_version")}

    def list_capabilities(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "active_version": self.capabilities.get("active_version"),
            "fallback_version": self.capabilities.get("fallback_version"),
            "versions": self.capabilities.get("versions", {}),
        }


def parse_payload(payload_raw: str) -> Dict[str, Any]:
    if not payload_raw:
        return {}
    parsed = json.loads(payload_raw)
    if not isinstance(parsed, dict):
        raise ValueError("payload must be a JSON object")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes Virtual bridge entrypoint")
    sub = parser.add_subparsers(dest="command", required=True)

    invoke = sub.add_parser("invoke", help="Invoke a bridge action")
    invoke.add_argument("--action", required=True, choices=sorted(ACTION_MAP.keys()))
    invoke.add_argument("--payload", default="{}", help="JSON object payload")
    invoke.add_argument("--target", help=f"Required target: {TARGET_MOBILE_UI} or {TARGET_BACKEND}")
    invoke.add_argument("--request-id", default="")
    invoke.add_argument("--idempotency-key", default="")
    invoke.add_argument("--async", dest="async_mode", action="store_true")

    status = sub.add_parser("status", help="Get async job status")
    status.add_argument("--job-id", required=True)

    runner = sub.add_parser("_run-job", help=argparse.SUPPRESS)
    runner.add_argument("--job-id", required=True)

    register = sub.add_parser("register-capability", help="Register a capability version")
    register.add_argument("--name", default="armadillo-hermes-mobile-bridge")
    register.add_argument("--version", required=True)
    register.add_argument("--activate", action="store_true")

    activate = sub.add_parser("activate-version", help="Activate or rollback capability version")
    activate.add_argument("--version", required=True)

    sub.add_parser("list-capabilities", help="List capability versions")

    args = parser.parse_args()
    bridge = HermesVirtualBridge()

    if args.command == "invoke":
        try:
            payload = parse_payload(args.payload)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 2

        req_id = args.request_id or str(uuid.uuid4())
        try:
            resp = bridge.invoke(
                action=args.action,
                payload=payload,
                target=args.target,
                request_id=req_id,
                async_mode=args.async_mode,
                idempotency_key=args.idempotency_key,
            )
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc), "request_id": req_id}, indent=2))
            return 1
        print(json.dumps(short_response(resp), indent=2))
        return 0 if resp.get("ok") else 1

    if args.command == "status":
        resp = bridge.status(args.job_id)
        print(json.dumps(short_response(resp), indent=2))
        return 0 if resp.get("ok") else 1

    if args.command == "_run-job":
        bridge.run_job(args.job_id)
        return 0

    if args.command == "register-capability":
        resp = bridge.register_capability(name=args.name, version=args.version, activate=args.activate)
        print(json.dumps(resp, indent=2))
        return 0 if resp.get("ok") else 1

    if args.command == "activate-version":
        resp = bridge.activate_version(args.version)
        print(json.dumps(resp, indent=2))
        return 0 if resp.get("ok") else 1

    if args.command == "list-capabilities":
        print(json.dumps(bridge.list_capabilities(), indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
