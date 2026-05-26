#!/usr/bin/env python3
"""AI News & Vocabulary Sync — fetch, curate, write to Feishu Docx + send IM digest daily.

AI backend (auto-detected):
  - ANTHROPIC_API_KEY set → Claude API
  - GH_PAT set              → GitHub Models free tier (GPT-4o-mini)
  At least one must be configured.
"""

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ──────────────────────────────────────────────────
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET")
FEISHU_DOC_TOKEN = os.getenv("FEISHU_DOC_TOKEN")
FEISHU_USER_OPEN_ID = os.getenv("FEISHU_USER_OPEN_ID", "")
GH_PAT = os.getenv("GH_PAT")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

CST = timezone(timedelta(hours=8))

# Feishu Docx block types
BLOCK_TEXT = 2
BLOCK_HEADING1 = 3
BLOCK_HEADING2 = 4
BLOCK_HEADING3 = 5
BLOCK_DIVIDER = 22

# ── Block builders ──────────────────────────────────────────

def _el(content, bold=False):
    el = {"text_run": {"content": content}}
    if bold:
        el["text_run"]["text_element_style"] = {"bold": True}
    return el

def block_text(content, bold=False):
    return {"block_type": BLOCK_TEXT, "text": {"elements": [_el(content, bold=bold)]}}

def block_h1(content):
    return {"block_type": BLOCK_HEADING1, "heading1": {"elements": [_el(content)]}}

def block_h2(content):
    return {"block_type": BLOCK_HEADING2, "heading2": {"elements": [_el(content)]}}

def block_h3(content):
    return {"block_type": BLOCK_HEADING3, "heading3": {"elements": [_el(content)]}}

def block_divider():
    return {"block_type": BLOCK_DIVIDER, "divider": {}}


# ── Step 1: News fetching ───────────────────────────────────

def fetch_ai_news():
    """Fetch AI news from multiple free sources. Combine & deduplicate.
    Only falls back to mock data if ALL sources fail or return < 3 items."""
    all_items = []
    sources = [
        ("HN Algolia", fetch_hn_algolia),
        ("HN Top AI", fetch_hn_top_ai),
        ("Google News", fetch_google_news_rss),
    ]
    for name, fn in sources:
        try:
            items = fn()
            print(f"      {name}: {len(items)} stories")
            all_items.extend(items)
        except Exception as exc:
            print(f"      {name}: FAILED ({exc})")

    if not all_items:
        print("      All sources failed, using mock data")
        return get_mock_news()

    # Deduplicate by title similarity
    unique = _deduplicate(all_items)
    unique.sort(key=lambda x: x["score"], reverse=True)

    if len(unique) < 3:
        print(f"      Only {len(unique)} unique real stories, supplementing with mock")
        unique.extend(get_mock_news()[:15])

    print(f"      Total unique: {len(unique)}")
    return unique[:30]


def fetch_hn_algolia():
    """HN Algolia search for AI stories from last 3 days."""
    since = int((datetime.now(CST) - timedelta(days=3)).timestamp())
    resp = requests.get("https://hn.algolia.com/api/v1/search_by_date", params={
        "query": "AI OR LLM OR machine learning OR artificial intelligence OR GPT",
        "tags": "story",
        "hitsPerPage": 40,
        "numericFilters": f"created_at_i>{since}",
    }, timeout=15)
    resp.raise_for_status()
    return [
        {"title": h["title"],
         "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
         "score": h.get("points", 0)}
        for h in resp.json().get("hits", [])
    ]


