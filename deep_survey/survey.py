#!/usr/bin/env python3
"""
大调查 (Deep Survey) — Multi-source native agentic research aggregator.

Orchestrates official subscription-backed native search CLIs, developer community indices,
and optional pluggable search APIs:
  1. codex  -> Personal ChatGPT subscription (--ignore-user-config --search), server-side OpenAI web_search + open_page
  2. grok   -> Personal Grok subscription (grok -p), server-side native X search + web_search (auto-failover to xai_api if configured)
  3. agy    -> Personal Google Antigravity subscription (agy -p), Google Search grounding (auto-failover to gemini_api if configured)
  4. github -> Official GitHub CLI (gh), repos + issues with deterministic popularity & maintenance scoring
  5. hn     -> Official Hacker News Algolia Index, stories + practitioner comments with points/comments calibration
  + Optional pluggable APIs: xai_api, gemini_api, perplexity, tavily, exa, brave, oai_api

Design principles:
  - Never modifies ~/.codex/config.toml (company/work mode stays untouched).
  - Never spends personal OpenAI API credits unless --include-oai-api is explicitly passed.
  - Graceful degradation: any subset of surviving channels (>=1) produces a synthesized report.
  - Context isolation: raw channel outputs stay on disk; main Agent receives a compact, cited report.
  - Recursive guard: sets DEEP_SURVEY_ACTIVE=1 and runs CLIs in an empty temp cwd with nice -n 10.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

from .connectors import (
    CONFIG_FILE,
    OPTIONAL_API_SPECS,
    configured_optional_channels,
    doctor_optional_connectors,
    get_connector_key,
    run_hn_channel,
    run_optional_api_channel,
)

HOME = Path.home()
LOCAL_BIN = HOME / ".local/bin"


def _find_bin(env_key: str, name: str, extra_paths: tuple[str, ...] = ()) -> str:
    override = os.environ.get(env_key)
    if override:
        return override
    for candidate in (str(LOCAL_BIN / name), *extra_paths):
        if Path(candidate).exists():
            return candidate
    found = shutil.which(name)
    return found or str(LOCAL_BIN / name)


CODEX_BIN = _find_bin("CODEX_BIN", "codex", ("/opt/homebrew/bin/codex", "/usr/local/bin/codex"))
GROK_BIN = _find_bin("GROK_BIN", "grok", (str(HOME / ".grok/bin/grok"), "/usr/local/bin/grok"))
AGY_BIN = _find_bin("AGY_BIN", "agy", ("/usr/local/bin/agy",))
GH_BIN = _find_bin("GH_BIN", "gh", ("/opt/homebrew/bin/gh", "/usr/bin/gh", "/usr/local/bin/gh"))
OAI_PERSONAL_BIN = _find_bin("OAI_PERSONAL_BIN", "oai-personal")

CACHE_ROOT = Path(os.environ.get("DEEP_SURVEY_CACHE_DIR") or (HOME / ".cache/deep-survey"))
RUNS_DIR = CACHE_ROOT / "runs"
EMPTY_CWD = CACHE_ROOT / "empty-workspace"

ALL_DEFAULT_CHANNELS = ("codex", "grok", "agy", "github", "hn")
URL_RE = re.compile(r"https?://[^\s)\]\"'>]+")
REPO_SLUG_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")

STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "what", "how", "why", "when",
    "where", "which", "into", "about", "using", "used", "best", "good", "tool", "tools",
    "use", "make", "build", "vs", "or", "in", "on", "to", "of", "is", "are", "can",
}


class SurveyError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def local_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def redact_secrets(text: object) -> str:
    s = str(text)
    s = re.sub(r"sk-[A-Za-z0-9_.*-]+", "[REDACTED_KEY]", s)
    s = re.sub(r"gh[pousr]_[A-Za-z0-9_]+", "[REDACTED_GH_TOKEN]", s)
    s = re.sub(r"xai-[A-Za-z0-9_.*-]+", "[REDACTED_XAI_KEY]", s)
    s = re.sub(r"pplx-[A-Za-z0-9_.*-]+", "[REDACTED_PPLX_KEY]", s)
    s = re.sub(r"tvly-[A-Za-z0-9_.*-]+", "[REDACTED_TAVILY_KEY]", s)
    s = re.sub(r"ds_[A-Za-z0-9_.*-]+", "[REDACTED_DS_TOKEN]", s)
    return s


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def ensure_empty_cwd() -> Path:
    EMPTY_CWD.mkdir(parents=True, exist_ok=True)
    for child in EMPTY_CWD.iterdir():
        if child.is_file() or child.is_symlink():
            child.unlink(missing_ok=True)
    return EMPTY_CWD


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    path_parts = [
        str(LOCAL_BIN),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        env.get("PATH", ""),
    ]
    env["PATH"] = ":".join(p for p in path_parts if p)
    env["DEEP_SURVEY_ACTIVE"] = "1"
    env["NO_COLOR"] = "1"
    return env


def run_proc(cmd: list[str], *, timeout: int, cwd: Path | None = None, use_nice: bool = True) -> subprocess.CompletedProcess[str]:
    nice_bin = shutil.which("nice")
    full_cmd = ([nice_bin, "-n", "10"] + cmd) if (use_nice and nice_bin) else cmd
    return subprocess.run(
        full_cmd,
        cwd=str(cwd or ensure_empty_cwd()),
        env=child_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def extract_urls(text: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for raw in URL_RE.findall(text or ""):
        clean = raw.rstrip(".,;:!?`'\"）)】》>")
        if clean and clean not in seen:
            seen.add(clean)
            urls.append(clean)
    return urls


def extract_repo_slugs(text: str) -> list[str]:
    seen: set[str] = set()
    slugs: list[str] = []
    excluded_tokens = {
        "topics", "search", "orgs", "settings", "marketplace", "features",
        "trending", "collections", "sponsors", "login", "pricing", "about",
        "users", "local", "bin", "share", "cache", "config", "var", "etc", "opt", "usr",
        "http", "https", "v1", "mcp", "channels", "runs", "outputs",
    }
    candidates = list(REPO_SLUG_RE.findall(text or ""))
    stripped_urls = URL_RE.sub(" ", text or "")
    for m in re.findall(r"(?<![/\w.-])([A-Za-z0-9_.-]{2,39}/[A-Za-z0-9_.-]{2,80})(?![/\w.-])", stripped_urls):
        candidates.append(m)
    for m in candidates:
        owner, repo = m.split("/", 1)
        repo = re.sub(r"\.git$", "", repo).rstrip(".,;:!?`'\"）)】》>")
        if not owner or not repo or owner.lower() in excluded_tokens or repo.lower() in excluded_tokens:
            continue
        if owner.startswith(".") or repo.startswith("."):
            continue
        slug = f"{owner}/{repo}"
        key = slug.lower()
        if key not in seen:
            seen.add(key)
            slugs.append(slug)
    return slugs


def slugify_topic(query: str) -> str:
    ascii_words = re.findall(r"[A-Za-z0-9]+", query.lower())
    if ascii_words:
        base = "-".join(ascii_words[:5])[:36].strip("-")
        if base:
            return base
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]+", query))[:12]
    return cjk or "survey"


def classify_cli_failure(raw_err: str, returncode: int) -> tuple[str, str]:
    low = (raw_err or "").lower()
    if "402" in low or "balance exhausted" in low or "quota" in low or "insufficient_quota" in low:
        if "grok build usage balance exhausted" in low:
            return ("quota_exhausted", "Grok 订阅周额度已耗尽 (HTTP 402)，已自动跳过并由其余通道接管")
        return ("quota_exhausted", f"该通道订阅额度已耗尽，已自动跳过 ({redact_secrets(raw_err.strip()[-180:])})")
    if "401" in low or "unauthorized" in low or "not logged in" in low:
        return ("auth_expired", f"登录态失效或未授权，已自动跳过 ({redact_secrets(raw_err.strip()[-180:])})")
    return ("failed", redact_secrets(raw_err.strip()[-400:]) or f"exit={returncode}")


# ---------------------------------------------------------------------------
# Channel 1: Codex Personal Subscription (Official Responses web_search)
# ---------------------------------------------------------------------------

def run_codex_channel(query: str, *, depth: str, focus_hint: str = "", timeout: int = 210) -> dict:
    started = time.monotonic()
    if not Path(CODEX_BIN).exists():
        return {"channel": "codex", "status": "unavailable", "error": f"未找到 codex: {CODEX_BIN}"}

    effort = "high" if depth == "deep" else "medium"
    depth_rule = (
        "至少从官方一手文档、技术博客/基准测试、GitHub 源码或 Issue 三个维度交叉检索，必要时打开关键页面核实细节（控制在 6 次以内搜索动作）。"
        if depth == "deep"
        else "高效执行 2~4 次针对性检索（仅在必要时打开 1~2 个核心页面），快速完成取证。"
    )
    extra = f"\n补充聚焦方向：{focus_hint}\n" if focus_hint else ""
    prompt = (
        f"你正在执行官方服务端原生联网调研（禁止读写本地文件，只使用内置 web_search 工具）。\n"
        f"调研主题：{query}\n{extra}"
        f"要求：\n"
        f"1. {depth_rule}\n"
        f"2. 用简体中文输出结构化取证笔记：核心事实、主流方案对比、适用边界/已知坑点、关键项目名（尽量带 GitHub owner/repo）。\n"
        f"3. 每一条关键事实必须附可点击的真实 URL 来源，明确区分【一手实证】与【推断/待核实】。"
    )
    cmd = [
        CODEX_BIN, "--search", "exec",
        "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
        "-s", "read-only",
        "-c", 'web_search="live"',
        "-c", f'model_reasoning_effort="{effort}"',
        "--json", prompt,
    ]
    try:
        proc = run_proc(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {
            "channel": "codex",
            "provider": "OpenAI ChatGPT Personal Subscription (Server-side web_search)",
            "status": "timeout",
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "error": f"Codex 检索超时 ({timeout}s)",
        }

    messages: list[str] = []
    search_actions: list[dict] = []
    sources: list[dict] = []
    seen_urls: set[str] = set()
    usage: dict = {}
    errors: list[str] = []

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except ValueError:
            continue
        etype = evt.get("type")
        if etype == "error":
            errors.append(str(evt.get("message") or evt))
        elif etype == "turn.failed":
            errors.append(str(evt.get("error") or evt))
        elif etype == "turn.completed":
            usage = evt.get("usage") or {}
        elif etype == "item.completed":
            item = evt.get("item") or {}
            itype = item.get("type")
            if itype == "agent_message" and item.get("text"):
                messages.append(item["text"])
            elif itype == "web_search":
                act = item.get("action") or {}
                search_actions.append({
                    "type": act.get("type", "search"),
                    "query": act.get("query") or item.get("query") or "",
                    "url": act.get("url") or "",
                })
                for res in item.get("results") or []:
                    u = res.get("url")
                    if u and u not in seen_urls:
                        seen_urls.add(u)
                        sources.append({"title": res.get("title") or res.get("domain") or u, "url": u})

    text = messages[-1].strip() if messages else ""
    for u in extract_urls(text):
        if u not in seen_urls:
            seen_urls.add(u)
            sources.append({"title": u, "url": u})

    if proc.returncode != 0 or not text:
        raw_err = "; ".join(errors) or proc.stderr or proc.stdout
        st, msg = classify_cli_failure(raw_err, proc.returncode)
        return {
            "channel": "codex",
            "provider": "OpenAI ChatGPT Personal Subscription (Server-side web_search)",
            "status": st,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "web_search_calls": len(search_actions),
            "error": msg,
        }

    return {
        "channel": "codex",
        "provider": "OpenAI ChatGPT Personal Subscription (Server-side web_search)",
        "status": "completed",
        "verified_native_search": len(search_actions) > 0,
        "web_search_calls": len(search_actions),
        "search_actions": search_actions,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "usage": usage,
        "sources": sources,
        "text": text,
    }


# ---------------------------------------------------------------------------
# Channel 2: Grok Subscription (Native x_search + auto-failover to xai_api)
# ---------------------------------------------------------------------------

def run_grok_channel(query: str, *, depth: str, focus_hint: str = "", timeout: int = 210) -> dict:
    started = time.monotonic()
    if not Path(GROK_BIN).exists():
        if get_connector_key("xai_api"):
            res = run_optional_api_channel("xai_api", query, depth=depth, timeout=timeout)
            res["channel"] = "grok"
            return res
        return {"channel": "grok", "status": "unavailable", "error": f"未找到 grok CLI 且未配置 XAI_API_KEY"}

    max_turns = 9 if depth == "deep" else 6
    extra = f"\n补充核查重点：{focus_hint}\n" if focus_hint else ""
    prompt = (
        f"你正在作为多源调研系统中的【X (Twitter) 与实时社区情报专员】执行调研。\n"
        f"请直接使用你内置的服务端 X 搜索与网页搜索能力（不要查找外部 MCP 工具，不要读写任何本地文件，并在 {max_turns - 2} 轮工具调用内收尾输出最终报告）。\n\n"
        f"调研主题：{query}\n{extra}\n"
        f"请重点挖掘：\n"
        f"1. **X 平台一手讨论**：真实开发者/用户的最新实测反馈、踩坑吐槽、性能/成本对比、推荐替代品或冷门高价值线索（务必附上具体的 `https://x.com/.../status/...` 帖子链接、作者与发帖日期）。\n"
        f"2. **舆论热度与真实口碑校准**：区分“刷屏营销/水军炒作”与“真正在生产/实战中用起来的口碑”，如果某方案在 X 上几乎无人讨论或负面反馈集中，请明确指出。\n"
        f"3. **网页与社区补充**：补充 Reddit / HN / 博客中的最新实战讨论链接。\n"
        f"请用简体中文输出结构清晰的取证报告。"
    )
    cmd = [
        GROK_BIN, "-p", prompt,
        "--output-format", "json",
        "--no-memory", "--no-subagents", "--no-plan",
        "--always-approve",
        "--max-turns", str(max_turns),
        "--cwd", str(ensure_empty_cwd()),
    ]
    try:
        proc = run_proc(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {
            "channel": "grok",
            "provider": "xAI Grok Subscription (Server-side X Search + Web Search)",
            "status": "timeout",
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "error": f"Grok 检索超时 ({timeout}s)",
        }

    payload = None
    raw_out = proc.stdout.strip()
    if raw_out:
        try:
            payload = json.loads(raw_out)
        except ValueError:
            idx = raw_out.find("{")
            ridx = raw_out.rfind("}")
            if idx != -1 and ridx > idx:
                try:
                    payload = json.loads(raw_out[idx : ridx + 1])
                except ValueError:
                    payload = None

    if proc.returncode != 0 or not isinstance(payload, dict):
        # Seamless failover to xAI Official API if user configured XAI_API_KEY
        if get_connector_key("xai_api"):
            api_res = run_optional_api_channel("xai_api", query, depth=depth, timeout=timeout)
            if api_res.get("status") == "completed":
                api_res["channel"] = "grok"
                api_res["provider"] = "xAI Official API (Auto-failover from Grok CLI)"
                return api_res
        st, msg = classify_cli_failure(proc.stderr or raw_out, proc.returncode)
        return {
            "channel": "grok",
            "provider": "xAI Grok Subscription (Server-side X Search + Web Search)",
            "status": st,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "error": msg,
        }

    text = (payload.get("text") or "").strip()
    urls = extract_urls(text)
    x_urls = [u for u in urls if "x.com/" in u or "twitter.com/" in u]
    num_turns = int(payload.get("num_turns") or 0)

    if not text:
        return {
            "channel": "grok",
            "provider": "xAI Grok Subscription (Server-side X Search + Web Search)",
            "status": "failed",
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "error": f"Grok 返回空正文 (stopReason={payload.get('stopReason')}, turns={num_turns})",
        }

    return {
        "channel": "grok",
        "provider": "xAI Grok Subscription (Server-side X Search + Web Search)",
        "status": "completed",
        "verified_native_search": num_turns >= 2 or len(x_urls) > 0 or len(urls) > 0,
        "num_turns": num_turns,
        "x_post_links_count": len(x_urls),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "usage": payload.get("usage") or {},
        "sources": [{"title": u, "url": u} for u in urls],
        "text": text,
    }


# ---------------------------------------------------------------------------
# Channel 3: Antigravity / Google Subscription (auto-failover to gemini_api)
# ---------------------------------------------------------------------------

def run_agy_channel(query: str, *, depth: str, focus_hint: str = "", timeout: int = 210) -> dict:
    started = time.monotonic()
    if not Path(AGY_BIN).exists():
        if get_connector_key("gemini_api"):
            res = run_optional_api_channel("gemini_api", query, depth=depth, timeout=timeout)
            res["channel"] = "agy"
            return res
        return {"channel": "agy", "status": "unavailable", "error": f"未找到 agy CLI 且未配置 GEMINI_API_KEY"}

    effort = "medium" if depth == "deep" else "low"
    budget_rule = (
        "控制在 3~5 次 search_web 调用内（最多用 read_url_content 精读 1~2 个最核心的一手页面），务必在 100 秒内收尾并输出报告。"
        if depth == "deep"
        else "严格控制在 2~3 次 search_web 调用内完成检索（不要反复调用 read_url_content 抓取大页面），务必在 60 秒内直接输出带 URL 的结论报告。"
    )
    extra = f"\n补充核查重点：{focus_hint}\n" if focus_hint else ""
    prompt = (
        f"你正在作为多源调研系统中的【Google 全网广度检索专员】执行调研。\n"
        f"禁止读写本地文件，禁止调用任何本机 shell 命令，只使用你内置的 search_web 工具。\n\n"
        f"调研主题：{query}\n{extra}\n"
        f"要求：\n"
        f"1. {budget_rule}\n"
        f"2. 使用中英文关键词通过 Google 检索官方文档、深度技术对比、GitHub 生态与社区论坛（Reddit/HN/博客等）的真实反馈。\n"
        f"3. 重点补充：主流成熟方案 vs 小众方案的优劣、生态热度、维护活跃度与潜在坑点，所有关键结论附真实可点击 URL。\n"
        f"4. 用简体中文输出结构化取证报告。"
    )
    inner_timeout = max(60, timeout - 15)
    cmd = [
        AGY_BIN, "-p", prompt,
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
        "--effort", effort,
        "--print-timeout", f"{inner_timeout}s",
    ]
    try:
        proc = run_proc(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {
            "channel": "agy",
            "provider": "Google Antigravity Subscription (Google Search Grounding)",
            "status": "timeout",
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "error": f"AGY 检索超时 ({timeout}s)",
        }

    payload = None
    raw_out = proc.stdout.strip()
    for line in reversed(raw_out.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue
    if payload is None and raw_out.startswith("{"):
        try:
            payload = json.loads(raw_out)
        except ValueError:
            payload = None

    text = ""
    if isinstance(payload, dict):
        text = (payload.get("response") or "").strip()
    elif raw_out and not raw_out.startswith("{"):
        text = raw_out.strip()

    if (proc.returncode != 0 and len(text) < 200) or not text:
        if get_connector_key("gemini_api"):
            api_res = run_optional_api_channel("gemini_api", query, depth=depth, timeout=timeout)
            if api_res.get("status") == "completed":
                api_res["channel"] = "agy"
                api_res["provider"] = "Google Gemini Official API (Auto-failover from AGY CLI)"
                return api_res
        st, msg = classify_cli_failure(proc.stderr or raw_out, proc.returncode)
        return {
            "channel": "agy",
            "provider": "Google Antigravity Subscription (Google Search Grounding)",
            "status": st,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "error": msg,
        }

    urls = extract_urls(text)
    return {
        "channel": "agy",
        "provider": "Google Antigravity Subscription (Google Search Grounding)",
        "status": "completed",
        "verified_native_search": len(urls) > 0,
        "num_turns": payload.get("num_turns") if isinstance(payload, dict) else None,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "usage": (payload.get("usage") or {}) if isinstance(payload, dict) else {},
        "sources": [{"title": u, "url": u} for u in urls],
        "text": text,
    }


# ---------------------------------------------------------------------------
# Shared English Technical Query Planner (for GitHub & Hacker News)
# ---------------------------------------------------------------------------

def derive_english_queries(query: str, extra_queries: list[str] | None = None) -> list[str]:
    candidates: list[str] = []
    if extra_queries:
        for q in extra_queries:
            if q and q.strip():
                candidates.append(q.strip())

    raw_tokens = [
        t for t in re.findall(r"[A-Za-z][A-Za-z0-9_+.-]{1,36}", query)
        if t.lower() not in STOPWORDS
    ]
    project_like = [t for t in raw_tokens if ("-" in t or "_" in t) and len(t) >= 4]
    concept_words = [t for t in raw_tokens if t not in project_like]

    for proj in project_like[:3]:
        candidates.append(proj)
    if len(concept_words) >= 2:
        candidates.append(" ".join(concept_words[:4]))
    elif len(concept_words) == 1 and project_like:
        candidates.append(f"{concept_words[0]} {project_like[0].replace('-', ' ').replace('_', ' ')}")

    if len(candidates) < 2:
        helper_prompt = (
            "Convert the following research topic into 3 concise English technical search query strings "
            "(2 to 4 keywords each, no quotes, no search operators). "
            "Output ONLY a valid JSON array of 3 strings and nothing else.\n\nTopic: " + query
        )
        parsed_arr: list[str] = []
        if Path(CODEX_BIN).exists():
            try:
                proc = run_proc(
                    [
                        CODEX_BIN, "exec",
                        "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                        "-s", "read-only", "-c", 'web_search="disabled"',
                        "-c", 'model_reasoning_effort="low"', "--json", helper_prompt,
                    ],
                    timeout=25,
                )
                if proc.returncode == 0:
                    for line in reversed(proc.stdout.splitlines()):
                        if line.startswith("{"):
                            evt = json.loads(line)
                            if evt.get("type") == "item.completed" and (evt.get("item") or {}).get("type") == "agent_message":
                                m = re.search(r"\[[\s\S]*?\]", evt["item"].get("text", ""))
                                if m:
                                    parsed_arr = [x.strip() for x in json.loads(m.group(0)) if isinstance(x, str) and x.strip()]
                                    break
            except Exception:
                pass
        if not parsed_arr and Path(GROK_BIN).exists():
            try:
                proc = run_proc(
                    [
                        GROK_BIN, "-p", helper_prompt,
                        "-m", "grok-4.7-build-fast",
                        "--disable-web-search", "--no-memory", "--no-subagents", "--no-plan",
                        "--max-turns", "1", "--output-format", "json",
                        "--cwd", str(ensure_empty_cwd()),
                    ],
                    timeout=20,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    txt = json.loads(proc.stdout).get("text", "")
                    m = re.search(r"\[[\s\S]*?\]", txt)
                    if m:
                        parsed_arr = [x.strip() for x in json.loads(m.group(0)) if isinstance(x, str) and x.strip()]
            except Exception:
                pass
        candidates.extend(parsed_arr)

    if not candidates:
        candidates.append(query[:60].strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        k = c.lower()
        if k not in seen:
            seen.add(k)
            deduped.append(c)
    return deduped[:4]


# ---------------------------------------------------------------------------
# Channel 4: GitHub Official Index + Popularity / Health Calibration
# ---------------------------------------------------------------------------

def parse_iso_days_ago(iso_str: str | None) -> int | None:
    if not iso_str:
        return None
    try:
        cleaned = iso_str.replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(cleaned)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        delta = dt.datetime.now(dt.timezone.utc) - d
        return max(0, int(delta.total_seconds() // 86400))
    except ValueError:
        return None


def classify_repo_health(stars: int, created_days: int | None, pushed_days: int | None, archived: bool) -> tuple[str, str]:
    velocity = round(stars / max(1, created_days or 365), 2)
    act = "今日活跃" if pushed_days == 0 else (f"近{pushed_days}天活跃" if pushed_days is not None else "活跃时间未知")
    if archived:
        return ("D_ARCHIVED", f"🕰️ 已归档停更 ({stars}⭐)")
    if pushed_days is not None and pushed_days > 365:
        if stars >= 500:
            return ("D_STALE_POPULAR", f"⚠️ 历史高热但超1年未更新 ({stars}⭐, {pushed_days}天未推代码)")
        return ("E_ABANDONED", f"❌ 冷门且长期停更 ({stars}⭐, {pushed_days}天未更新)")
    if stars >= 1500:
        return ("A_MAINSTREAM", f"🔥 主流高热验证 ({stars}⭐, {act})")
    if stars >= 150:
        return ("B_ESTABLISHED", f"✅ 成熟活跃方案 ({stars}⭐, {act})")
    if (created_days is not None and created_days <= 180 and stars >= 20) or velocity >= 0.5:
        return ("C_EMERGING", f"🌱 近期高潜新秀 ({stars}⭐, 日均+{velocity}⭐, {act})")
    if stars >= 30:
        return ("C_NICHE_ACTIVE", f"🧩 垂直小众活跃 ({stars}⭐, {act}；需核实功能深度)")
    return ("E_LOW_TRACTION", f"⚠️ 极冷门未验证 ({stars}⭐, {act}；星数过低，警惕玩具项目或效果不佳)")


def fetch_repo_detail(slug: str, timeout: int = 15) -> dict | None:
    try:
        proc = run_proc([GH_BIN, "api", f"repos/{slug}"], timeout=timeout, use_nice=False)
        if proc.returncode != 0:
            return None
        d = json.loads(proc.stdout)
        return {
            "fullName": d.get("full_name") or slug,
            "url": d.get("html_url") or f"https://github.com/{slug}",
            "description": d.get("description") or "",
            "stargazersCount": int(d.get("stargazers_count") or 0),
            "forksCount": int(d.get("forks_count") or 0),
            "openIssuesCount": int(d.get("open_issues_count") or 0),
            "isArchived": bool(d.get("archived")),
            "createdAt": d.get("created_at"),
            "pushedAt": d.get("pushed_at") or d.get("updated_at"),
            "language": d.get("language") or "",
            "license": (d.get("license") or {}).get("spdx_id") or "",
            "topics": d.get("topics") or [],
        }
    except Exception:
        return None


def run_github_channel(
    query: str,
    *,
    depth: str,
    prebuilt_queries: list[str] | None = None,
    inspect_slugs: list[str] | None = None,
    timeout: int = 90,
) -> dict:
    started = time.monotonic()
    if not Path(GH_BIN).exists():
        return {"channel": "github", "status": "unavailable", "error": f"未找到 gh: {GH_BIN}"}

    gh_queries = prebuilt_queries or derive_english_queries(query)
    repos_by_name: dict[str, dict] = {}
    issues_found: list[dict] = []
    seen_issues: set[str] = set()

    explicit_slugs = list(dict.fromkeys([*(extract_repo_slugs(query)), *(inspect_slugs or [])]))
    for slug in explicit_slugs[:10]:
        detail = fetch_repo_detail(slug)
        if detail:
            repos_by_name[detail["fullName"].lower()] = detail

    six_months_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=180)).strftime("%Y-%m-%d")

    for gq in gh_queries:
        try:
            p_top = run_proc(
                [
                    GH_BIN, "search", "repos",
                    "--sort", "stars", "--limit", "8",
                    "--json", "fullName,description,stargazersCount,forksCount,openIssuesCount,isArchived,createdAt,pushedAt,updatedAt,url,language",
                    "--", gq,
                ],
                timeout=20,
                use_nice=False,
            )
            if p_top.returncode == 0 and p_top.stdout.strip():
                for r in json.loads(p_top.stdout):
                    key = (r.get("fullName") or "").lower()
                    if key and key not in repos_by_name:
                        repos_by_name[key] = r
        except Exception:
            pass

        try:
            p_recent = run_proc(
                [
                    GH_BIN, "search", "repos",
                    "--sort", "updated", "--limit", "5",
                    "--json", "fullName,description,stargazersCount,forksCount,openIssuesCount,isArchived,createdAt,pushedAt,updatedAt,url,language",
                    "--", f"{gq} stars:>=15 pushed:>={six_months_ago}",
                ],
                timeout=20,
                use_nice=False,
            )
            if p_recent.returncode == 0 and p_recent.stdout.strip():
                for r in json.loads(p_recent.stdout):
                    key = (r.get("fullName") or "").lower()
                    if key and key not in repos_by_name:
                        repos_by_name[key] = r
        except Exception:
            pass

        if depth == "deep" or len(issues_found) < 6:
            try:
                p_iss = run_proc(
                    [
                        GH_BIN, "search", "issues",
                        "--sort", "comments", "--limit", "5",
                        "--json", "title,url,commentsCount,state,updatedAt,repository",
                        "--", gq,
                    ],
                    timeout=20,
                    use_nice=False,
                )
                if p_iss.returncode == 0 and p_iss.stdout.strip():
                    for iss in json.loads(p_iss.stdout):
                        u = iss.get("url")
                        if u and u not in seen_issues:
                            seen_issues.add(u)
                            issues_found.append(iss)
            except Exception:
                pass

    enriched: list[dict] = []
    tier_order = {
        "A_MAINSTREAM": 0,
        "B_ESTABLISHED": 1,
        "C_EMERGING": 2,
        "C_NICHE_ACTIVE": 3,
        "D_STALE_POPULAR": 4,
        "D_ARCHIVED": 5,
        "E_LOW_TRACTION": 6,
        "E_ABANDONED": 7,
    }
    for r in repos_by_name.values():
        stars = int(r.get("stargazersCount") or 0)
        c_days = parse_iso_days_ago(r.get("createdAt"))
        p_days = parse_iso_days_ago(r.get("pushedAt") or r.get("updatedAt"))
        archived = bool(r.get("isArchived"))
        tier, badge = classify_repo_health(stars, c_days, p_days, archived)
        item = dict(r)
        item["tier"] = tier
        item["healthBadge"] = badge
        item["createdDaysAgo"] = c_days
        item["pushedDaysAgo"] = p_days
        enriched.append(item)

    explicit_keys = {s.lower() for s in explicit_slugs}
    enriched.sort(
        key=lambda x: (
            0 if (x.get("fullName") or "").lower() in explicit_keys else 1,
            tier_order.get(x["tier"], 9),
            -int(x.get("stargazersCount") or 0),
        )
    )

    lines = [
        "# GitHub 官方索引与热度校准报告",
        f"- 检索词组：{', '.join(f'`{q}`' for q in gh_queries)}",
        f"- 命中去重仓库数：{len(enriched)}；高讨论度 Issues 数：{len(issues_found)}",
        "",
        "## 一、仓库热度与维护健康度分级（客观 API 指标）",
    ]
    if not enriched:
        lines.append("未检索到直接匹配的公开仓库。")
    else:
        for r in enriched[:18]:
            lang = f" · `{r.get('language')}`" if r.get("language") else ""
            pushed_date = (r.get("pushedAt") or r.get("updatedAt") or "")[:10] or "未知"
            arch_flag = "已归档" if r.get("isArchived") else "未归档"
            desc = (r.get("description") or "无描述").strip().replace("\n", " ")
            lines.append(
                f"- **[{r['fullName']}]({r['url']})** — {r['healthBadge']}{lang} · Forks: {r.get('forksCount', 0)} · 最近推送: `{pushed_date}` · `{arch_flag}`\n"
                f"  - 简介：{desc[:220]}"
            )

    if issues_found:
        lines.extend(["", "## 二、高讨论度 GitHub Issues（真实踩坑与社区反馈）"])
        for iss in issues_found[:10]:
            repo_name = (iss.get("repository") or {}).get("nameWithOwner") or ""
            lines.append(
                f"- [{repo_name}: {iss.get('title')}]({iss.get('url')}) "
                f"(评论数: {iss.get('commentsCount', 0)}, 状态: {iss.get('state')})"
            )

    sources = [{"title": r["fullName"], "url": r["url"]} for r in enriched[:18]] + [
        {"title": iss.get("title") or iss["url"], "url": iss["url"]} for iss in issues_found[:8]
    ]

    return {
        "channel": "github",
        "provider": "GitHub Official Search & REST API (gh CLI)",
        "status": "completed",
        "verified_native_search": True,
        "queries": gh_queries,
        "repos_count": len(enriched),
        "issues_count": len(issues_found),
        "repos": enriched[:20],
        "issues": issues_found[:10],
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "sources": sources,
        "text": "\n".join(lines),
    }


# ---------------------------------------------------------------------------
# Optional Opt-in Channel: oai-personal API (ONLY when --include-oai-api)
# ---------------------------------------------------------------------------

def run_oai_api_channel(query: str, out_dir: Path, *, depth: str, timeout: int = 240) -> dict:
    started = time.monotonic()
    if not Path(OAI_PERSONAL_BIN).exists():
        return {"channel": "oai_api", "status": "unavailable", "error": "未找到 oai-personal"}
    out_md = out_dir / "oai_api_raw.md"
    subcmd = "research" if depth == "deep" else "search"
    cmd = [OAI_PERSONAL_BIN, subcmd, query, "--out", str(out_md)]
    if subcmd == "research":
        cmd.extend(["--wait", str(min(180, timeout - 15))])
    try:
        proc = run_proc(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"channel": "oai_api", "status": "timeout", "elapsed_seconds": round(time.monotonic() - started, 2), "error": "oai-personal 超时"}
    if proc.returncode != 0 or not out_md.exists():
        return {
            "channel": "oai_api",
            "status": "failed",
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "error": redact_secrets((proc.stderr or proc.stdout)[-500:]),
        }
    text = out_md.read_text(encoding="utf-8")
    urls = extract_urls(text)
    return {
        "channel": "oai_api",
        "provider": "OpenAI Personal Official API (oai-personal)",
        "status": "completed",
        "verified_native_search": True,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "sources": [{"title": u, "url": u} for u in urls],
        "text": text,
    }


# ---------------------------------------------------------------------------
# Wave 2 Cross-Verification (Repo Verifier + Optional Deep X Follow-up)
# ---------------------------------------------------------------------------

def run_wave2_verification(
    query: str,
    wave1_results: dict[str, dict],
    *,
    allow_llm_followup: bool = True,
    timeout: int = 120,
) -> dict[str, dict]:
    wave2: dict[str, dict] = {}
    started_audit = time.monotonic()

    already_known = {
        r["fullName"].lower()
        for r in (wave1_results.get("github", {}).get("repos") or [])
        if r.get("fullName")
    }
    discovered_slugs: list[str] = []
    for ch, payload in wave1_results.items():
        if ch == "github":
            continue
        txt = (payload or {}).get("text") or ""
        for slug in extract_repo_slugs(txt):
            if slug.lower() not in already_known and slug not in discovered_slugs:
                discovered_slugs.append(slug)

    if discovered_slugs and Path(GH_BIN).exists():
        verified_repos: list[dict] = []
        for slug in discovered_slugs[:12]:
            d = fetch_repo_detail(slug)
            if d:
                c_days = parse_iso_days_ago(d.get("createdAt"))
                p_days = parse_iso_days_ago(d.get("pushedAt"))
                tier, badge = classify_repo_health(d["stargazersCount"], c_days, p_days, d["isArchived"])
                d["tier"] = tier
                d["healthBadge"] = badge
                verified_repos.append(d)
        if verified_repos:
            lines = ["## 二阶段核验：各路提及 GitHub 仓库的真实热度与维护状态"]
            for r in verified_repos:
                pushed_date = (r.get("pushedAt") or "")[:10] or "未知"
                arch_flag = "已归档" if r.get("isArchived") else "未归档"
                lines.append(
                    f"- **[{r['fullName']}]({r['url']})** — {r['healthBadge']} · 最近推送: `{pushed_date}` · `{arch_flag}` · Issues: {r.get('openIssuesCount', 0)} · 简介：{(r.get('description') or '无')[:180]}"
                )
            wave2["github_audit"] = {
                "channel": "github_audit",
                "provider": "GitHub API Second-Pass Repo Verifier",
                "status": "completed",
                "elapsed_seconds": round(time.monotonic() - started_audit, 2),
                "verified_repos": verified_repos,
                "text": "\n".join(lines),
            }

    top_candidates = [
        r["fullName"]
        for r in (wave1_results.get("github", {}).get("repos") or [])[:4]
        if r.get("tier") in {"A_MAINSTREAM", "B_ESTABLISHED", "C_EMERGING"}
    ]
    for r in (wave2.get("github_audit", {}).get("verified_repos") or [])[:3]:
        if r.get("tier") in {"A_MAINSTREAM", "B_ESTABLISHED", "C_EMERGING"} and r["fullName"] not in top_candidates:
            top_candidates.append(r["fullName"])

    if allow_llm_followup and top_candidates and wave1_results.get("grok", {}).get("status") == "completed":
        grok_text_lower = (wave1_results["grok"].get("text") or "").lower()
        missing_on_x = [name for name in top_candidates[:4] if name.split("/")[-1].lower() not in grok_text_lower]
        if missing_on_x:
            focus = f"专门在 X 上核查以下候选项目/方案的真实开发者评价、缺陷吐槽或生产可用性：{', '.join(missing_on_x)}"
            res = run_grok_channel(query, depth="fast", focus_hint=focus, timeout=min(100, timeout))
            if res.get("status") == "completed":
                res["channel"] = "grok_wave2"
                res["provider"] = "Grok Second-Pass X Reputation Check"
                wave2["grok_wave2"] = res

    return wave2


# ---------------------------------------------------------------------------
# Cross-Channel Synthesizer (with multi-CLI automatic failover)
# ---------------------------------------------------------------------------

def synthesize_report(
    query: str,
    depth: str,
    channel_results: dict[str, dict],
    wave2_results: dict[str, dict],
    run_dir: Path,
) -> tuple[str, str]:
    status_lines = []
    evidence_blocks = []

    all_blocks = list(channel_results.values()) + list(wave2_results.values())
    for item in all_blocks:
        ch = item.get("channel", "?")
        st = item.get("status", "?")
        prov = item.get("provider", ch)
        elapsed = item.get("elapsed_seconds", "-")
        if st == "completed":
            extra_meta = []
            if "web_search_calls" in item:
                extra_meta.append(f"原生搜索 {item['web_search_calls']} 次")
            if "x_post_links_count" in item:
                extra_meta.append(f"X 帖子链接 {item['x_post_links_count']} 条")
            if "repos_count" in item:
                extra_meta.append(f"仓库 {item['repos_count']} 个 / Issue {item.get('issues_count', 0)} 条")
            if "stories_count" in item:
                extra_meta.append(f"HN 帖子 {item['stories_count']} 条 / 评论 {item.get('comments_count', 0)} 条")
            meta_str = f"（{', '.join(extra_meta)}）" if extra_meta else ""
            status_lines.append(f"- ✅ **{ch}** ({prov}) — 成功，耗时 {elapsed}s{meta_str} · 原文：`{run_dir / 'channels' / (ch + '.md')}`")
            txt = (item.get("text") or "").strip()
            if len(txt) > 14000:
                txt = txt[:14000] + "\n...[详见本地分报告文件]..."
            evidence_blocks.append(f"=== CHANNEL: {ch} ({prov}) ===\n{txt}")
        else:
            err = (item.get("error") or "未启用或失败").replace("\n", " ")[:180]
            status_lines.append(f"- ⚠️ **{ch}** ({prov}) — `{st}`：{err}")

    synth_prompt = (
        "你是「大调查」(Deep Survey) 多源调研系统的【总编辑与交叉核验官】。下面是从多个独立官方原生检索与社区索引通道采集到的一手取证材料。\n"
        "请勿再发起任何联网搜索或读写本地文件，严格基于以下提供的材料完成交叉核验、去重与热度校准，用简体中文输出高密度调研报告。\n\n"
        f"原始调研问题：{query}\n\n"
        "输出结构要求（Markdown）：\n"
        "1. **核心结论与选型/行动建议（TL;DR）**：先说结论，直接回答问题，不要套话。\n"
        "2. **多源交叉共识（高置信度事实）**：综合多路一致印证的事实、架构原理或官方文档结论，附可点击来源链接。\n"
        "3. **GitHub 生态与热度校准评估**（若涉及开源工具/框架/代码方案）：\n"
        "   - 必须结合 GitHub API 客观星数、最近推送日期与归档状态，明确区分：【🔥 主流成熟高 Star 方案】、【🌱 近期高潜活跃新秀】、【⚠️ 历史高热但已停更/归档】、【⚠️ 极冷门未验证玩具（星数过低可能只是效果不好，切勿盲目推荐）】。\n"
        "4. **X / Hacker News / Issues 一线实战情报**：提炼 X 推文、HN 工程师讨论、GitHub Issues 里的真实口碑、隐藏坑点、未写进官方文档的限制或冷门实战技巧（保留具体帖子/评论/Issue 链接）。\n"
        "5. **分歧、存疑与未证实项**：明确指出各路材料互相矛盾、仅有单源孤证或本次未能核实的部分。\n\n"
        "严禁编造材料中不存在的链接、星数或事实。\n\n"
        "================ 采集证据开始 ================\n\n"
        + "\n\n".join(evidence_blocks)
    )

    header = (
        f"# 大调查（Deep Survey）聚合调研报告\n\n"
        f"- **调研主题**：{query}\n"
        f"- **档位模式**：`{depth}` · **生成时间**：{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- **本地完整归档目录**：`{run_dir}`\n\n"
        f"## 通道执行与容错状态\n"
        + "\n".join(status_lines)
        + "\n\n---\n\n"
    )

    completed_primary = [k for k, v in channel_results.items() if v.get("status") == "completed"]
    if len(completed_primary) == 1 and not wave2_results and depth in {"x", "github", "hn"}:
        only_ch = completed_primary[0]
        body = channel_results[only_ch].get("text") or ""
        return header + body + "\n", "passthrough"

    # 1) Codex synthesis
    if Path(CODEX_BIN).exists() and channel_results.get("codex", {}).get("status") not in {"failed", "quota_exhausted", "auth_expired"}:
        try:
            proc = run_proc(
                [
                    CODEX_BIN, "exec",
                    "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                    "-s", "read-only", "-c", 'web_search="disabled"',
                    "-c", 'model_reasoning_effort="medium"',
                    "--json", synth_prompt,
                ],
                timeout=120,
            )
            if proc.returncode == 0:
                msgs = []
                for line in proc.stdout.splitlines():
                    if line.startswith("{"):
                        try:
                            evt = json.loads(line)
                            if evt.get("type") == "item.completed" and (evt.get("item") or {}).get("type") == "agent_message":
                                msgs.append(evt["item"].get("text", ""))
                        except ValueError:
                            pass
                if msgs and msgs[-1].strip():
                    return header + msgs[-1].strip() + "\n", "codex"
        except Exception:
            pass

    # 2) AGY synthesis failover
    if Path(AGY_BIN).exists() and channel_results.get("agy", {}).get("status") not in {"failed", "quota_exhausted", "auth_expired"}:
        try:
            proc = run_proc(
                [
                    AGY_BIN, "-p", synth_prompt,
                    "--output-format", "json",
                    "--dangerously-skip-permissions", "--disable-slash-commands",
                    "--effort", "medium", "--print-timeout", "90s",
                ],
                timeout=105,
            )
            if proc.returncode == 0:
                for line in reversed(proc.stdout.strip().splitlines()):
                    if line.startswith("{"):
                        d = json.loads(line)
                        if d.get("status") == "SUCCESS" and (d.get("response") or "").strip():
                            return header + d["response"].strip() + "\n", "agy"
        except Exception:
            pass

    # 3) Grok synthesis failover
    if Path(GROK_BIN).exists() and channel_results.get("grok", {}).get("status") not in {"failed", "quota_exhausted", "auth_expired"}:
        try:
            proc = run_proc(
                [
                    GROK_BIN, "-p", synth_prompt,
                    "--disable-web-search", "--no-memory", "--no-subagents", "--no-plan",
                    "--max-turns", "1", "--output-format", "json",
                    "--cwd", str(ensure_empty_cwd()),
                ],
                timeout=90,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                txt = (json.loads(proc.stdout).get("text") or "").strip()
                if txt:
                    return header + txt + "\n", "grok"
        except Exception:
            pass

    fallback_body = "\n\n---\n\n".join(evidence_blocks)
    return header + "## 各存活通道原始取证汇总（离线兜底拼接）\n\n" + fallback_body + "\n", "deterministic_fallback"


# ---------------------------------------------------------------------------
# Orchestrator & State Management
# ---------------------------------------------------------------------------

def execute_survey_run(
    run_id: str,
    run_dir: Path,
    query: str,
    *,
    mode: str,
    channels: list[str],
    include_oai_api: bool = False,
    out_report: Path | None = None,
    timeout_per_channel: int = 210,
) -> dict:
    if os.environ.get("DEEP_SURVEY_ACTIVE") == "1" and os.environ.get("DEEP_SURVEY_WORKER") != "1":
        raise SurveyError("检测到递归调用 (DEEP_SURVEY_ACTIVE=1)，已阻止子代理套娃执行 deep-survey。")

    started_clock = time.monotonic()
    channels_dir = run_dir / "channels"
    channels_dir.mkdir(parents=True, exist_ok=True)
    state_file = run_dir / "state.json"
    report_file = out_report or (run_dir / "report.md")

    active_channels = list(dict.fromkeys([*channels, *configured_optional_channels()]))
    if include_oai_api and "oai_api" not in active_channels:
        active_channels.append("oai_api")

    state: dict = {
        "run_id": run_id,
        "status": "in_progress",
        "phase": "wave1_parallel_search",
        "mode": mode,
        "query": query,
        "requested_channels": active_channels,
        "started_at": utc_now(),
        "pid": os.getpid(),
        "run_dir": str(run_dir),
        "report_file": str(report_file),
        "channels_completed": {},
    }
    atomic_write_json(state_file, state)

    depth = "deep" if mode == "deep" else "fast"
    shared_en_queries = derive_english_queries(query) if ({"github", "hn"} & set(active_channels)) else []
    channel_results: dict[str, dict] = {}

    def _run_one(ch: str) -> tuple[str, dict]:
        if ch == "codex":
            return ch, run_codex_channel(query, depth=depth, timeout=timeout_per_channel)
        if ch == "grok":
            return ch, run_grok_channel(query, depth=depth, timeout=timeout_per_channel)
        if ch == "agy":
            return ch, run_agy_channel(query, depth=depth, timeout=timeout_per_channel)
        if ch == "github":
            return ch, run_github_channel(query, depth=depth, prebuilt_queries=shared_en_queries, timeout=min(90, timeout_per_channel))
        if ch == "hn":
            return ch, run_hn_channel(shared_en_queries or [query[:60]], depth=depth, timeout=30)
        if ch == "oai_api":
            return ch, run_oai_api_channel(query, channels_dir, depth=depth, timeout=timeout_per_channel)
        if ch in OPTIONAL_API_SPECS:
            return ch, run_optional_api_channel(ch, query, depth=depth, timeout=timeout_per_channel)
        return ch, {"channel": ch, "status": "unavailable", "error": f"未知通道: {ch}"}

    with ThreadPoolExecutor(max_workers=max(1, len(active_channels))) as pool:
        futures = {pool.submit(_run_one, ch): ch for ch in active_channels}
        for fut in as_completed(futures):
            ch = futures[fut]
            try:
                _, res = fut.result()
            except Exception as exc:
                res = {"channel": ch, "status": "failed", "error": redact_secrets(exc)}
            channel_results[ch] = res
            atomic_write_json(channels_dir / f"{ch}.json", res)
            if res.get("text"):
                atomic_write_text(channels_dir / f"{ch}.md", res["text"] + "\n")
            state["channels_completed"][ch] = {
                "status": res.get("status"),
                "elapsed_seconds": res.get("elapsed_seconds"),
                "verified_native_search": res.get("verified_native_search", False),
            }
            atomic_write_json(state_file, state)

    succeeded = [ch for ch, r in channel_results.items() if r.get("status") == "completed"]
    if not succeeded:
        state["status"] = "failed"
        state["phase"] = "aborted_all_channels_failed"
        state["completed_at"] = utc_now()
        state["elapsed_seconds"] = round(time.monotonic() - started_clock, 2)
        state["errors"] = {ch: r.get("error") for ch, r in channel_results.items()}
        atomic_write_json(state_file, state)
        raise SurveyError("所有检索通道均未成功返回：" + json.dumps(state["errors"], ensure_ascii=False))

    wave2_results: dict[str, dict] = {}
    if mode in {"fast", "deep"}:
        state["phase"] = "wave2_cross_verification"
        atomic_write_json(state_file, state)
        wave2_results = run_wave2_verification(
            query,
            channel_results,
            allow_llm_followup=(mode == "deep"),
            timeout=110,
        )
        for k, res in wave2_results.items():
            atomic_write_json(channels_dir / f"{k}.json", res)
            if res.get("text"):
                atomic_write_text(channels_dir / f"{k}.md", res["text"] + "\n")

    state["phase"] = "synthesizing"
    atomic_write_json(state_file, state)
    report_md, synth_used = synthesize_report(
        query,
        mode if mode in {"x", "github", "hn"} else depth,
        channel_results,
        wave2_results,
        run_dir,
    )

    atomic_write_text(run_dir / "report.md", report_md)
    if report_file.resolve() != (run_dir / "report.md").resolve():
        atomic_write_text(report_file, report_md)

    state.update({
        "status": "completed",
        "phase": "done",
        "synthesizer": synth_used,
        "succeeded_channels": succeeded,
        "failed_channels": [ch for ch in active_channels if ch not in succeeded],
        "wave2_channels": list(wave2_results.keys()),
        "completed_at": utc_now(),
        "elapsed_seconds": round(time.monotonic() - started_clock, 2),
    })
    atomic_write_json(state_file, state)
    return state


# ---------------------------------------------------------------------------
# Detached Worker, Doctor & CLI Entrypoints
# ---------------------------------------------------------------------------

def spawn_detached_worker(
    run_id: str,
    run_dir: Path,
    query: str,
    *,
    mode: str,
    channels: list[str],
    include_oai_api: bool,
    out_report: Path | None,
    timeout_per_channel: int,
) -> int:
    spec = {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "query": query,
        "mode": mode,
        "channels": channels,
        "include_oai_api": include_oai_api,
        "out_report": str(out_report) if out_report else None,
        "timeout_per_channel": timeout_per_channel,
    }
    spec_path = run_dir / "job_spec.json"
    atomic_write_json(spec_path, spec)
    log_path = run_dir / "worker.log"
    repo_root = str(Path(__file__).resolve().parent.parent)
    env = child_env()
    env["DEEP_SURVEY_WORKER"] = "1"
    env["PYTHONPATH"] = repo_root + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    with log_path.open("a", encoding="utf-8") as logf:
        proc = subprocess.Popen(
            [sys.executable, "-B", "-m", "deep_survey.survey", "_worker", str(spec_path)],
            cwd=str(ensure_empty_cwd()),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
            start_new_session=True,
        )
    return proc.pid


def wait_for_run(run_dir: Path, wait_seconds: int) -> dict:
    state_file = run_dir / "state.json"
    deadline = time.monotonic() + wait_seconds
    while True:
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except ValueError:
                state = {}
            if state.get("status") in {"completed", "failed"}:
                return state
            pid = state.get("pid")
            if pid and state.get("status") == "in_progress":
                try:
                    os.kill(int(pid), 0)
                except ProcessLookupError:
                    state["status"] = "failed"
                    state["error"] = "后台 worker 进程意外退出，请检查 worker.log"
                    atomic_write_json(state_file, state)
                    return state
        if time.monotonic() >= deadline:
            if state_file.exists():
                return json.loads(state_file.read_text(encoding="utf-8"))
            return {"status": "starting", "run_dir": str(run_dir)}
        time.sleep(min(2.0, max(0.2, deadline - time.monotonic())))


def collect_doctor_data() -> dict:
    checks: dict[str, dict] = {}
    codex_auth = HOME / ".codex/auth.json"
    checks["codex"] = {
        "binary": CODEX_BIN,
        "installed": Path(CODEX_BIN).exists(),
        "chatgpt_subscription_auth_present": codex_auth.exists(),
        "mode": "Personal ChatGPT Subscription (--ignore-user-config --search)",
    }
    grok_ok = False
    if Path(GROK_BIN).exists():
        try:
            p = run_proc([GROK_BIN, "models"], timeout=10, use_nice=False)
            grok_ok = p.returncode == 0 and "grok" in p.stdout.lower()
        except Exception:
            grok_ok = False
    checks["grok"] = {
        "binary": GROK_BIN,
        "installed": Path(GROK_BIN).exists(),
        "authenticated": grok_ok,
        "xai_api_failover_configured": bool(get_connector_key("xai_api")),
        "mode": "Personal Grok Subscription (Server-side X Search + Web Search)",
    }
    checks["agy"] = {
        "binary": AGY_BIN,
        "installed": Path(AGY_BIN).exists(),
        "gemini_api_failover_configured": bool(get_connector_key("gemini_api")),
        "mode": "Personal Antigravity Subscription (Google Search Grounding)",
    }
    gh_ok = False
    if Path(GH_BIN).exists():
        try:
            p = run_proc([GH_BIN, "auth", "status"], timeout=10, use_nice=False)
            gh_ok = p.returncode == 0
        except Exception:
            gh_ok = False
    checks["github"] = {
        "binary": GH_BIN,
        "installed": Path(GH_BIN).exists(),
        "authenticated": gh_ok,
        "mode": "GitHub Official Search & REST API (Popularity + Activity Calibration)",
    }
    checks["hn"] = {
        "enabled_by_default": True,
        "requires_key": False,
        "mode": "Hacker News Algolia Official Index (Points + Comments Calibration)",
    }
    return {
        "ok": True,
        "default_channels": list(ALL_DEFAULT_CHANNELS),
        "primary_channels": checks,
        "optional_api_connectors": doctor_optional_connectors(),
        "config_file": str(CONFIG_FILE),
        "runs_dir": str(RUNS_DIR),
    }


def resolve_channels_for_mode(mode: str, explicit_channels: str | None) -> list[str]:
    if explicit_channels:
        return [c.strip() for c in explicit_channels.split(",") if c.strip()]
    if mode == "x":
        return ["grok"]
    if mode == "github":
        return ["github"]
    if mode == "hn":
        return ["hn"]
    return list(ALL_DEFAULT_CHANNELS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deep-survey",
        description="大调查 (Deep Survey) — 多源官方原生检索、社区热度校准与远程/本地 MCP 聚合调研系统。",
    )
    subs = parser.add_subparsers(dest="command", required=True)

    subs.add_parser("doctor", help="检查所有原生检索底座与可选 API 插件就绪状态（零消耗）")
    subs.add_parser("mcp", help="启动本地 stdio MCP Server（供 Claude Desktop 等挂载）")

    p_serve = subs.add_parser("serve", help="启动远程 HTTP REST API 与 Streamable HTTP MCP 服务（适用于 VPS/内网穿透）")
    p_serve.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1；公网/局域网暴露可设 0.0.0.0）")
    p_serve.add_argument("--port", type=int, default=8788, help="监听端口（默认 8788）")
    p_serve.add_argument("--token", help="Bearer 鉴权密钥（或设环境变量 DEEP_SURVEY_TOKEN；公网监听未设时自动生成到 ~/.config/deep-survey/server-token）")
    p_serve.add_argument("--allow-unauthenticated", action="store_true", help="显式关闭 Bearer 鉴权（仅建议本机回环或受信任隧道内使用）")

    for mode in ("fast", "deep", "x", "github", "hn"):
        helps = {
            "fast": "五路并发（Codex + Grok X + AGY Google + GitHub + HN）+ 仓库真实性核验 + 交叉汇总",
            "deep": "五路并发 + 二阶段仓库指标核验与 X 深度口碑追查 + 汇总",
            "x": "仅调用 Grok 原生 X 搜索快速摸底推文、讨论串与实时舆情",
            "github": "仅调用 GitHub 官方索引检索仓库与高赞 Issues 并做星数/停更健康度分级",
            "hn": "仅调用 Hacker News 官方索引检索高赞讨论与一线工程师评论",
        }
        p = subs.add_parser(mode, help=helps[mode])
        p.add_argument("query", nargs="?", help="调研问题或主题")
        p.add_argument("--prompt-file", help="从文件读取长调研指令")
        p.add_argument("--channels", help="自定义逗号分隔通道（支持 codex,grok,agy,github,hn 及 xai_api,gemini_api,perplexity,tavily,exa,brave）")
        p.add_argument("--include-oai-api", action="store_true", help="显式额外启用 oai-personal 个人 API 通道（默认关闭）")
        p.add_argument("--out", help="指定最终汇总 Markdown 报告写入路径")
        p.add_argument("--timeout", type=int, default=210, help="单通道最大超时秒数（默认 210）")
        p.add_argument("--wait", type=int, default=360, help="前台等待秒数；设为 0 则立即后台运行并返回 run_id（默认 360）")
        p.add_argument("--detach", action="store_true", help="立即后台运行并输出 run_id，用 status 查询")
        p.add_argument("--print-report", action="store_true", help="完成后直接把最终报告全文打印到 stdout")

    p_status = subs.add_parser("status", help="查询后台或历史调研任务状态")
    p_status.add_argument("run_id", help="任务 run_id（或 'latest'）")
    p_status.add_argument("--wait", type=int, default=0, help="若仍在运行，最多继续等待秒数")
    p_status.add_argument("--print-report", action="store_true", help="若已完成，直接打印报告全文")

    p_list = subs.add_parser("list", help="列出最近的本地调研记录")
    p_list.add_argument("--limit", type=int, default=10)

    p_worker = subs.add_parser("_worker")
    p_worker.add_argument("spec_path")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "doctor":
        print(json.dumps(collect_doctor_data(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "mcp":
        from .mcp_server import serve_stdio
        serve_stdio()
        return 0

    if args.command == "serve":
        from .http_server import serve_http
        return serve_http(
            args.host,
            args.port,
            token=args.token,
            allow_unauthenticated=args.allow_unauthenticated,
        )

    if args.command == "_worker":
        spec = json.loads(Path(args.spec_path).read_text(encoding="utf-8"))
        try:
            execute_survey_run(
                spec["run_id"],
                Path(spec["run_dir"]),
                spec["query"],
                mode=spec["mode"],
                channels=spec["channels"],
                include_oai_api=bool(spec.get("include_oai_api")),
                out_report=Path(spec["out_report"]) if spec.get("out_report") else None,
                timeout_per_channel=int(spec.get("timeout_per_channel") or 210),
            )
            return 0
        except Exception as exc:
            state_file = Path(spec["run_dir"]) / "state.json"
            state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
            state.update({"status": "failed", "error": redact_secrets(exc), "completed_at": utc_now()})
            atomic_write_json(state_file, state)
            return 1

    if args.command == "list":
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        items = []
        for d in sorted(RUNS_DIR.iterdir(), reverse=True):
            sf = d / "state.json"
            if sf.exists():
                try:
                    s = json.loads(sf.read_text(encoding="utf-8"))
                    items.append({
                        "run_id": s.get("run_id", d.name),
                        "status": s.get("status"),
                        "mode": s.get("mode"),
                        "query": (s.get("query") or "")[:80],
                        "report_file": s.get("report_file"),
                    })
                except ValueError:
                    continue
            if len(items) >= args.limit:
                break
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0

    if args.command == "status":
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        if args.run_id == "latest":
            dirs = sorted([d for d in RUNS_DIR.iterdir() if (d / "state.json").exists()], reverse=True)
            if not dirs:
                raise SurveyError("暂无历史调研记录。")
            run_dir = dirs[0]
        else:
            if "/" in args.run_id or ".." in args.run_id:
                raise SurveyError("无效 run_id。")
            run_dir = RUNS_DIR / args.run_id
            if not run_dir.exists():
                raise SurveyError(f"未找到任务：{args.run_id}")
        state = wait_for_run(run_dir, max(0, args.wait))
        if args.print_report and state.get("status") == "completed":
            rf = Path(state.get("report_file") or (run_dir / "report.md"))
            if rf.exists():
                print(rf.read_text(encoding="utf-8"))
                return 0
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0 if state.get("status") != "failed" else 1

    query = Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else (args.query or "")
    query = query.strip()
    if not query:
        raise SurveyError("请提供调研问题或 --prompt-file。")

    run_id = f"{local_stamp()}-{slugify_topic(query)}-{uuid.uuid4().hex[:6]}"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    out_report = Path(args.out).expanduser().resolve() if args.out else None
    channels = resolve_channels_for_mode(args.command, args.channels)

    if args.detach or args.wait == 0:
        pid = spawn_detached_worker(
            run_id,
            run_dir,
            query,
            mode=args.command,
            channels=channels,
            include_oai_api=args.include_oai_api,
            out_report=out_report,
            timeout_per_channel=args.timeout,
        )
        print(json.dumps({
            "run_id": run_id,
            "status": "in_progress",
            "pid": pid,
            "run_dir": str(run_dir),
            "report_file": str(out_report or (run_dir / "report.md")),
            "next": f"deep-survey status {run_id} --wait 60",
        }, ensure_ascii=False, indent=2))
        return 0

    spawn_detached_worker(
        run_id,
        run_dir,
        query,
        mode=args.command,
        channels=channels,
        include_oai_api=args.include_oai_api,
        out_report=out_report,
        timeout_per_channel=args.timeout,
    )
    state = wait_for_run(run_dir, args.wait)
    if state.get("status") == "completed" and args.print_report:
        rf = Path(state.get("report_file") or (run_dir / "report.md"))
        print(rf.read_text(encoding="utf-8"))
        return 0

    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0 if state.get("status") != "failed" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SurveyError, OSError, ValueError) as exc:
        print(f"错误：{redact_secrets(exc)}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("已中断前台等待；后台调研若已启动仍会继续运行，可用 deep-survey status latest 查看。", file=sys.stderr)
        sys.exit(130)
