"""Shared local DB/API/PDF/browser adapters for per-paper coordinators.

Credentials remain inside the API adapter; no session token is returned or
written to artifacts. CLI entrypoints only read, and output paths stay in-project.
"""
from __future__ import annotations

import json
import subprocess
import time
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import UUID

from app.utils.project_paths import PROJECT_ROOT


class AdapterError(RuntimeError):
    pass


class ReadonlyDatabase:
    def __init__(self, database_url):
        from sqlalchemy import create_engine
        self._engine = create_engine(database_url, future=True)

    def paper_package(self, paper_id):
        from sqlalchemy import text
        from sqlalchemy.orm import Session
        from app.services.rebuild_workflow_service import get_paper, list_files, list_assets, list_rows
        paper_id = UUID(str(paper_id))
        with self._engine.connect() as connection, connection.begin():
            connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            with Session(bind=connection, autoflush=False) as session:
                rows, offset, total = [], 0, None
                while total is None or offset < total:
                    page = list_rows(session, paper_id=paper_id, offset=offset, limit=500)
                    total = page["total"]
                    rows.extend(page["items"])
                    if not page["has_more"]:
                        break
                    offset = page["next_offset"]
                return {"paper": get_paper(session, paper_id), "files": list_files(session, paper_id),
                        "assets": list_assets(session, paper_id), "rows": {"items": rows, "total": total, "has_more": False}}

    def close(self):
        self._engine.dispose()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _Cookies:
    def __init__(self, jar):
        self.jar = jar

    def get(self, name, default=None):
        return next((cookie.value for cookie in self.jar if cookie.name == name), default)


class _Response:
    def __init__(self, status, body, headers):
        self.status_code, self.body, self.headers = status, body, headers

    def json(self):
        return json.loads(self.body)


class _LocalHTTPClient:
    """stdlib client makes host API/browser tools usable without host DB packages."""
    def __init__(self, base_url):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise AdapterError("workbench_base_url_invalid")
        self.base_url = base_url.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.cookies = _Cookies(self.jar)
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect())

    def request(self, method, path, *, json=None, headers=None):
        body = None
        outbound = dict(headers or {})
        if json is not None:
            import json as encoder
            body = encoder.dumps(json, ensure_ascii=False, allow_nan=False).encode()
            outbound["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=body, headers=outbound, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                return _Response(response.status, response.read(), response.headers)
        except urllib.error.HTTPError as response:
            try:
                return _Response(response.code, response.read(), response.headers)
            finally:
                response.close()

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, *, json):
        return self.request("POST", path, json=json)

    def close(self):
        self.opener.close()