def fetch_hn_top_ai():
    """HN top 100 stories, filter for AI-related, keep top 30 by score."""
    ai_kw = re.compile(
        r"\b(ai|llm|gpt|openai|anthropic|claude|gemini|deepmind|machine.learning|"
        r"deep.learning|neural|transformer|language.model|diffusion|chatgpt|copilot|"
        r"agent|rag|embedding|vector|nvidia|gpu|pytorch|tensorflow|llama|mistral|"
        r"deepseek|stable.diffusion|sora|robot|agi|token|inference|fine.tun|rlhf|"
        r"alignment|grok|safety|autonomous|humanoid|model|data|research|science)\b", re.I
    )
    resp = requests.get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=10)
    story_ids = resp.json()[:100]

    def _fetch(sid):
        try:
            d = requests.get(
                f"https://hacker-news.firebaseio.com/v0/item/{sid}.json", timeout=5
            ).json()
            if d and d.get("title"):
                return {"title": d["title"], "url": d.get("url", ""), "score": d.get("score", 0)}
        except Exception:
            pass
        return None

    stories = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for f in as_completed([ex.submit(_fetch, sid) for sid in story_ids]):
            r = f.result()
            if r:
                stories.append(r)

    ai_stories = [s for s in stories if ai_kw.search(s["title"])]
    ai_stories.sort(key=lambda x: x["score"], reverse=True)
    return ai_stories[:30]


def fetch_google_news_rss():
    """Google News RSS for AI topics. Returns parsed headlines."""
    query = quote_plus("artificial intelligence technology")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()

    root = ET.fromstring(resp.text)
    items = []
    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        if title_el is not None and title_el.text:
            items.append({
                "title": title_el.text.rsplit(" - ", 1)[0],  # strip source suffix
                "url": link_el.text if link_el is not None else "",
                "score": 50,
            })
    return items[:25]


def _deduplicate(items):
    """Remove near-duplicate titles (simple prefix match)."""
    seen = set()
    unique = []
    for item in sorted(items, key=lambda x: x["score"], reverse=True):
        # Use first 50 chars as dedup key
        key = item["title"][:50].lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def get_mock_news():
    """Fallback headlines — only used when all live sources fail."""
    return [
        {"title": "OpenAI Ships GPT-5 with Native Multimodal Reasoning Across Text, Image, and Audio", "url": "", "score": 100},
        {"title": "Anthropic Claude Opus 4.7 Sets New Standard with 500K Token Context and Tool Use", "url": "", "score": 98},
        {"title": "Google DeepMind Gemini 3 Achieves Breakthrough on Protein Folding Benchmarks", "url": "", "score": 95},
        {"title": "Meta Releases Llama 4 Family Under Open Weights License", "url": "", "score": 92},
        {"title": "NVIDIA Blackwell Ultra GPU Delivers 4x Training Throughput for Large-Scale AI", "url": "", "score": 90},
        {"title": "EU AI Act Implementation: Key Compliance Deadlines and Industry Impact", "url": "", "score": 88},
        {"title": "Tesla Optimus Gen-3 Humanoid Robot Begins Factory Pilot Deployments", "url": "", "score": 85},
        {"title": "AI Video Platform Sora 2.0 Opens Public Access with Advanced Editing", "url": "", "score": 83},
        {"title": "Microsoft Launches AI Copilot for Scientific Research", "url": "", "score": 80},
        {"title": "Stanford AI Lab Demonstrates First End-to-End AI Drug Discovery Pipeline", "url": "", "score": 78},
        {"title": "Apple Intelligence Platform Expands API Access for Enterprise Developers", "url": "", "score": 75},
        {"title": "DeepSeek-V4 Open Source MoE Model Matches Proprietary Leaders on Key Benchmarks", "url": "", "score": 73},
        {"title": "AI Code Review Becomes Mandatory at 60% of Fortune 500 Software Teams", "url": "", "score": 70},
        {"title": "Global AI Safety Treaty Signed by 45 Nations at Geneva Summit", "url": "", "score": 68},
        {"title": "MIT CSAIL Breakthrough Cuts AI Training Energy by 60% with Novel Sparsity Algorithm", "url": "", "score": 65},
    ]


# ── Step 2: AI Curation ─────────────────────────────────────

