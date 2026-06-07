"""
Config-driven news collection pipeline for HCSA's News Report.

The collector keeps the current free/static architecture:
GitHub Actions -> RSS/Google News RSS -> JSON -> GitHub Pages.

Quality upgrades included here:
- sector/source/keyword policy is loaded from config/news_config.json
- source credibility, recency, relevance, and market-impact scoring
- URL/title/issue-key based duplicate removal
- optional article-body extraction with local one-line Korean summaries
- no required AI/API dependency, but summarizer boundaries are ready for later providers
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable
from urllib.parse import quote_plus, urlparse

import certifi
import feedparser
import requests
from dateutil import parser as dateparser

try:
    import trafilatura
except Exception:  # The collector still works without body extraction.
    trafilatura = None

ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=certifi.where())

KST = timezone(timedelta(hours=9))
UTC = timezone.utc
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT_DIR, "config", "news_config.json")
OUTPUT_PATH = os.path.join(ROOT_DIR, "docs", "data", "news_latest.json")
GEMINI_USAGE_PATH = os.path.join(ROOT_DIR, "docs", "data", "gemini_usage.json")
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={ceid}"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; HCSA-NewsReport/2.0; +https://github.com/hocheolsa/news-dashboard)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/rss+xml;q=0.8,*/*;q=0.5",
})


@dataclass(frozen=True)
class Group:
    sector: str
    category: str | None
    target: int
    queries: list[str]
    include: list[str]
    priority: list[str]
    source_tags: list[str]
    exclude: list[str]


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


CONFIG = load_config()
GROUPS = [
    Group(
        sector=item["sector"],
        category=item.get("category"),
        target=int(item["target"]),
        queries=item["queries"],
        include=item["include"],
        priority=item["priority"],
        source_tags=item.get("source_tags", []),
        exclude=item.get("exclude", []),
    )
    for item in CONFIG["groups"]
]
EXPECTED_TOTAL = sum(group.target for group in GROUPS)
GEMINI_LAST_CALL_AT = 0.0


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_kst_iso(dt: datetime) -> str:
    return dt.astimezone(KST).isoformat(timespec="seconds")


def normalize_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<script[\s\S]*?</script>", " ", value, flags=re.I)
    value = re.sub(r"<style[\s\S]*?</style>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def has_korean(text: str) -> bool:
    return bool(re.search(r"[가-힣]", text or ""))


def mostly_english(text: str) -> bool:
    letters = re.findall(r"[A-Za-z가-힣]", text or "")
    if not letters:
        return False
    english = sum(1 for char in letters if re.match(r"[A-Za-z]", char))
    return english / len(letters) > 0.65


def contains_any(text: str, keywords: Iterable[str]) -> bool:
    lowered = (text or "").lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def keyword_hits(text: str, keywords: Iterable[str]) -> int:
    lowered = (text or "").lower()
    return sum(1 for keyword in keywords if keyword.lower() in lowered)


def parse_datetime(entry) -> datetime | None:
    for key in ("published", "updated", "created"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            dt = dateparser.parse(raw)
        except Exception:
            try:
                dt = parsedate_to_datetime(raw)
            except Exception:
                continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime.fromtimestamp(time.mktime(parsed), UTC)
    return None


def google_rss_url(query: str, korean: bool = True) -> str:
    if korean:
        return GOOGLE_NEWS_RSS.format(query=quote_plus(query), hl="ko", gl="KR", ceid="KR:ko")
    return GOOGLE_NEWS_RSS.format(query=quote_plus(query), hl="en", gl="US", ceid="US:en")


def fetch_feed(url: str):
    response = SESSION.get(url, timeout=10)
    response.raise_for_status()
    return feedparser.parse(response.content)


def entry_source(feed, entry, fallback: str = "") -> str:
    source = entry.get("source")
    if isinstance(source, dict) and source.get("title"):
        return normalize_text(source.get("title"))
    return normalize_text(fallback or feed.feed.get("title", "Unknown"))


def strip_source_suffix(title: str, source: str) -> str:
    cleaned = title
    source_bits = [
        source,
        source.replace(" - Google 뉴스", ""),
        source.replace(" - Google News", ""),
    ]
    for bit in source_bits:
        bit = bit.strip()
        if bit:
            cleaned = re.sub(rf"\s+-\s+{re.escape(bit)}$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+-\s+[^-]{2,45}$", "", cleaned)
    return cleaned.strip() or title


def light_translate(text: str) -> str:
    translated = text
    for pattern, replacement in CONFIG["translation_replacements"]:
        translated = re.sub(pattern, replacement, translated, flags=re.I)
    return re.sub(r"\s+", " ", translated).strip()


def title_topic(sector: str, category: str | None) -> str:
    topics = {
        "AI": "AI 모델·서비스",
        "Tech": "양자·반도체·메모리 기술",
        "사이버보안": "사이버보안",
        "국내 경제·증시": "국내 경제·증시",
        "국제 경제·증시": "글로벌 경제·증시",
    }
    return topics.get(sector, category or "산업별 증시")


def extract_entity(text: str, source: str) -> str:
    for name in CONFIG["known_entities"]:
        if name.lower() in f"{text} {source}".lower():
            return name
    source = re.sub(r"\s+-\s+Google (뉴스|News)$", "", source).strip()
    return source.split("|")[0].strip() or "주요 매체"


def koreanize_title(title: str, sector: str, category: str | None, source: str) -> str:
    translated = light_translate(title)
    if not mostly_english(translated):
        return translated
    return f"{extract_entity(translated, source)}, {title_topic(sector, category)} 관련 최신 소식"


def normalize_title_key(title: str) -> str:
    text = normalize_text(title).lower()
    text = re.sub(r"[^\w가-힣一-龥ぁ-んァ-ン ]+", " ", text)
    stopwords = set(CONFIG["dedupe_stopwords"])
    words = [word for word in text.split() if len(word) > 1 and word not in stopwords]
    return " ".join(words[:12])


def domain_key(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host.replace("www.", "")


def stable_url_key(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.netloc:
        return url
    clean = f"{parsed.netloc.lower()}{parsed.path.rstrip('/')}"
    return hashlib.sha1(clean.encode("utf-8")).hexdigest()[:16]


def extract_issue_key(article: dict, group: Group) -> str:
    text = f"{article['title']} {article.get('body_text', '')} {article.get('rss_summary', '')}"
    entity = extract_entity(text, article["source"]).lower()
    matched_event = next(
        (word.lower() for word in CONFIG["event_keywords"] if word.lower() in text.lower()),
        "",
    )
    topic_hits = [word.lower() for word in group.include if word.lower() in text.lower()]
    topic = "-".join(topic_hits[:2]) or normalize_title_key(article["title"]).split(" ")[0]
    day = article["published_dt"].astimezone(KST).strftime("%Y-%m-%d")
    return f"{group.category or group.sector}:{entity}:{matched_event}:{topic}:{day}"


def source_tier(source: str, url: str) -> str:
    haystack = f"{source} {domain_key(url)}".lower()
    for tier_name, patterns in CONFIG["source_tiers"].items():
        if any(pattern.lower() in haystack for pattern in patterns):
            return tier_name
    return "general"


def source_bonus(source: str, url: str) -> int:
    tier = source_tier(source, url)
    return int(CONFIG["scoring"]["source_bonus"].get(tier, 0))


def recency_bonus(published_dt: datetime, now: datetime) -> int:
    age_hours = max((now - published_dt).total_seconds() / 3600, 0)
    if age_hours <= 3:
        return 8
    if age_hours <= 6:
        return 6
    if age_hours <= 12:
        return 4
    if age_hours <= 18:
        return 2
    return 1


def article_body(url: str) -> str:
    if not url or trafilatura is None:
        return ""
    try:
        response = SESSION.get(url, timeout=8)
        if not response.ok or not response.text:
            return ""
        text = trafilatura.extract(
            response.text,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
    except Exception:
        return ""
    return normalize_text(text or "")[:5000]


def sentence_candidates(text: str) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []
    sentences = re.split(r"(?<=[.!?。！？])\s+|(?<=[다요죠음임함됨됨])\.\s*", text)
    return [sentence.strip(" -·") for sentence in sentences if 28 <= len(sentence.strip()) <= 240]


def local_summary(article: dict, group: Group) -> str:
    text = article.get("body_text") or article.get("rss_summary") or ""
    candidates = sentence_candidates(light_translate(text))
    ranked = sorted(
        candidates,
        key=lambda sentence: (
            keyword_hits(sentence, group.priority) * 5
            + keyword_hits(sentence, group.include) * 3
            + keyword_hits(sentence, CONFIG["event_keywords"]) * 4
            + (2 if has_korean(sentence) else 0)
        ),
        reverse=True,
    )
    if ranked and (has_korean(ranked[0]) or not mostly_english(ranked[0])):
        summary = ranked[0]
    else:
        entity = extract_entity(article["title"], article["source"])
        topic = title_topic(group.sector, group.category)
        event = next((word for word in group.priority if word.lower() in f"{article['title']} {text}".lower()), "")
        if "관련 최신 소식" in article["title"]:
            summary = f"{article['source']}가 다룬 {entity}의 {topic} 소식으로, 관련 산업과 시장 흐름을 점검할 필요가 있습니다."
        elif has_korean(article["title"]) and len(article["title"]) >= 18:
            if event:
                summary = f"{article['title']} 이슈가 보도되며 {topic} 분야의 {event} 영향과 후속 흐름을 확인할 필요가 있습니다."
            else:
                summary = f"{article['title']} 이슈가 보도되며 {topic} 분야의 최신 흐름과 시장 영향을 점검할 필요가 있습니다."
        else:
            event = event or "주요"
            summary = f"{entity} 관련 {topic} 기사로, {event} 이슈와 시장·산업 영향을 함께 확인할 필요가 있습니다."
    summary = re.sub(r"\s+", " ", summary).strip()
    if len(summary) > 138:
        summary = summary[:138].rstrip() + "..."
    return summary


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def ai_config() -> dict:
    config = CONFIG.get("gemini", {}).copy()
    config["enabled"] = env_bool("ENABLE_GEMINI", bool(config.get("enabled", False)))
    config["model"] = os.getenv("GEMINI_MODEL", config.get("model", "gemini-2.5-flash-lite"))
    config["api_key"] = os.getenv("GEMINI_API_KEY", "")
    return config


def kst_day(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%Y-%m-%d")


def load_gemini_usage(now: datetime) -> dict:
    today = kst_day(now)
    try:
        with open(GEMINI_USAGE_PATH, "r", encoding="utf-8") as f:
            usage = json.load(f)
    except Exception:
        usage = {}
    if usage.get("date") != today:
        return {
            "date": today,
            "requests": 0,
            "estimated_input_tokens": 0,
            "estimated_output_tokens": 0,
            "skipped": 0,
            "errors": 0,
            "model": "",
        }
    usage.setdefault("requests", 0)
    usage.setdefault("estimated_input_tokens", 0)
    usage.setdefault("estimated_output_tokens", 0)
    usage.setdefault("skipped", 0)
    usage.setdefault("errors", 0)
    return usage


def save_gemini_usage(usage: dict) -> None:
    os.makedirs(os.path.dirname(GEMINI_USAGE_PATH), exist_ok=True)
    with open(GEMINI_USAGE_PATH, "w", encoding="utf-8") as f:
        json.dump(usage, f, ensure_ascii=False, indent=2)
        f.write("\n")


def estimate_tokens(text: str) -> int:
    # Conservative approximation for Korean/English mixed prompts.
    return max(1, int(len(text) / 3.2))


def trim_text(text: str, limit: int) -> str:
    text = normalize_text(text)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def can_use_gemini(config: dict, usage: dict, prompt: str) -> tuple[bool, str]:
    if usage.get("disabled_for_run"):
        return False, usage.get("disabled_reason", "disabled_for_run")
    if not config.get("enabled"):
        return False, "disabled"
    if not config.get("api_key"):
        return False, "missing_api_key"
    if usage["requests"] >= int(config.get("max_requests_per_day", 80)):
        return False, "daily_budget_reached"
    if estimate_tokens(prompt) > int(config.get("max_prompt_tokens", 18000)):
        return False, "prompt_too_large"
    return True, "ok"


def wait_for_gemini_slot(config: dict) -> None:
    global GEMINI_LAST_CALL_AT
    min_seconds = float(config.get("min_seconds_between_requests", 5))
    elapsed = time.time() - GEMINI_LAST_CALL_AT
    if elapsed < min_seconds:
        time.sleep(min_seconds - elapsed)
    GEMINI_LAST_CALL_AT = time.time()


def build_gemini_prompt(group: Group, candidates: list[dict], config: dict) -> tuple[str, list[dict]]:
    max_candidates = int(config.get("max_candidates_per_group", 20))
    body_chars = int(config.get("max_body_chars", 700))
    summary_chars = int(config.get("max_summary_chars", 350))
    trimmed = candidates[:max_candidates]
    rows = []
    for index, article in enumerate(trimmed, start=1):
        article["candidate_id"] = f"{group.sector}-{group.category or 'core'}-{index:02d}"
        rows.append({
            "id": article["candidate_id"],
            "title": article["title"],
            "source": re.sub(r"\s+-\s+Google (뉴스|News)$", "", article["source"]).strip(),
            "published_at_kst": to_kst_iso(article["published_dt"]),
            "source_tier": source_tier(article["source"], article.get("resolved_url") or article["url"]),
            "url_domain": domain_key(article.get("resolved_url") or article["url"]),
            "local_score": article.get("score", 0),
            "rss_summary": trim_text(article.get("rss_summary", ""), summary_chars),
            "body_excerpt": trim_text(article.get("body_text", ""), body_chars),
        })

    prompt = {
        "role": "news_editor",
        "language": "ko",
        "task": "최근 24시간 뉴스 후보를 편집자 관점에서 선별하고 한줄요약을 작성한다.",
        "strict_rules": [
            "광고, 홍보성, 출처 불명, 카테고리 관련도가 낮은 후보는 제외한다.",
            "동일 이슈 중복은 가장 신뢰도 높고 최신인 1건만 선택한다.",
            "공식기관/공식기업/전문매체/통신사/금융매체를 우선한다.",
            "한줄요약은 기사 제목을 반복하지 말고, 무엇이 왜 중요한지 1문장으로 쓴다.",
            "추측하지 말고 후보에 포함된 정보만 사용한다.",
        ],
        "sector": group.sector,
        "category": group.category,
        "target_count": group.target,
        "include_keywords": group.include,
        "priority_keywords": group.priority,
        "output_json_schema": {
            "selected": [
                {
                    "id": "candidate id",
                    "rank": 1,
                    "keep": True,
                    "one_line_summary": "한국어 80자 이내",
                    "tags": ["짧은 태그"],
                    "reason": "선별 이유 40자 이내"
                }
            ]
        },
        "candidates": rows,
    }
    return json.dumps(prompt, ensure_ascii=False), trimmed


def parse_gemini_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            return json.loads(match.group(0))
    return {}


def call_gemini(prompt: str, config: dict) -> dict:
    model = config.get("model", "gemini-2.5-flash-lite")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": float(config.get("temperature", 0.2)),
            "maxOutputTokens": int(config.get("max_output_tokens", 1800)),
            "responseMimeType": "application/json",
        },
    }
    response = SESSION.post(
        url,
        params={"key": config["api_key"]},
        json=payload,
        timeout=int(config.get("timeout_seconds", 30)),
    )
    if response.status_code in {401, 403, 429}:
        raise RuntimeError(f"Gemini API stopped by status {response.status_code}: {response.text[:240]}")
    if response.status_code >= 500:
        raise RuntimeError(f"Gemini API server error {response.status_code}: {response.text[:240]}")
    response.raise_for_status()
    data = response.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts)
    return parse_gemini_json(text)


def gemini_edit_candidates(group: Group, candidates: list[dict], usage: dict, now: datetime) -> list[dict]:
    config = ai_config()
    if not candidates:
        return candidates
    prompt, trimmed = build_gemini_prompt(group, candidates, config)
    ok, reason = can_use_gemini(config, usage, prompt)
    if not ok:
        if config.get("enabled"):
            usage["skipped"] += 1
            print(f"  Gemini skipped: {group.sector}/{group.category or '-'} | {reason}", flush=True)
        return candidates

    max_requests_run = int(config.get("max_requests_per_run", len(GROUPS)))
    if usage.get("run_requests", 0) >= max_requests_run:
        usage["skipped"] += 1
        print(f"  Gemini skipped: run budget reached", flush=True)
        return candidates

    try:
        wait_for_gemini_slot(config)
        result = call_gemini(prompt, config)
    except Exception as exc:
        usage["errors"] += 1
        usage["disabled_for_run"] = True
        usage["disabled_reason"] = str(exc)[:220]
        print(f"  Gemini error: {group.sector}/{group.category or '-'} | {exc}", flush=True)
        print("  Gemini disabled for this run. Falling back to local summaries.", flush=True)
        return candidates

    usage["requests"] += 1
    usage["run_requests"] = usage.get("run_requests", 0) + 1
    usage["estimated_input_tokens"] += estimate_tokens(prompt)
    usage["estimated_output_tokens"] += int(config.get("max_output_tokens", 1800))
    usage["model"] = config.get("model", "")

    selected = result.get("selected", [])
    if not isinstance(selected, list):
        return candidates

    by_id = {article.get("candidate_id"): article for article in trimmed}
    edited_ids = set()
    for item in selected:
        if not isinstance(item, dict) or not item.get("keep", True):
            continue
        article = by_id.get(item.get("id"))
        if not article:
            continue
        edited_ids.add(item.get("id"))
        rank = int(item.get("rank") or 99)
        article["score"] = article.get("score", 0) + max(0, 80 - rank * 5)
        summary = normalize_text(item.get("one_line_summary", ""))
        if summary and len(summary) >= 18:
            article["summary"] = summary[:150]
            article["summary_provider"] = "gemini"
        tags = item.get("tags")
        if isinstance(tags, list):
            article["tags"] = [normalize_text(str(tag)) for tag in tags[:5] if normalize_text(str(tag))]
        article["editor_reason"] = normalize_text(item.get("reason", ""))[:80]

    for article in trimmed:
        if article.get("candidate_id") not in edited_ids:
            article["score"] = article.get("score", 0) - int(config.get("unselected_penalty", 18))
    return candidates


def candidate_score(article: dict, group: Group, now: datetime) -> int:
    searchable = f"{article['title']} {article.get('rss_summary', '')} {article.get('body_text', '')} {article['source']}"
    if contains_any(article["source"], CONFIG["blocked_source_patterns"]) or contains_any(article["url"], CONFIG["blocked_source_patterns"]):
        return -100
    if contains_any(searchable, CONFIG["common_exclude"] + group.exclude):
        return -100

    include_hits = keyword_hits(searchable, group.include)
    priority_hits = keyword_hits(searchable, group.priority)
    event_hits = keyword_hits(searchable, CONFIG["event_keywords"])
    if include_hits == 0:
        return -30

    score = (
        include_hits * int(CONFIG["scoring"]["include_hit"])
        + priority_hits * int(CONFIG["scoring"]["priority_hit"])
        + event_hits * int(CONFIG["scoring"]["event_hit"])
        + source_bonus(article["source"], article["url"])
        + recency_bonus(article["published_dt"], now)
    )

    if article.get("body_text"):
        score += int(CONFIG["scoring"]["body_available_bonus"])
    if group.sector == "증시 산업별" and priority_hits == 0 and event_hits == 0:
        score += int(CONFIG["scoring"]["market_without_event_penalty"])
    if len(article["title"]) < 18:
        score -= 4
    return score


def feed_urls_for_group(group: Group) -> list[tuple[str, str]]:
    feeds: list[tuple[str, str]] = []
    for query in group.queries:
        feeds.append(("", google_rss_url(query, korean=has_korean(query))))

    for source in CONFIG["direct_feeds"]:
        tags = set(source.get("tags", []))
        if tags & set(group.source_tags) or contains_any(f"{source['name']} {source['url']}", group.include):
            feeds.append((source["name"], source["url"]))
    return feeds


def collect_candidates(group: Group, now: datetime, window_start: datetime) -> list[dict]:
    candidates = []
    for fallback_source, feed_url in feed_urls_for_group(group):
        try:
            feed = fetch_feed(feed_url)
        except Exception as exc:
            print(f"  feed error: {feed_url} | {exc}", flush=True)
            continue

        for entry in feed.entries[:45]:
            published_dt = parse_datetime(entry)
            if not published_dt or published_dt < window_start or published_dt > now:
                continue
            raw_title = normalize_text(entry.get("title", ""))
            source = entry_source(feed, entry, fallback_source)
            link = normalize_text(entry.get("link", ""))
            title = koreanize_title(strip_source_suffix(raw_title, source), group.sector, group.category, source)
            if not title or not link:
                continue
            rss_summary = normalize_text(entry.get("summary", ""))
            article = {
                "sector": group.sector,
                "category": group.category,
                "title": title,
                "source": source,
                "published_dt": published_dt,
                "url": link,
                "rss_summary": rss_summary,
                "body_text": "",
            }
            quick_score = candidate_score(article, group, now)
            if quick_score < int(CONFIG["scoring"]["candidate_threshold"]):
                continue
            article["score"] = quick_score
            candidates.append(article)

    candidates.sort(key=lambda item: (item["score"], item["published_dt"]), reverse=True)
    for article in candidates[: int(CONFIG["article_extraction"]["max_articles_per_group"])]:
        article["resolved_url"] = resolve_google_url(article["url"])
        article["body_text"] = article_body(article["resolved_url"])
        article["score"] = candidate_score(article, group, now)
        article["summary"] = local_summary(article, group)
    for article in candidates:
        if not article.get("summary"):
            article["summary"] = local_summary(article, group)
    return candidates


def resolve_google_url(url: str) -> str:
    if "news.google.com" not in url:
        return url
    try:
        response = SESSION.get(url, timeout=8)
        match = re.search(r'data-n-au="([^"]+)"', response.text)
        if match:
            return html.unescape(match.group(1))
    except Exception:
        pass
    return url


def load_previous_items() -> list[dict]:
    try:
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    return data.get("items") or data.get("articles") or []


def is_update(article: dict, previous_items: list[dict]) -> bool:
    current = set(normalize_title_key(article["title"]).split()[:8])
    if len(current) < 3:
        return False
    for prev in previous_items:
        prev_words = set(normalize_title_key(prev.get("title", "")).split()[:8])
        if len(current & prev_words) >= 4:
            return True
    return False


def select_articles(group: Group, candidates: list[dict], used_keys: set[str], previous_items: list[dict]) -> list[dict]:
    candidates.sort(key=lambda item: (item["score"], item["published_dt"]), reverse=True)
    selected = []
    local_title_keys = set()
    local_issue_keys = set()

    for article in candidates:
        url_key = f"url:{stable_url_key(article['url'])}"
        title_key = f"title:{normalize_title_key(article['title'])}"
        issue_key = f"issue:{extract_issue_key(article, group)}"
        scoped_title_key = f"{group.category or group.sector}:{title_key}"
        scoped_issue_key = f"{group.category or group.sector}:{issue_key}"
        if (
            url_key in used_keys
            or scoped_title_key in used_keys
            or scoped_issue_key in used_keys
            or title_key in local_title_keys
            or issue_key in local_issue_keys
        ):
            continue
        used_keys.update({url_key, scoped_title_key, scoped_issue_key})
        local_title_keys.add(title_key)
        local_issue_keys.add(issue_key)
        selected.append(article)
        if len(selected) >= group.target:
            break

    shortage = len(selected) < group.target
    final = []
    for article in selected:
        final.append({
            "sector": article["sector"],
            "category": article["category"],
            "title": article["title"],
            "source": re.sub(r"\s+-\s+Google (뉴스|News)$", "", article["source"]).strip(),
            "published_at": to_kst_iso(article["published_dt"]),
            "url": article.get("resolved_url") or resolve_google_url(article["url"]),
            "summary": article["summary"],
            "summary_provider": article.get("summary_provider", "local"),
            "tags": article.get("tags", []),
            "editor_reason": article.get("editor_reason", ""),
            "is_update": is_update(article, previous_items),
            "shortage": shortage,
            "score": article["score"],
            "source_tier": source_tier(article["source"], article["url"]),
        })
    return final


def main() -> None:
    now = now_utc()
    window_start = now - timedelta(hours=int(CONFIG["collection_window_hours"]))
    next_update = now + timedelta(hours=int(CONFIG.get("update_interval_hours", 6)))
    previous_items = load_previous_items()
    gemini_usage = load_gemini_usage(now)
    gemini_usage["run_requests"] = 0
    gemini_usage.pop("disabled_for_run", None)
    gemini_usage.pop("disabled_reason", None)
    used_keys: set[str] = set()
    items = []
    shortage_groups = 0

    print("뉴스 수집 시작", flush=True)
    print(f"수집 범위: {to_kst_iso(window_start)} ~ {to_kst_iso(now)}", flush=True)

    for group in GROUPS:
        candidates = collect_candidates(group, now, window_start)
        candidates = gemini_edit_candidates(group, candidates, gemini_usage, now)
        selected = select_articles(group, candidates, used_keys, previous_items)
        if len(selected) < group.target:
            shortage_groups += 1
        items.extend(selected)
        label = f"{group.sector} / {group.category}" if group.category else group.sector
        print(f"{label}: {len(selected)}/{group.target} (candidates {len(candidates)})", flush=True)

    order = {name: index for index, name in enumerate(CONFIG["sector_order"])}
    items.sort(key=lambda item: (
        order.get(item["sector"], 99),
        item["category"] or "",
        item["published_at"],
    ))

    output = {
        "generated_at": to_kst_iso(now),
        "next_update_at": to_kst_iso(next_update),
        "window": {
            "from": to_kst_iso(window_start),
            "to": to_kst_iso(now),
            "timezone": "Asia/Seoul",
        },
        "ai": {
            "provider": "gemini" if any(item.get("summary_provider") == "gemini" for item in items) else "local",
            "label": "Gemini의 요약이 반영되어 있습니다" if any(item.get("summary_provider") == "gemini" for item in items) else "자체 요약이 반영되어 있습니다",
            "gemini_enabled": ai_config().get("enabled") and bool(ai_config().get("api_key")),
            "gemini_requests_today": gemini_usage.get("requests", 0),
            "gemini_requests_this_run": gemini_usage.get("run_requests", 0),
            "gemini_errors_today": gemini_usage.get("errors", 0),
            "gemini_disabled_reason": gemini_usage.get("disabled_reason", ""),
            "model": gemini_usage.get("model") or ai_config().get("model", ""),
        },
        "totals": {
            "expected": EXPECTED_TOTAL,
            "collected": len(items),
            "shortage_groups": shortage_groups,
        },
        "items": items,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
        f.write("\n")
    gemini_usage.pop("run_requests", None)
    gemini_usage.pop("disabled_for_run", None)
    save_gemini_usage(gemini_usage)

    print(f"완료: {len(items)}/{EXPECTED_TOTAL}건 저장 -> {OUTPUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