class WorkbenchAPI:
    def __init__(self, base_url, *, client=None):
        self._client = client or _LocalHTTPClient(base_url)

    @classmethod
    def from_local_credentials(cls, credentials_file=None):
        path = Path(credentials_file or PROJECT_ROOT / ".env.workbench-login.local")
        data = json.loads(path.read_text())
        adapter = cls(data["base_url"])
        try:
            response = adapter._client.post("/api/auth/login", json={"username": data["username"], "password": data["password"]})
            if response.status_code != 200:
                raise AdapterError("workbench_login_failed_use_existing_auth_configuration")
            return adapter
        except Exception:
            adapter.close()
            raise AdapterError("workbench_login_unconfirmed_no_credential_output") from None

    @staticmethod
    def _relative_api(path):
        if not isinstance(path, str) or not path.startswith("/api/") or "\\" in path or "\r" in path or "\n" in path:
            raise AdapterError("same_origin_api_path_required")
        return path

    def get(self, path):
        path = self._relative_api(path)
        for attempt in range(4):
            try:
                response = self._client.get(path)
            except Exception:
                raise AdapterError("authenticated_api_read_transport_failed") from None
            if response.status_code != 429 or attempt == 3:
                break
            from app.services.rebuild_progress_guard import retry_guidance
            guidance = retry_guidance(429, attempt=attempt, retry_after=response.headers.get("Retry-After"))
            time.sleep(min(60, guidance["delay_seconds"]))
        if response.status_code != 200:
            raise AdapterError(f"authenticated_api_read_failed:{response.status_code}")
        return response.json()

    def work_package(self, paper_id):
        paper_id = str(UUID(str(paper_id)))
        path = f"/api/rebuild/papers/{paper_id}/ai-work-package"
        all_rows, seen, expected_revision, total, package = [], set(), None, None, None
        while path:
            page = self.get(path)
            revision = page.get("output_revision")
            if page.get("paper", {}).get("id") != paper_id or not revision:
                raise AdapterError("work_package_identity_or_revision_missing")
            if expected_revision is not None and revision != expected_revision:
                raise AdapterError("work_package_changed_restart_read_only_pagination")
            expected_revision = revision
            rows = page["rows"]
            if total is not None and total != rows["total"]:
                raise AdapterError("work_package_total_changed")
            total = rows["total"]
            if rows.get("offset") != len(all_rows) or rows.get("has_more") and not rows["items"]:
                raise AdapterError("work_package_pagination_gap")
            for row in rows["items"]:
                if row.get("paper_id") != paper_id or row["id"] in seen:
                    raise AdapterError("work_package_duplicate_or_foreign_row")
                seen.add(row["id"]); all_rows.append(row)
            next_page = rows.get("continuation_page") or rows.get("next_page")
            if bool(next_page) != bool(rows.get("has_more")):
                raise AdapterError("work_package_pagination_contract_invalid")
            if next_page and not next_page.startswith(f"/api/rebuild/papers/{paper_id}/ai-work-package?"):
                raise AdapterError("work_package_next_page_outside_paper")
            if package is None:
                package = page  # Row-only continuations must not discard initial context.
            path = next_page
        if len(all_rows) != total:
            raise AdapterError("work_package_truncated")
        return {**package, "rows": {"total": total, "items": all_rows, "has_more": False, "next_page": None}}

    def mutate(self, path, body, *, method="POST", request_id, task_id=None, reviewer_authorization=None):
        # Persist caller intent before calling this. No automatic mutation retries.
        headers = {"X-Request-ID": str(request_id), "X-CSRF-Token": self._client.cookies.get("litai_csrf", "")}
        if task_id:
            headers["X-Task-ID"] = task_id
        if reviewer_authorization:
            headers["X-Reviewer-Authorization"] = reviewer_authorization
        try:
            response = self._client.request(method, self._relative_api(path), json=body, headers=headers)
        except Exception:
            raise AdapterError("write_outcome_unknown_read_receipt_before_retry_same_request_id") from None
        if response.status_code >= 500:
            raise AdapterError("write_outcome_unknown_read_receipt_before_retry_same_request_id")
        if response.status_code >= 400:
            raise AdapterError(f"write_rejected:{response.status_code}")
        return response.json()

    def close(self):
        self._client.close()


class BrowserAdapter:
    CONTROLLER = "/home/2401liyuhao/apps/server-browser-control/browser.mjs"

    @classmethod
    def inspect(cls, command="status"):
        if command not in {"status", "text", "frames"}:
            raise AdapterError("browser_read_only_command_required")
        try:
            result = subprocess.run(["node", cls.CONTROLLER, command], capture_output=True, text=True, timeout=15, check=False)
            if result.returncode:
                raise AdapterError("existing_browser_controller_unavailable_no_new_session_started")
            return json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            raise AdapterError("browser_inspection_failed") from None


def write_project_output(path, value):
    destination = Path(path).resolve()
    root = (PROJECT_ROOT / "outputs").resolve()
    if not destination.is_relative_to(root) or destination == root:
        raise AdapterError("output_must_be_under_project_outputs")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Create only: no accidental replacement of an earlier report or fixture.
    with destination.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
