"""
Remote HTTP REST API + MCP Streamable HTTP Server for Deep Survey (大调查).

Enables deploying Deep Survey on a VPS (or exposing via Tailscale / Cloudflare Tunnel)
so remote Agents, mobile workflows, or web apps can invoke multi-source research via:
  1. MCP Streamable HTTP (`POST /mcp` — JSON-RPC 2.0 `initialize`, `tools/list`, `tools/call`)
  2. Clean REST API:
     - `GET  /health`
     - `GET  /v1/doctor`
     - `POST /v1/survey`          (sync wait or async job creation)
     - `GET  /v1/survey/<run_id>` (poll status & fetch report markdown)

Security:
  - Enforces Bearer token authentication by default whenever bound to a non-loopback address.
  - Token read from `--token`, `DEEP_SURVEY_TOKEN` env var, or `~/.config/deep-survey/server-token`.
"""
from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import parse_qs, urlparse
import uuid

from .mcp_server import SERVER_INFO, TOOLS, handle_tool_call
from .survey import (
    RUNS_DIR,
    SurveyError,
    collect_doctor_data,
    local_stamp,
    redact_secrets,
    resolve_channels_for_mode,
    slugify_topic,
    spawn_detached_worker,
    wait_for_run,
)

TOKEN_FILE = Path.home() / ".config/deep-survey/server-token"


def load_or_init_token(explicit_token: str | None, generate_if_missing: bool = False) -> str | None:
    if explicit_token and explicit_token.strip():
        return explicit_token.strip()
    env_tok = os.environ.get("DEEP_SURVEY_TOKEN")
    if env_tok and env_tok.strip():
        return env_tok.strip()
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    if generate_if_missing:
        TOKEN_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tok = "ds_" + secrets.token_urlsafe(32)
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tok + "\n")
        return tok
    return None