def curate_news(news_items):
    """Select top 10 news + 10 key terms via AI. Auto-picks free or paid backend."""
    prompt = _build_curation_prompt(news_items)

    if ANTHROPIC_API_KEY:
        print("  [AI] Using Anthropic Claude API")
        result = _call_claude(prompt)
    elif GH_PAT:
        print("  [AI] Using GitHub Models (free tier)")
        result = _call_github_models(prompt)
    else:
        raise SystemExit("Neither ANTHROPIC_API_KEY nor GH_PAT is set.")

    return _extract_json(result)


def _build_curation_prompt(items):
    today = datetime.now(CST).strftime("%Y-%m-%d")
    news_text = "\n".join(
        f"{i+1}. {n['title']}" + (f" — {n['url']}" if n.get("url") else "")
        for i, n in enumerate(items)
    )
    return f"""You are a senior AI industry analyst. Process these AI news headlines for {today}.

TASK 1 — Select the TOP 10 most impactful AI news. For each return:
  "title": refined English title (concise, professional)
  "summary_cn": one Chinese sentence on WHY this matters

TASK 2 — Extract 10 cutting-edge AI terms from these 10 stories. For each:
  "english": the term
  "chinese": accurate Chinese translation
  "definition": one clear English glossary sentence
  "desc_cn": one Chinese sentence explaining the term in plain language for a non-technical reader

Candidates ({len(items)} items):
{news_text}

Return ONLY a JSON object (no markdown, no extra text):
{{"news":[{{"title":"...","summary_cn":"..."}}],"terms":[{{"english":"...","chinese":"...","definition":"...","desc_cn":"..."}}]}}"""


def _call_claude(prompt):
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        temperature=0.3,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


def _call_github_models(prompt):
    model = os.getenv("GITHUB_MODEL", "gpt-4o-mini")
    resp = requests.post(
        "https://models.inference.ai.azure.com/chat/completions",
        headers={
            "Authorization": f"Bearer {GH_PAT}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "You return only valid JSON. No markdown, no explanation."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 8192,
            "response_format": {"type": "json_object"},
        },
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _extract_json(text):
    text = text.strip()
    for fence in ("```json", "```"):
        i = text.find(fence)
        if i != -1:
            text = text[i + len(fence):]
            if text.endswith("```"):
                text = text[:-3]
            break
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


# ── Step 3: Feishu API ──────────────────────────────────────

def feishu_get_token():
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=15,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"Feishu auth error {data.get('code')}: {data.get('msg')}")
    return data["tenant_access_token"]


def feishu_get_doc(token, doc_id):
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(
        f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}",
        headers=headers, timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"Get doc error {data.get('code')}: {data.get('msg')}")
    return data["data"]["document"]


def feishu_append_blocks(token, doc_id, parent_block_id, blocks, batch_size=15):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    for i in range(0, len(blocks), batch_size):
        batch = blocks[i:i + batch_size]
        body = {"children": batch, "index": -1}
        resp = requests.post(
            f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/{parent_block_id}/children",
            headers=headers, json=body, timeout=60,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"Append blocks error (batch {i // batch_size + 1}): {data.get('code')}: {data.get('msg')}")
        time.sleep(0.3)
    return {"ok": True, "batches": (len(blocks) + batch_size - 1) // batch_size}


def feishu_send_im(token, open_id, curated):
    """Send a post-type rich message to the user via IM."""
    today = datetime.now(CST).strftime("%Y-%m-%d")

    # Build post content — each news item as a rich text block
    news_blocks = []
    for i, item in enumerate(curated["news"], 1):
        news_blocks.append([
            {"tag": "text", "text": f"{i}. "},
            {"tag": "text", "text": item["title"], "style": ["bold"]},
        ])
        news_blocks.append([
            {"tag": "text", "text": f"    {item['summary_cn']}"},
        ])

    term_blocks = []
    for term in curated["terms"]:
        term_blocks.append([
            {"tag": "text", "text": f"{term['english']} — {term['chinese']}", "style": ["bold"]},
        ])
        term_blocks.append([
            {"tag": "text", "text": term["desc_cn"]},
        ])

    content = {
        "zh_cn": {
            "title": f"AI 行业日报 — {today}",
            "content": [
                [{"tag": "text", "text": "今日 Top 10 AI 行业新闻", "style": ["bold"]}],
                *news_blocks,
                [{"tag": "text", "text": ""}],
                [{"tag": "text", "text": "今日 10 大核心 AI 词汇", "style": ["bold"]}],
                *term_blocks,
            ]
        }
    }

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        "receive_id": open_id,
        "msg_type": "post",
        "content": json.dumps(content, ensure_ascii=False),
    }
    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
        headers=headers, json=body, timeout=15,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"IM send error {data.get('code')}: {data.get('msg')}")
    return data


