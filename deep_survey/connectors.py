"""
Pluggable community & API search connectors for Deep Survey (大调查).

1. Zero-key official community index:
   - `hn`: Hacker News Official Algolia API (stories + practitioner comments, calibrated by points & comment counts)

2. Optional API-key connectors (auto-discovered via ~/.config/deep-survey/config.json or environment variables):
   - `xai_api`     (XAI_API_KEY)        -> Official xAI Responses API with server-side x_search + web_search (great for VPS or Grok CLI 402 fallback)
   - `gemini_api`  (GEMINI_API_KEY)     -> Official Google Gemini API with google_search grounding (great for headless Linux VPS)
   - `perplexity`  (PERPLEXITY_API_KEY) -> Perplexity Sonar / Deep Research API
   - `tavily`      (TAVILY_API_KEY)     -> Tavily Search API (advanced search depth)
   - `exa`         (EXA_API_KEY)        -> Exa neural/keyword search API
   - `brave`       (BRAVE_API_KEY)      -> Brave Web Search API
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG_FILE = Path.home() / ".config/deep-survey/config.json"

OPTIONAL_API_SPECS = {
    "xai_api": {
        "env": "XAI_API_KEY",
        "config_key": "xai_api_key",
        "provider": "xAI Official API (Server-side x_search + web_search)",
    },
    "gemini_api": {
        "env": "GEMINI_API_KEY",
        "config_key": "gemini_api_key",
        "provider": "Google Gemini Official API (google_search Grounding)",
    },
    "perplexity": {
        "env": "PERPLEXITY_API_KEY",
        "config_key": "perplexity_api_key",
        "provider": "Perplexity Official Sonar API",
    },
    "tavily": {
        "env": "TAVILY_API_KEY",
        "config_key": "tavily_api_key",
        "provider": "Tavily Search API",
    },
    "exa": {
        "env": "EXA_API_KEY",
        "config_key": "exa_api_key",
        "provider": "Exa Neural Search API",
    },
    "brave": {
        "env": "BRAVE_API_KEY",
        "config_key": "brave_api_key",
        "provider": "Brave Web Search API",
    },
}


def load_user_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_connector_key(channel: str) -> str | None:
    spec = OPTIONAL_API_SPECS.get(channel)
    if not spec:
        return None
    val = os.environ.get(spec["env"])
    if val and val.strip():
        return val.strip()
    cfg = load_user_config()
    val = cfg.get(spec["config_key"])
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def configured_optional_channels() -> list[str]:
    cfg = load_user_config()
    auto_enable = cfg.get("auto_enable_configured_apis", False)
    if not auto_enable:
        return []
    return [ch for ch in OPTIONAL_API_SPECS if get_connector_key(ch)]


def doctor_optional_connectors() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for ch, spec in OPTIONAL_API_SPECS.items():
        has_key = bool(get_connector_key(ch))
        out[ch] = {
            "configured": has_key,
            "env_var": spec["env"],
            "config_key": spec["config_key"],
            "provider": spec["provider"],
        }
    return out


def _http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict | None = None,
    timeout: int = 45,
) -> dict:
    hdrs = {"User-Agent": "deep-survey/1.1", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = (
        text.replace("&#x27;", "'")
        .replace("&quot;", '"')
        .replace("&amp;", "&")
        .replace("&gt;", ">")
        .replace("&lt;", "<")
        .replace("&#x2F;", "/")
    )
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Zero-Key Official Community Index: Hacker News Algolia API (`hn`)
# ---------------------------------------------------------------------------

def classify_hn_traction(points: int, comments: int) -> str:
    if points >= 150 or comments >= 80:
        return f"🔥 HN 高热讨论 ({points} pts · {comments} 评论)"
    if points >= 30 or comments >= 15:
        return f"✅ 社区有效讨论 ({points} pts · {comments} 评论)"
    if points >= 8 or comments >= 3:
        return f"🧩 小范围讨论 ({points} pts · {comments} 评论)"
    return f"⚠️ 极低热度 ({points} pts · {comments} 评论；缺乏社区验证)"


def run_hn_channel(queries: list[str], *, depth: str = "fast", timeout: int = 25) -> dict:
    started = time.monotonic()
    stories_by_id: dict[str, dict] = {}
    comments_list: list[dict] = []
    seen_comments: set[str] = set()

    per_query = 8 if depth == "deep" else 5
    for q in queries[:3]:
        q_enc = urllib.parse.quote_plus(q)
        try:
            s_data = _http_json(
                f"https://hn.algolia.com/api/v1/search?query={q_enc}&tags=story&hitsPerPage={per_query}",
                timeout=min(12, timeout),
            )
            for h in s_data.get("hits") or []:
                oid = str(h.get("objectID") or "")
                if oid and oid not in stories_by_id:
                    stories_by_id[oid] = h
        except Exception:
            pass

        try:
            c_data = _http_json(
                f"https://hn.algolia.com/api/v1/search?query={q_enc}&tags=comment&hitsPerPage=4",
                timeout=min(12, timeout),
            )
            for c in c_data.get("hits") or []:
                oid = str(c.get("objectID") or "")
                txt = _strip_html(c.get("comment_text") or "")
                if oid and oid not in seen_comments and len(txt) >= 60:
                    seen_comments.add(oid)
                    comments_list.append({
                        "id": oid,
                        "author": c.get("author") or "anon",
                        "story_title": c.get("story_title") or "",
                        "created_at": (c.get("created_at") or "")[:10],
                        "url": f"https://news.ycombinator.com/item?id={oid}",
                        "excerpt": txt[:320],
                    })
        except Exception:
            pass

    stories = sorted(
        stories_by_id.values(),
        key=lambda x: (int(x.get("points") or 0) + int(x.get("num_comments") or 0) * 2),
        reverse=True,
    )

    lines = [
        "# Hacker News 官方索引与社区热度校准",
        f"- 检索词组：{', '.join(f'`{q}`' for q in queries[:3])}",
        f"- 命中帖子：{len(stories)} 条；一线工程师评论摘录：{len(comments_list)} 条",
        "",
        "## 一、HN 帖子与热度分级",
    ]
    sources: list[dict] = []
    if not stories:
        lines.append("未检索到直接匹配的 HN 帖子。")
    else:
        for s in stories[:12]:
            oid = s.get("objectID")
            hn_url = f"https://news.ycombinator.com/item?id={oid}"
            ext_url = s.get("url") or hn_url
            pts = int(s.get("points") or 0)
            coms = int(s.get("num_comments") or 0)
            date_s = (s.get("created_at") or "")[:10]
            badge = classify_hn_traction(pts, coms)
            title = (s.get("title") or "Untitled").strip()
            lines.append(f"- **[{title}]({hn_url})** — {badge} · 日期: `{date_s}` · [原文链接]({ext_url})")
            sources.append({"title": f"HN: {title}", "url": hn_url})

    if comments_list:
        lines.extend(["", "## 二、HN 评论区实战反馈摘录"])
        for c in comments_list[:8]:
            lines.append(
                f"- **@{c['author']}** (`{c['created_at']}`, 讨论串《{c['story_title']}》, [链接]({c['url']}))：{c['excerpt']}"
            )
            sources.append({"title": f"HN comment by {c['author']}", "url": c["url"]})

    return {
        "channel": "hn",
        "provider": "Hacker News Algolia Official Index (Points + Comments Calibration)",
        "status": "completed",
        "verified_native_search": True,
        "stories_count": len(stories),
        "comments_count": len(comments_list),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "sources": sources,
        "text": "\n".join(lines),
    }


# ---------------------------------------------------------------------------
# Optional API Connectors (xAI API / Gemini API / Tavily / Exa / Perplexity / Brave)
# ---------------------------------------------------------------------------

def run_optional_api_channel(channel: str, query: str, *, depth: str = "fast", timeout: int = 120) -> dict:
    started = time.monotonic()
    api_key = get_connector_key(channel)
    spec = OPTIONAL_API_SPECS.get(channel, {})
    prov = spec.get("provider", channel)
    if not api_key:
        return {
            "channel": channel,
            "provider": prov,
            "status": "unavailable",
            "error": f"未配置 {spec.get('env', channel)}（可在 ~/.config/deep-survey/config.json 或环境变量中配置）",
        }

    try:
        if channel == "xai_api":
            cfg = load_user_config()
            model = cfg.get("xai_model") or "grok-4"
            payload = {
                "model": model,
                "input": [
                    {
                        "role": "user",
                        "content": (
                            f"请调用 x_search 和 web_search 深入调研以下主题，重点给出 X 平台上的真实开发者反馈、"
                            f"具体帖子链接 (https://x.com/...)、口碑分化与踩坑细节，用简体中文输出：\n\n{query}"
                        ),
                    }
                ],
                "tools": [{"type": "x_search"}, {"type": "web_search"}],
            }
            res = _http_json(
                "https://api.x.ai/v1/responses",
                method="POST",
                headers={"Authorization": f"Bearer {api_key}"},
                payload=payload,
                timeout=timeout,
            )
            texts = []
            urls = []
            for item in res.get("output") or []:
                for c in item.get("content") or []:
                    if c.get("type") == "output_text" and c.get("text"):
                        texts.append(c["text"])
                    for ann in c.get("annotations") or []:
                        if ann.get("url"):
                            urls.append(ann["url"])
            body = "\n\n".join(texts).strip()
            return {
                "channel": channel,
                "provider": prov,
                "status": "completed" if body else "failed",
                "verified_native_search": True,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "sources": [{"title": u, "url": u} for u in dict.fromkeys(urls)],
                "text": body,
                "error": "" if body else "xAI API 返回空内容",
            }

        if channel == "gemini_api":
            cfg = load_user_config()
            model = cfg.get("gemini_model") or "gemini-2.5-pro"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            payload = {
                "contents": [{"parts": [{"text": f"请联网全面检索并用简体中文输出带真实来源链接的结构化调研笔记：\n\n{query}"}]}],
                "tools": [{"google_search": {}}],
            }
            res = _http_json(
                url,
                method="POST",
                headers={"x-goog-api-key": api_key},
                payload=payload,
                timeout=timeout,
            )
            cand = (res.get("candidates") or [{}])[0]
            parts = (cand.get("content") or {}).get("parts") or []
            body = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
            sources = []
            chunks = (cand.get("groundingMetadata") or {}).get("groundingChunks") or []
            for ch_item in chunks:
                web = ch_item.get("web") or {}
                if web.get("uri"):
                    sources.append({"title": web.get("title") or web["uri"], "url": web["uri"]})
            if sources:
                body += "\n\n### Grounding 来源\n" + "\n".join(f"- [{s['title']}]({s['url']})" for s in sources)
            return {
                "channel": channel,
                "provider": prov,
                "status": "completed" if body else "failed",
                "verified_native_search": len(sources) > 0,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "sources": sources,
                "text": body,
            }

        if channel == "tavily":
            res = _http_json(
                "https://api.tavily.com/search",
                method="POST",
                payload={
                    "api_key": api_key,
                    "query": query[:400],
                    "search_depth": "advanced",
                    "include_answer": True,
                    "max_results": 10 if depth == "deep" else 6,
                },
                timeout=min(45, timeout),
            )
            lines = ["# Tavily 深度网页检索结果"]
            if res.get("answer"):
                lines.extend(["## 摘要", res["answer"], ""])
            lines.append("## 命中网页")
            sources = []
            for r in res.get("results") or []:
                lines.append(f"- **[{r.get('title')}]({r.get('url')})** (score: {r.get('score')})\n  - {(r.get('content') or '')[:350]}")
                if r.get("url"):
                    sources.append({"title": r.get("title") or r["url"], "url": r["url"]})
            return {
                "channel": channel,
                "provider": prov,
                "status": "completed",
                "verified_native_search": True,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "sources": sources,
                "text": "\n".join(lines),
            }

        if channel == "exa":
            res = _http_json(
                "https://api.exa.ai/search",
                method="POST",
                headers={"x-api-key": api_key},
                payload={
                    "query": query[:400],
                    "numResults": 10 if depth == "deep" else 6,
                    "contents": {"text": {"maxCharacters": 800}},
                },
                timeout=min(45, timeout),
            )
            lines = ["# Exa 神经语义检索结果"]
            sources = []
            for r in res.get("results") or []:
                lines.append(f"- **[{r.get('title') or r.get('url')}]({r.get('url')})** (`{(r.get('publishedDate') or '')[:10]}`)\n  - {(r.get('text') or '')[:380]}")
                if r.get("url"):
                    sources.append({"title": r.get("title") or r["url"], "url": r["url"]})
            return {
                "channel": channel,
                "provider": prov,
                "status": "completed",
                "verified_native_search": True,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "sources": sources,
                "text": "\n".join(lines),
            }

        if channel == "perplexity":
            cfg = load_user_config()
            model = cfg.get("perplexity_model") or ("sonar-deep-research" if depth == "deep" else "sonar-pro")
            res = _http_json(
                "https://api.perplexity.ai/chat/completions",
                method="POST",
                headers={"Authorization": f"Bearer {api_key}"},
                payload={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "请用简体中文提供带可核查引用的深度调研笔记。"},
                        {"role": "user", "content": query},
                    ],
                },
                timeout=timeout,
            )
            choice = ((res.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            cits = res.get("citations") or []
            if cits:
                choice += "\n\n### Perplexity 引用来源\n" + "\n".join(f"- {u}" for u in cits)
            return {
                "channel": channel,
                "provider": prov,
                "status": "completed" if choice else "failed",
                "verified_native_search": True,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "sources": [{"title": u, "url": u} for u in cits],
                "text": choice,
            }

        if channel == "brave":
            q_enc = urllib.parse.quote_plus(query[:300])
            res = _http_json(
                f"https://api.search.brave.com/res/v1/web/search?q={q_enc}&count=8",
                headers={"X-Subscription-Token": api_key},
                timeout=min(30, timeout),
            )
            results = (res.get("web") or {}).get("results") or []
            lines = ["# Brave 独立索引检索结果"]
            sources = []
            for r in results:
                lines.append(f"- **[{r.get('title')}]({r.get('url')})** (`{r.get('age') or ''}`)\n  - {(r.get('description') or '')[:320]}")
                if r.get("url"):
                    sources.append({"title": r.get("title") or r["url"], "url": r["url"]})
            return {
                "channel": channel,
                "provider": prov,
                "status": "completed",
                "verified_native_search": True,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "sources": sources,
                "text": "\n".join(lines),
            }

        return {"channel": channel, "provider": prov, "status": "unavailable", "error": f"未实现的连接器: {channel}"}
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="ignore")[:240]
        except Exception:
            pass
        return {
            "channel": channel,
            "provider": prov,
            "status": "quota_exhausted" if exc.code in {402, 429} else "failed",
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "error": f"HTTP {exc.code}: {body}",
        }
    except Exception as exc:
        return {
            "channel": channel,
            "provider": prov,
            "status": "failed",
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "error": str(exc)[:240],
        }