def make_handler(expected_token: str | None):
    class SurveyHTTPHandler(BaseHTTPRequestHandler):
        server_version = "DeepSurvey/1.1"

        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write(f"[deep-survey-http] {self.address_string()} - {fmt % args}\n")

        def _send_json(self, code: int, data: dict) -> None:
            raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _check_auth(self) -> bool:
            if not expected_token:
                return True
            auth = self.headers.get("Authorization") or ""
            if auth.startswith("Bearer "):
                provided = auth[7:].strip()
                if hmac.compare_digest(provided, expected_token):
                    return True
            self._send_json(401, {"error": "Unauthorized: missing or invalid Bearer token"})
            return False

        def _read_json_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > 2 * 1024 * 1024:
                raise SurveyError("请求体超过 2 MiB 上限")
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path == "/health":
                self._send_json(200, {"ok": True, "service": SERVER_INFO})
                return

            if not self._check_auth():
                return

            if path == "/v1/doctor":
                self._send_json(200, collect_doctor_data())
                return

            if path.startswith("/v1/survey/"):
                run_id = path.split("/v1/survey/", 1)[1].strip("/")
                qs = parse_qs(parsed.query)
                wait_sec = max(0, min(120, int((qs.get("wait") or ["0"])[0])))
                RUNS_DIR.mkdir(parents=True, exist_ok=True)
                if run_id == "latest":
                    dirs = sorted([d for d in RUNS_DIR.iterdir() if (d / "state.json").exists()], reverse=True)
                    if not dirs:
                        self._send_json(404, {"error": "暂无调研任务"})
                        return
                    run_dir = dirs[0]
                else:
                    if "/" in run_id or ".." in run_id:
                        self._send_json(400, {"error": "无效 run_id"})
                        return
                    run_dir = RUNS_DIR / run_id
                    if not run_dir.exists():
                        self._send_json(404, {"error": f"未找到任务: {run_id}"})
                        return
                state = wait_for_run(run_dir, wait_sec)
                resp = dict(state)
                if state.get("status") == "completed":
                    rf = Path(state.get("report_file") or (run_dir / "report.md"))
                    if rf.exists():
                        resp["report_markdown"] = rf.read_text(encoding="utf-8")
                self._send_json(200, resp)
                return

            self._send_json(404, {"error": f"Not found: {path}"})

        def do_POST(self) -> None:
            if not self._check_auth():
                return
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            try:
                body = self._read_json_body()
            except Exception as exc:
                self._send_json(400, {"error": f"Invalid JSON: {redact_secrets(exc)}"})
                return

            # 1. MCP Streamable HTTP endpoint (JSON-RPC 2.0 over HTTP POST)
            if path == "/mcp":
                msg_id = body.get("id")
                method = body.get("method", "")
                params = body.get("params") or {}
                if msg_id is None:
                    self.send_response(202)
                    self.end_headers()
                    return
                try:
                    if method == "initialize":
                        result = {
                            "protocolVersion": params.get("protocolVersion") or "2025-03-26",
                            "capabilities": {"tools": {}},
                            "serverInfo": SERVER_INFO,
                        }
                    elif method == "ping":
                        result = {}
                    elif method == "tools/list":
                        result = {"tools": TOOLS}
                    elif method == "tools/call":
                        result = handle_tool_call(params.get("name", ""), params.get("arguments") or {})
                    elif method in {"resources/list", "prompts/list"}:
                        key = "resources" if "resources" in method else "prompts"
                        result = {key: []}
                    else:
                        self._send_json(200, {
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "error": {"code": -32601, "message": f"Method not found: {method}"},
                        })
                        return
                    self._send_json(200, {"jsonrpc": "2.0", "id": msg_id, "result": result})
                except Exception as exc:
                    self._send_json(200, {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {"content": [{"type": "text", "text": f"错误：{redact_secrets(exc)}"}], "isError": True},
                    })
                return

            # 2. REST API endpoint
            if path == "/v1/survey":
                query = (body.get("query") or "").strip()
                if not query:
                    self._send_json(400, {"error": "query 不能为空"})
                    return
                mode = body.get("mode") if body.get("mode") in {"fast", "deep", "x", "github", "hn"} else "fast"
                wait_sec = max(0, min(300, int(body.get("wait_seconds", 120))))
                channels = resolve_channels_for_mode(mode, body.get("channels"))
                run_id = f"{local_stamp()}-{slugify_topic(query)}-{uuid.uuid4().hex[:6]}"
                run_dir = RUNS_DIR / run_id
                run_dir.mkdir(parents=True, exist_ok=True)
                spawn_detached_worker(
                    run_id,
                    run_dir,
                    query,
                    mode=mode,
                    channels=channels,
                    include_oai_api=bool(body.get("include_oai_api", False)),
                    out_report=None,
                    timeout_per_channel=int(body["timeout"]) if body.get("timeout") is not None else None,
                )
                state = wait_for_run(run_dir, wait_sec)
                resp = dict(state)
                if state.get("status") == "completed":
                    rf = Path(state.get("report_file") or (run_dir / "report.md"))
                    if rf.exists():
                        resp["report_markdown"] = rf.read_text(encoding="utf-8")
                self._send_json(200 if state.get("status") == "completed" else 202, resp)
                return

            self._send_json(404, {"error": f"Not found: {path}"})

    return SurveyHTTPHandler


def serve_http(host: str, port: int, *, token: str | None = None, allow_unauthenticated: bool = False) -> int:
    is_loopback = host in {"127.0.0.1", "localhost", "::1"}
    resolved_token = load_or_init_token(token, generate_if_missing=(not is_loopback and not allow_unauthenticated))
    if not is_loopback and not allow_unauthenticated and not resolved_token:
        raise SurveyError("非本地回环地址必须启用 Bearer 鉴权（请通过 --token 或 DEEP_SURVEY_TOKEN 配置）")

    server = ThreadingHTTPServer((host, port), make_handler(None if allow_unauthenticated else resolved_token))
    info = {
        "listening": f"http://{host}:{port}",
        "mcp_streamable_http_endpoint": f"http://{host}:{port}/mcp",
        "rest_survey_endpoint": f"http://{host}:{port}/v1/survey",
        "auth_required": bool(resolved_token and not allow_unauthenticated),
        "token_file": str(TOKEN_FILE) if (resolved_token and TOKEN_FILE.exists()) else None,
    }
    print(json.dumps(info, ensure_ascii=False, indent=2), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
    return 0