# ── Step 4: Build document blocks ───────────────────────────

def build_blocks(curated):
    today = datetime.now(CST).strftime("%Y-%m-%d")
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M CST")
    blocks = []

    blocks.append(block_h1(f"AI 行业日报 — {today}"))
    blocks.append(block_text(f"自动生成 · {now}"))
    blocks.append(block_divider())

    blocks.append(block_h2("今日 Top 10 最具价值 AI 行业新闻"))
    for i, item in enumerate(curated["news"], 1):
        blocks.append(block_text(f"{i}. {item['title']}", bold=True))
        blocks.append(block_text(f"   {item['summary_cn']}"))
    blocks.append(block_divider())

    blocks.append(block_h2("今日 10 大核心 AI 词汇"))
    for term in curated["terms"]:
        blocks.append(block_h3(f"{term['english']} — {term['chinese']}"))
        blocks.append(block_text(term["definition"]))
        blocks.append(block_text(f"   {term['desc_cn']}"))
    blocks.append(block_divider())

    return blocks


# ── Main ────────────────────────────────────────────────────

def main():
    t0 = time.time()
    print(f"╔══════════════════════════════════════╗")
    print(f"║  AI News Sync — {datetime.now(CST).strftime('%Y-%m-%d %H:%M CST'):<20s}║")
    print(f"╚══════════════════════════════════════╝")

    missing = []
    for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_DOC_TOKEN"):
        if not os.getenv(k):
            missing.append(k)
    if not ANTHROPIC_API_KEY and not GH_PAT:
        missing.append("ANTHROPIC_API_KEY or GH_PAT")
    if missing:
        raise SystemExit(f"Missing env vars: {', '.join(missing)}")

    # [1] Fetch
    print("\n[1/5] Fetching AI news...")
    candidates = fetch_ai_news()
    print(f"      {len(candidates)} candidates → curating...")

    # [2] Curate
    print("\n[2/5] AI curation...")
    curated = curate_news(candidates)
    print(f"      {len(curated['news'])} news + {len(curated['terms'])} terms")

    # [3] Build
    print("\n[3/5] Building Feishu blocks...")
    blocks = build_blocks(curated)
    print(f"      {len(blocks)} blocks")

    # [4] Write to Docx
    print("\n[4/5] Writing to Feishu Docx...")
    token = feishu_get_token()
    doc = feishu_get_doc(token, FEISHU_DOC_TOKEN)
    feishu_append_blocks(token, FEISHU_DOC_TOKEN, doc["document_id"], blocks)
    print(f"      Appended to {FEISHU_DOC_TOKEN}")

    # [5] Send IM
    print("\n[5/5] Sending IM digest...")
    if FEISHU_USER_OPEN_ID:
        try:
            feishu_send_im(token, FEISHU_USER_OPEN_ID, curated)
            print(f"      Sent to {FEISHU_USER_OPEN_ID}")
        except Exception as exc:
            print(f"      IM send failed: {exc}")
            print(f"      (need 'im:message' permission on the Feishu app)")
    else:
        print("      Skipped (FEISHU_USER_OPEN_ID not set)")

    print(f"\n{'─' * 40}")
    print(f"Done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
