#!/usr/bin/env python3
"""
Zero-dependency stdio MCP Server for Deep Survey (大调查).
Supports both newline-delimited JSON-RPC 2.0 and Content-Length framed messages.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any
import uuid

from .survey import (
    RUNS_DIR,
    SurveyError,
    local_stamp,
    redact_secrets,
    resolve_channels_for_mode,
    slugify_topic,
    spawn_detached_worker,
    wait_for_run,
)

SERVER_INFO = {
    "name": "deep-survey",
    "version": "1.1.0",
}

TOOLS = [
    {
        "name": "deep_survey",
        "description": (
            "大调查（Deep Survey）多源官方原生检索与深度调研工具（替代浅层单次网页搜索）。并发调用已登录订阅的 "
            "Codex（OpenAI 服务端多跳搜索）、Grok（原生 X/Twitter 推文与讨论串搜索）、Antigravity（Google Search Grounding）、"
            "GitHub 官方索引（含 Star 热度、停更归档与低星玩具过滤校准）及 Hacker News 官方高赞讨论，在后台隔离原始长文并"
            "只返回交叉核验后的高信噪比中文报告。适用于技术选型、开源轮子摸底、新领域前置调研、社区真实口碑与踩坑核查。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要深入调研的问题、技术方向、产品对比或待核实的论点（尽量包含背景与关注维度）。",
                },
                "mode": {
                    "type": "string",
                    "enum": ["fast", "deep"],
                    "default": "fast",
                    "description": "fast：五路并发一阶段检索+仓库真实性审计+汇总；deep：五路并发+二阶段仓库核验与X口碑追查（若超时自动返回 run_id 供 deep_survey_status 续查）。",
                },
                "channels": {
                    "type": "string",
                    "description": "可选：逗号分隔的自定义通道子集（默认 codex,grok,agy,github,hn；亦支持已配 Key 的 xai_api,gemini_api,perplexity,tavily,exa,brave）。",
                },
                "wait_seconds": {
                    "type": "integer",
                    "default": 95,
                    "description": "本次调用最多等待秒数（建议 60-110s 以免客户端超时；若未完成可用 deep_survey_status 继续获取结果）。",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "ask_grok_x",
        "description": (
            "专门调用 Grok 服务端原生 X (Twitter) 搜索能力，检索实时推文、开发者讨论串、最新舆情、真实吐槽与一线动态。"
            "返回带具体 x.com 帖子链接的中文摘要。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要在 X (Twitter) 和实时社区中检索的主题、关键词、项目名或事件。",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "survey_github_ecosystem",
        "description": (
            "调用已鉴权的 GitHub 官方索引检索相关开源仓库与高讨论度 Issues，并基于真实 Star 数、创建时间、最近推送日期与归档状态进行热度与健康度分级"
            "（区分主流成熟方案、近期高潜新秀、停更老项目与极冷门未验证玩具）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要摸底的技术方向、功能关键词或具体 GitHub 仓库名。",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "deep_survey_status",
        "description": "查询仍在后台运行的 deep_survey 调研任务进度或读取已完成的汇总报告。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "之前返回的 run_id，或传 'latest' 查询最近一次任务。",
                },
                "wait_seconds": {
                    "type": "integer",
                    "default": 60,
                    "description": "若任务仍在运行，最多等待秒数（默认 60）。",
                },
            },
            "required": ["run_id"],
        },
    },
]


def _format_state_for_mcp(state: dict) -> dict[str, Any]:
    status = state.get("status")
    run_dir = Path(state.get("run_dir", ""))
    if status == "completed":
        rf = Path(state.get("report_file") or (run_dir / "report.md"))
        text = rf.read_text(encoding="utf-8") if rf.exists() else json.dumps(state, ensure_ascii=False, indent=2)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    if status == "failed":
        return {
            "content": [{"type": "text", "text": f"调研失败：{json.dumps(state, ensure_ascii=False, indent=2)}"}],
            "isError": True,
        }
    return {
        "content": [{
            "type": "text",
            "text": (
                f"调研任务仍在后台运行中（不会丢失进度）。\n"
                f"- `run_id`: `{state.get('run_id')}`\n"
                f"- 当前阶段: `{state.get('phase')}`\n"
                f"- 已完成通道: `{json.dumps(state.get('channels_completed', {}), ensure_ascii=False)}`\n\n"
                f"请立即再次调用 `deep_survey_status(run_id=\"{state.get('run_id')}\", wait_seconds=60)` 获取最终汇总报告。"
            ),
        }],
        "isError": False,
    }


def handle_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name in {"deep_survey", "ask_grok_x", "survey_github_ecosystem"}:
        query = (arguments.get("query") or "").strip()
        if not query:
            raise SurveyError("参数 query 不能为空。")
        if name == "ask_grok_x":
            mode = "x"
            wait_sec = int(arguments.get("wait_seconds") or 90)
            channels = ["grok"]
        elif name == "survey_github_ecosystem":
            mode = "github"
            wait_sec = int(arguments.get("wait_seconds") or 45)
            channels = ["github"]
        else:
            mode = arguments.get("mode") if arguments.get("mode") in {"fast", "deep"} else "fast"
            wait_sec = max(15, min(115, int(arguments.get("wait_seconds") or 95)))
            channels = resolve_channels_for_mode(mode, arguments.get("channels"))

        run_id = f"{local_stamp()}-{slugify_topic(query)}-{uuid.uuid4().hex[:6]}"
        run_dir = RUNS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        spawn_detached_worker(
            run_id,
            run_dir,
            query,
            mode=mode,
            channels=channels,
            include_oai_api=False,
            out_report=None,
        )
        state = wait_for_run(run_dir, wait_sec)
        return _format_state_for_mcp(state)

    if name == "deep_survey_status":
        run_id = (arguments.get("run_id") or "").strip()
        wait_sec = max(0, min(115, int(arguments.get("wait_seconds") or 60)))
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        if run_id == "latest":
            dirs = sorted([d for d in RUNS_DIR.iterdir() if (d / "state.json").exists()], reverse=True)
            if not dirs:
                raise SurveyError("暂无历史调研任务。")
            run_dir = dirs[0]
        else:
            if "/" in run_id or ".." in run_id:
                raise SurveyError("无效 run_id。")
            run_dir = RUNS_DIR / run_id
            if not run_dir.exists():
                raise SurveyError(f"未找到 run_id: {run_id}")
        state = wait_for_run(run_dir, wait_sec)
        return _format_state_for_mcp(state)

    raise SurveyError(f"未知工具: {name}")


def _read_message(stdin_buf) -> tuple[dict | None, bool]:
    while True:
        line = stdin_buf.readline()
        if not line:
            return None, False
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(b"content-length:"):
            length = int(stripped.split(b":", 1)[1].strip())
            while True:
                hdr = stdin_buf.readline()
                if not hdr or hdr in (b"\r\n", b"\n"):
                    break
            body = stdin_buf.read(length)
            return json.loads(body.decode("utf-8")), True
        return json.loads(stripped.decode("utf-8")), False


def _write_message(stdout_buf, msg: dict, framed: bool) -> None:
    raw = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    if framed:
        header = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii")
        stdout_buf.write(header + raw)
    else:
        stdout_buf.write(raw + b"\n")
    stdout_buf.flush()


def serve_stdio() -> None:
    stdin_buf = sys.stdin.buffer
    stdout_buf = sys.stdout.buffer

    while True:
        try:
            req, framed = _read_message(stdin_buf)
        except Exception as exc:
            sys.stderr.write(f"MCP parse error: {redact_secrets(exc)}\n")
            sys.stderr.flush()
            continue
        if req is None:
            break

        method = req.get("method", "")
        msg_id = req.get("id")
        params = req.get("params") or {}

        if msg_id is None:
            continue

        try:
            if method == "initialize":
                proto = params.get("protocolVersion") or "2024-11-05"
                result = {
                    "protocolVersion": proto,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                tname = params.get("name", "")
                targs = params.get("arguments") or {}
                result = handle_tool_call(tname, targs)
            elif method in {"resources/list", "prompts/list"}:
                key = "resources" if "resources" in method else "prompts"
                result = {key: []}
            else:
                _write_message(
                    stdout_buf,
                    {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}},
                    framed,
                )
                continue

            _write_message(stdout_buf, {"jsonrpc": "2.0", "id": msg_id, "result": result}, framed)
        except Exception as exc:
            _write_message(
                stdout_buf,
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": f"错误：{redact_secrets(exc)}"}],
                        "isError": True,
                    },
                },
                framed,
            )


if __name__ == "__main__":
    serve_stdio()
