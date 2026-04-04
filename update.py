#!/usr/bin/env python3
"""
AI Magazine OS — Daily Update Script
=====================================
Scrapes AI sources, scores content, generates structured data.json.

Usage:
    python3 update.py                  # Full update
    python3 update.py --source hf      # Single source
    python3 update.py --dry-run        # Preview without writing

Cron (daily at 7am):
    0 7 * * * cd /path/to/ai-magazine-os && python3 update.py >> logs/update.log 2>&1

Scheduler (systemd timer, GitHub Actions, etc.):
    See README section at bottom of this file.
"""

import json
import re
import sys
import hashlib
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field, asdict

# ---------------------------------------------------------------------------
# Optional heavy imports — graceful fallback
# ---------------------------------------------------------------------------
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
OUTPUT_PATH = Path(__file__).parent / "data.json"
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "update.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("ai-magazine-os")

HEADERS = {
    "User-Agent": "AI-Magazine-OS/1.0 (https://github.com/ai-magazine-os)"
}
REQUEST_TIMEOUT = 20

# Subreddits to scrape
SUBREDDITS = ["MachineLearning", "LocalLLaMA", "artificial"]

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class Scores:
    knowledge_value: int = 5
    marketing_value: int = 5
    forkability: int = 5
    engageability: int = 5

    @property
    def total(self):
        return (
            self.knowledge_value
            + self.marketing_value
            + self.forkability
            + self.engageability
        )

@dataclass
class Item:
    id: str = ""
    title: str = ""
    summary: str = ""
    source: str = ""                # hf_papers | hf_models | hf_spaces | github | pwc | reddit
    tags: list = field(default_factory=list)   # model | product | repo | tool | discussion
    read_url: str = ""
    try_url: str = ""
    fork_url: str = ""
    engage_url: str = ""
    why_it_matters: str = ""
    content_angle: str = ""
    scores: Scores = field(default_factory=Scores)
    scraped_at: str = ""

    def to_dict(self):
        d = asdict(self)
        d["scores"]["total"] = self.scores.total
        return d

# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------
def make_id(source: str, title: str) -> str:
    raw = f"{source}:{title}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]

def safe_get(url: str, **kwargs) -> Optional[requests.Response]:
    if not HAS_REQUESTS:
        log.warning("requests not installed — returning None")
        return None
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, **kwargs)
        r.raise_for_status()
        return r
    except Exception as e:
        log.error(f"GET {url} failed: {e}")
        return None

def truncate(text: str, length: int = 200) -> str:
    text = text.strip()
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0] + "…"

def now_iso():
    return datetime.now(timezone.utc).isoformat()

# ---------------------------------------------------------------------------
# Scoring heuristics (rule-based, no LLM needed)
# ---------------------------------------------------------------------------
MARKETING_KEYWORDS = [
    "demo", "app", "tool", "launch", "release", "free", "open source",
    "tutorial", "guide", "workflow", "automation", "no-code", "low-code",
    "agent", "chatbot", "image", "video", "generate", "create",
]
KNOWLEDGE_KEYWORDS = [
    "paper", "research", "benchmark", "sota", "state-of-the-art",
    "architecture", "training", "dataset", "evaluation", "reasoning",
    "transformer", "attention", "fine-tune", "pretraining",
]
FORK_KEYWORDS = [
    "github", "repo", "open source", "mit license", "apache",
    "fork", "clone", "huggingface", "model card", "weights",
]
ENGAGE_KEYWORDS = [
    "discussion", "debate", "opinion", "controversial", "thread",
    "ama", "ask", "why", "should", "vs", "compared",
]

def auto_score(item: Item) -> Scores:
    """Score an item based on keyword heuristics."""
    text = f"{item.title} {item.summary}".lower()

    def kw_score(keywords):
        hits = sum(1 for kw in keywords if kw in text)
        return min(10, 3 + hits * 2)

    return Scores(
        knowledge_value=kw_score(KNOWLEDGE_KEYWORDS),
        marketing_value=kw_score(MARKETING_KEYWORDS),
        forkability=kw_score(FORK_KEYWORDS),
        engageability=kw_score(ENGAGE_KEYWORDS),
    )

def auto_tag(item: Item) -> list:
    """Assign tags based on source and content."""
    text = f"{item.title} {item.summary}".lower()
    tags = []
    if item.source in ("hf_papers", "hf_models", "pwc"):
        tags.append("model")
    if item.source == "hf_spaces":
        tags.append("product")
    if item.source == "github":
        tags.append("repo")
    if item.source == "reddit":
        tags.append("discussion")
    if any(kw in text for kw in ["agent", "workflow", "automation", "tool"]):
        tags.append("tool")
    return list(dict.fromkeys(tags)) or ["model"]

def auto_why(item: Item) -> str:
    """根据标题和摘要关键词，动态生成中文一句话价值总结。"""
    t = f"{item.title} {item.summary}".lower()
    name = _extract_name(item.title)

    # --- 具体关键词匹配（优先级从高到低）---

    # 视频/图像/音频生成
    if any(kw in t for kw in ["video generat", "text-to-video", "image generat", "text-to-image", "diffusion", "flux", "stable diffusion", "midjourney"]):
        return f"{name} 降低了视觉内容创作门槛，非设计师也能做出专业素材"
    # 代码/编程
    if any(kw in t for kw in ["code", "coding", "copilot", "cursor", "programming", "debug", "refactor"]):
        return f"{name} 正在改变写代码的方式——速度和质量都在跃升"
    # Agent / 自动化
    if any(kw in t for kw in ["agent", "agentic", "automation", "automat", "workflow", "browser", "computer use"]):
        return f"{name} 让 AI 从「回答问题」进化到「自动干活」，效率提升是量级的"
    # RAG / 知识库 / 搜索
    if any(kw in t for kw in ["rag", "retrieval", "knowledge base", "search", "embedding", "vector"]):
        return f"{name} 提升了 AI 获取和使用外部知识的能力，准确率更高了"
    # 小模型 / 量化 / 端侧
    if any(kw in t for kw in ["small model", "quantiz", "gguf", "onnx", "edge", "mobile", "tiny", "mini", "1b", "3b", "7b", "8b"]):
        return f"{name} 证明小模型也能跑出好效果，本地部署成本大幅降低"
    # 大模型 / 旗舰
    if any(kw in t for kw in ["gpt", "claude", "gemini", "llama", "qwen", "deepseek", "mistral", "70b", "72b", "405b"]):
        return f"{name} 刷新了模型能力上限，直接影响应用层能做什么"
    # 多模态
    if any(kw in t for kw in ["multimodal", "vision", "audio", "speech", "tts", "asr", "ocr"]):
        return f"{name} 让 AI 能同时处理文本、图像、语音，应用场景扩大了"
    # 微调 / 训练
    if any(kw in t for kw in ["fine-tun", "finetun", "lora", "qlora", "train", "pretrain"]):
        return f"{name} 降低了定制模型的门槛，小团队也能训练专属 AI"
    # 开源 / 许可
    if any(kw in t for kw in ["open source", "open-source", "apache", "mit license", "released under"]):
        return f"{name} 是开源的，意味着任何人都可以免费使用、修改和部署"
    # 数据集
    if any(kw in t for kw in ["dataset", "benchmark", "leaderboard", "evaluation", "eval"]):
        return f"{name} 提供了新的评估标准，帮助判断哪些模型真正好用"
    # 安全 / 对齐
    if any(kw in t for kw in ["safety", "alignment", "rlhf", "guardrail", "jailbreak", "red team"]):
        return f"{name} 关系到 AI 能不能被安全使用，这是行业的底线问题"
    # 工具 / 产品
    if any(kw in t for kw in ["tool", "app", "saas", "platform", "api", "sdk", "framework"]):
        return f"{name} 是可以直接用的工具，能立刻提升你的工作效率"
    # 讨论 / 争议
    if any(kw in t for kw in ["discuss", "opinion", "debate", "controversy", "should", "will ai"]):
        return f"{name} 是行业正在争论的话题，了解各方观点能帮你做判断"

    # --- 按来源兜底 ---
    src_fallback = {
        "hf_papers": f"{name} 代表了 AI 研究的最新方向，可能影响未来几个月的产品迭代",
        "hf_models": f"{name} 是新发布的模型，直接影响开发者能用什么",
        "hf_spaces": f"{name} 是可以在线体验的 demo，适合截图做内容素材",
        "github": f"{name} 正在 GitHub 上快速增长，说明开发者有真实需求",
        "pwc": f"{name} 是带代码的论文，可以直接复现验证效果",
        "reddit": f"{name} 在社区引发了广泛讨论，代表了从业者的真实关切",
    }
    return src_fallback.get(item.source, f"{name} 是值得关注的 AI 动态")


def auto_angle(item: Item) -> str:
    """根据内容类型和关键词，生成具体可执行的内容角度（中文）。"""
    t = f"{item.title} {item.summary}".lower()
    name = _extract_name(item.title)

    # --- 具体场景匹配 ---
    if any(kw in t for kw in ["video generat", "text-to-video", "image generat", "diffusion"]):
        return f"拿 {name} 生成一组作品截图，发帖「AI 做的图/视频能到什么水平了」"
    if any(kw in t for kw in ["code", "coding", "copilot", "cursor", "programming"]):
        return f"用 {name} 完成一个实际任务，录屏发帖「AI 写代码 vs 人写代码」"
    if any(kw in t for kw in ["agent", "agentic", "automation", "browser"]):
        return f"录制 {name} 自动完成任务的过程，发帖「AI 自动化实操演示」"
    if any(kw in t for kw in ["rag", "retrieval", "knowledge base", "search"]):
        return f"用 {name} 搭一个简单知识库 demo，发帖「10 分钟搭建 AI 知识助手」"
    if any(kw in t for kw in ["quantiz", "gguf", "local", "ollama", "mini", "small"]):
        return f"在本地跑一下 {name}，截图发帖「用自己电脑跑 AI 是什么体验」"
    if any(kw in t for kw in ["70b", "72b", "405b", "gpt", "claude", "gemini"]):
        return f"对比测试 {name} 和竞品，发帖「大模型横评：谁更好用？」"
    if any(kw in t for kw in ["open source", "github", "star", "trending"]):
        return f"写一篇 {name} 的安装和上手教程，发帖「这个开源项目值得试」"
    if any(kw in t for kw in ["fine-tun", "finetun", "lora", "train"]):
        return f"写 {name} 的微调教程，发帖「手把手教你训练自己的 AI 模型」"
    if any(kw in t for kw in ["discuss", "opinion", "debate", "vs"]):
        return f"整理 {name} 讨论中的正反观点，发帖「这个争论你站哪边？」"
    if any(kw in t for kw in ["tool", "app", "workflow", "no-code"]):
        return f"实测 {name} 做一个具体任务，发帖「这个 AI 工具能帮你省多少时间」"

    # --- 按来源兜底 ---
    if item.source == "hf_papers":
        return f"用通俗语言解读 {name}，发帖「一句话看懂今天的 AI 论文」"
    if item.source == "hf_spaces":
        return f"截图 {name} 的效果，发帖「又一个好玩的 AI demo」"
    if item.source == "reddit":
        return f"翻译/总结 {name} 讨论要点，发帖「Reddit AI 社区在聊什么」"
    return f"围绕 {name} 写一篇简短解读，分享你的观点"


def _extract_name(title: str) -> str:
    """从标题中提取核心名称，用于生成文案。"""
    t = title.strip()
    # HuggingFace "org/model-name" 格式 → 取模型名
    if "/" in t and len(t.split("/")) == 2 and " " not in t:
        t = t.split("/")[1]
    # 去掉常见前缀噪音
    for prefix in ["[D]", "[R]", "[P]", "[N]", "[Project]"]:
        t = t.replace(prefix, "").strip()
    # 取冒号/破折号前面的部分（通常是产品名）
    for sep in ["：", ":", " — ", " - ", " – "]:
        if sep in t:
            left = t.split(sep)[0].strip()
            if 2 <= len(left) <= 30:
                return left
    # 如果太长，在空格处截断
    if len(t) > 30:
        words = t[:30].rsplit(" ", 1)
        return words[0] + "…" if len(words) > 1 else t[:28] + "…"
    return t

# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------

def scrape_hf_papers() -> list[Item]:
    """Scrape Hugging Face daily papers."""
    log.info("Scraping Hugging Face Papers...")
    items = []

    # Try the API first
    r = safe_get("https://huggingface.co/api/daily_papers")
    if r:
        try:
            data = r.json()
            for entry in data[:15]:
                paper = entry.get("paper", {})
                title = paper.get("title", "").strip()
                summary = truncate(paper.get("summary", ""), 200)
                paper_id = paper.get("id", "")
                if not title:
                    continue
                item = Item(
                    id=make_id("hf_papers", title),
                    title=title,
                    summary=summary,
                    source="hf_papers",
                    read_url=f"https://huggingface.co/papers/{paper_id}" if paper_id else "",
                    try_url="",
                    fork_url="",
                    engage_url=f"https://huggingface.co/papers/{paper_id}" if paper_id else "",
                    scraped_at=now_iso(),
                )
                item.tags = auto_tag(item)
                item.scores = auto_score(item)
                item.why_it_matters = auto_why(item)
                item.content_angle = auto_angle(item)
                items.append(item)
            log.info(f"  → Got {len(items)} papers from API")
            return items
        except Exception as e:
            log.warning(f"  API parse failed: {e}, falling back to HTML")

    # Fallback: scrape HTML
    if not HAS_BS4:
        log.warning("  bs4 not installed, skipping HTML fallback")
        return items

    r = safe_get("https://huggingface.co/papers")
    if not r:
        return items
    soup = BeautifulSoup(r.text, "html.parser")
    articles = soup.select("article")[:15]
    for art in articles:
        a = art.select_one("a[href*='/papers/']")
        if not a:
            continue
        title = a.get_text(strip=True)
        href = a.get("href", "")
        summary_el = art.select_one("p")
        summary = truncate(summary_el.get_text(strip=True), 200) if summary_el else ""
        item = Item(
            id=make_id("hf_papers", title),
            title=title,
            summary=summary,
            source="hf_papers",
            read_url=f"https://huggingface.co{href}",
            engage_url=f"https://huggingface.co{href}",
            scraped_at=now_iso(),
        )
        item.tags = auto_tag(item)
        item.scores = auto_score(item)
        item.why_it_matters = auto_why(item)
        item.content_angle = auto_angle(item)
        items.append(item)
    log.info(f"  → Got {len(items)} papers from HTML")
    return items


def scrape_hf_models() -> list[Item]:
    """Scrape trending models from Hugging Face."""
    log.info("Scraping Hugging Face Models...")
    items = []
    r = safe_get("https://huggingface.co/api/models?sort=trending&direction=-1&limit=10")
    if not r:
        return items
    try:
        data = r.json()
        for model in data:
            model_id = model.get("modelId", "") or model.get("id", "")
            title = model_id
            summary = f"Downloads: {model.get('downloads', 'N/A')} · Likes: {model.get('likes', 'N/A')}"
            pipeline = model.get("pipeline_tag", "")
            if pipeline:
                summary = f"{pipeline} — {summary}"
            item = Item(
                id=make_id("hf_models", title),
                title=title,
                summary=summary,
                source="hf_models",
                read_url=f"https://huggingface.co/{model_id}",
                try_url=f"https://huggingface.co/{model_id}" if pipeline else "",
                fork_url=f"https://huggingface.co/{model_id}",
                engage_url="",
                scraped_at=now_iso(),
            )
            item.tags = auto_tag(item)
            item.scores = auto_score(item)
            item.why_it_matters = auto_why(item)
            item.content_angle = auto_angle(item)
            items.append(item)
    except Exception as e:
        log.error(f"  HF models parse error: {e}")
    log.info(f"  → Got {len(items)} models")
    return items


def scrape_hf_spaces() -> list[Item]:
    """Scrape trending spaces from Hugging Face."""
    log.info("Scraping Hugging Face Spaces...")
    items = []
    r = safe_get("https://huggingface.co/api/spaces?sort=trending&direction=-1&limit=10")
    if not r:
        return items
    try:
        data = r.json()
        for space in data:
            space_id = space.get("id", "")
            title = space_id
            sdk = space.get("sdk", "")
            likes = space.get("likes", 0)
            summary = f"SDK: {sdk} · Likes: {likes}" if sdk else f"Likes: {likes}"
            item = Item(
                id=make_id("hf_spaces", title),
                title=title,
                summary=summary,
                source="hf_spaces",
                read_url=f"https://huggingface.co/spaces/{space_id}",
                try_url=f"https://huggingface.co/spaces/{space_id}",
                fork_url=f"https://huggingface.co/spaces/{space_id}",
                engage_url="",
                scraped_at=now_iso(),
            )
            item.tags = auto_tag(item)
            item.scores = auto_score(item)
            item.why_it_matters = auto_why(item)
            item.content_angle = auto_angle(item)
            items.append(item)
    except Exception as e:
        log.error(f"  HF spaces parse error: {e}")
    log.info(f"  → Got {len(items)} spaces")
    return items


def scrape_github_trending() -> list[Item]:
    """Scrape GitHub Trending page."""
    log.info("Scraping GitHub Trending...")
    items = []
    if not HAS_BS4:
        log.warning("  bs4 not installed, skipping")
        return items
    r = safe_get("https://github.com/trending?since=daily")
    if not r:
        return items
    soup = BeautifulSoup(r.text, "html.parser")
    rows = soup.select("article.Box-row")[:12]
    for row in rows:
        h2 = row.select_one("h2 a")
        if not h2:
            continue
        repo_path = h2.get("href", "").strip("/")
        title = repo_path
        desc_el = row.select_one("p")
        summary = truncate(desc_el.get_text(strip=True), 200) if desc_el else ""
        # Stars today
        stars_el = row.select_one("span.d-inline-block.float-sm-right")
        stars_today = stars_el.get_text(strip=True) if stars_el else ""
        if stars_today:
            summary = f"{summary} · {stars_today}" if summary else stars_today
        lang_el = row.select_one("[itemprop='programmingLanguage']")
        lang = lang_el.get_text(strip=True) if lang_el else ""
        if lang:
            summary = f"[{lang}] {summary}"
        item = Item(
            id=make_id("github", title),
            title=title,
            summary=summary,
            source="github",
            read_url=f"https://github.com/{repo_path}",
            try_url="",
            fork_url=f"https://github.com/{repo_path}",
            engage_url=f"https://github.com/{repo_path}/discussions",
            scraped_at=now_iso(),
        )
        item.tags = auto_tag(item)
        item.scores = auto_score(item)
        item.why_it_matters = auto_why(item)
        item.content_angle = auto_angle(item)
        items.append(item)
    log.info(f"  → Got {len(items)} repos")
    return items


def scrape_papers_with_code() -> list[Item]:
    """Scrape Papers with Code trending."""
    log.info("Scraping Papers with Code...")
    items = []
    if not HAS_BS4:
        log.warning("  bs4 not installed, skipping")
        return items
    r = safe_get("https://paperswithcode.com")
    if not r:
        return items
    soup = BeautifulSoup(r.text, "html.parser")
    cards = soup.select(".paper-card")[:10]
    for card in cards:
        a = card.select_one("a.paper-card-title, h1 a")
        if not a:
            continue
        title = a.get_text(strip=True)
        href = a.get("href", "")
        abstract_el = card.select_one(".paper-card-desc, .item-strip-abstract")
        summary = truncate(abstract_el.get_text(strip=True), 200) if abstract_el else ""
        github_a = card.select_one("a[href*='github.com']")
        fork_url = github_a.get("href", "") if github_a else ""
        item = Item(
            id=make_id("pwc", title),
            title=title,
            summary=summary,
            source="pwc",
            read_url=f"https://paperswithcode.com{href}" if href.startswith("/") else href,
            try_url="",
            fork_url=fork_url,
            engage_url="",
            scraped_at=now_iso(),
        )
        item.tags = auto_tag(item)
        item.scores = auto_score(item)
        item.why_it_matters = auto_why(item)
        item.content_angle = auto_angle(item)
        items.append(item)
    log.info(f"  → Got {len(items)} papers")
    return items


def scrape_reddit() -> list[Item]:
    """Scrape top posts from AI subreddits via JSON API."""
    log.info("Scraping Reddit...")
    items = []
    for sub in SUBREDDITS:
        url = f"https://www.reddit.com/r/{sub}/hot.json?limit=8"
        r = safe_get(url)
        if not r:
            continue
        try:
            data = r.json()
            posts = data.get("data", {}).get("children", [])
            for post in posts:
                d = post.get("data", {})
                if d.get("stickied"):
                    continue
                title = d.get("title", "").strip()
                if not title:
                    continue
                selftext = truncate(d.get("selftext", ""), 200)
                ups = d.get("ups", 0)
                num_comments = d.get("num_comments", 0)
                permalink = d.get("permalink", "")
                summary = selftext if selftext else f"⬆ {ups} · 💬 {num_comments} comments"
                item = Item(
                    id=make_id("reddit", title),
                    title=title,
                    summary=summary,
                    source="reddit",
                    read_url=f"https://reddit.com{permalink}",
                    try_url="",
                    fork_url="",
                    engage_url=f"https://reddit.com{permalink}",
                    scraped_at=now_iso(),
                )
                item.tags = auto_tag(item)
                item.scores = auto_score(item)
                item.scores.engageability = min(10, 5 + num_comments // 50)
                item.scores.marketing_value = min(10, 5 + ups // 200)
                item.why_it_matters = auto_why(item)
                item.content_angle = auto_angle(item)
                items.append(item)
        except Exception as e:
            log.error(f"  Reddit r/{sub} parse error: {e}")
    log.info(f"  → Got {len(items)} reddit posts")
    return items


# ---------------------------------------------------------------------------
# Trend summary generator
# ---------------------------------------------------------------------------
def generate_trends(items: list[Item]) -> list[dict]:
    """Generate trend radar entries from top items."""
    # Group by tag and pick patterns
    tag_counts = {}
    for item in items:
        for tag in item.tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    trends = []

    # Find dominant themes from titles
    all_titles = " ".join(i.title.lower() for i in items)
    theme_signals = [
        ("agent", "real", "真方向", "AI Agent 生态持续扩展", "多 agent、工具调用、浏览器操作等方向正在从 demo 走向生产"),
        ("open source", "real", "真方向", "开源模型加速追赶", "开源社区的迭代速度正在缩小与闭源模型的差距"),
        ("小模型", "real", "真方向", "端侧推理持续升温", "小模型在手机和边缘设备上的部署能力越来越强"),
        ("video", "watch", "值得关注", "AI 视频生成竞赛加速", "多家公司发布视频生成模型，质量和速度都在快速提升"),
        ("agi", "hype", "Hype 预警", "AGI 叙事再次升温", "AGI 时间线预测频繁出现，但具体评估标准仍然模糊"),
        ("benchmark", "watch", "值得关注", "Benchmark 刷新频率加快", "模型性能天花板持续提升，但实际应用差距仍待验证"),
    ]

    for keyword, label, label_text, title, text in theme_signals:
        if keyword in all_titles or tag_counts.get(keyword, 0) > 0:
            trends.append({
                "label": label,
                "labelText": label_text,
                "title": title,
                "text": text,
            })
        if len(trends) >= 6:
            break

    # Fill with defaults if needed
    defaults = [
        {"label": "real", "labelText": "真方向", "title": "开源模型能力持续提升", "text": "HuggingFace 上新模型的质量和数量都在加速增长"},
        {"label": "watch", "labelText": "值得关注", "title": "AI 工具链整合加速", "text": "从单点工具到完整工作流，AI 开发体验正在被重塑"},
        {"label": "hype", "labelText": "Hype 预警", "title": "AI 产品同质化严重", "text": "大量包装类 AI 产品涌现，核心差异化不足"},
        {"label": "real", "labelText": "真方向", "title": "多模态成为标配", "text": "文本、图像、音频、视频的统一处理能力越来越普遍"},
        {"label": "watch", "labelText": "值得关注", "title": "AI 监管政策收紧", "text": "全球多地出台 AI 相关法规，合规成本上升"},
        {"label": "hype", "labelText": "Hype 预警", "title": "AI 替代一切的叙事", "text": "AI 在特定任务上表现优异，但通用替代仍需时间"},
    ]
    while len(trends) < 6:
        trends.append(defaults[len(trends) % len(defaults)])

    return trends[:6]


# ---------------------------------------------------------------------------
# Marketing insights generator
# ---------------------------------------------------------------------------
def generate_insights(items: list[Item]) -> list[dict]:
    """Generate marketing insight cards from top items."""
    top_marketing = sorted(items, key=lambda i: i.scores.marketing_value, reverse=True)[:5]
    top_knowledge = sorted(items, key=lambda i: i.scores.knowledge_value, reverse=True)[:3]

    insights = [
        {
            "title": "🔥 本周趋势关键词",
            "text": "基于抓取内容自动提取的高频主题",
            "items": list(dict.fromkeys(
                tag for item in items[:20] for tag in item.tags
            ))[:6],
        },
        {
            "title": "📝 可发内容 Top 5",
            "text": "营销价值最高的话题",
            "items": [f"「{i.title[:30]}」—— {i.content_angle[:40]}" for i in top_marketing],
        },
        {
            "title": "📸 截图素材建议",
            "text": "可直接截图发社交媒体",
            "items": [
                f"{i.title[:25]} 的界面/结果截图" for i in top_marketing[:4]
            ],
        },
        {
            "title": "⚡ 本周行动建议",
            "text": "基于趋势推荐的 3 个行动",
            "items": [
                f"试用/Fork: {top_marketing[0].title[:25]}" if top_marketing else "浏览 HuggingFace trending",
                f"深度阅读: {top_knowledge[0].title[:25]}" if top_knowledge else "阅读 Papers with Code 热门",
                "在 Reddit 发一篇 AI 工具对比帖",
            ],
        },
    ]
    return insights


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
SOURCE_MAP = {
    "hf": [scrape_hf_papers, scrape_hf_models, scrape_hf_spaces],
    "github": [scrape_github_trending],
    "pwc": [scrape_papers_with_code],
    "reddit": [scrape_reddit],
}

def run_update(sources: list[str] | None = None, dry_run: bool = False):
    log.info("=" * 60)
    log.info(f"AI Magazine OS — Update started at {now_iso()}")
    log.info("=" * 60)

    if not HAS_REQUESTS:
        log.error("'requests' library not installed. Run: pip install requests beautifulsoup4")
        log.info("Generating sample data.json with mock data instead...")
        generate_mock_data()
        return

    all_items: list[Item] = []
    scrapers = []

    if sources:
        for src in sources:
            scrapers.extend(SOURCE_MAP.get(src, []))
    else:
        for funcs in SOURCE_MAP.values():
            scrapers.extend(funcs)

    for scraper in scrapers:
        try:
            items = scraper()
            all_items.extend(items)
        except Exception as e:
            log.error(f"Scraper {scraper.__name__} crashed: {e}")

    # Deduplicate by id
    seen = set()
    unique = []
    for item in all_items:
        if item.id not in seen:
            seen.add(item.id)
            unique.append(item)
    all_items = unique

    log.info(f"Total unique items: {len(all_items)}")

    # Sort by total score descending
    all_items.sort(key=lambda i: i.scores.total, reverse=True)

    # Split into cover (top scored) and sections
    cover_threshold = 24
    cover_items = [i for i in all_items if i.scores.total >= cover_threshold][:8]
    if len(cover_items) < 5:
        cover_items = all_items[:min(8, len(all_items))]

    # Section assignment
    sections = {
        "models": [i for i in all_items if i.source in ("hf_papers", "hf_models", "pwc")],
        "products": [i for i in all_items if i.source == "hf_spaces"],
        "opensource": [i for i in all_items if i.source == "github"],
        "tools": [i for i in all_items if "tool" in i.tags],
        "discussions": [i for i in all_items if i.source == "reddit"],
    }

    # Generate trends & insights
    trends = generate_trends(all_items)
    insights = generate_insights(all_items)

    # Build output
    output = {
        "meta": {
            "updated_at": now_iso(),
            "total_items": len(all_items),
            "sources_scraped": list(set(i.source for i in all_items)),
            "version": "1.0",
        },
        "trends": trends,
        "cover": [i.to_dict() for i in cover_items],
        "sections": {
            key: [i.to_dict() for i in items[:8]]
            for key, items in sections.items()
        },
        "insights": insights,
        "all_items": [i.to_dict() for i in all_items],
    }

    if dry_run:
        log.info("DRY RUN — not writing file")
        print(json.dumps(output, indent=2, ensure_ascii=False)[:3000])
        print("... (truncated)")
        return

    # Write
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    log.info(f"Written to {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size:,} bytes)")
    log.info("Update complete!")


def generate_mock_data():
    """Generate a mock data.json when scraping isn't available."""
    mock_items = [
        Item(id="mock01", title="Qwen3-72B 发布：开源模型新标杆", summary="阿里最新开源模型在多项 benchmark 上超越同量级竞品", source="hf_models",
             tags=["model"], read_url="https://huggingface.co/Qwen", fork_url="https://github.com/QwenLM/Qwen",
             why_it_matters="开源模型首次在 reasoning 任务上逼近 GPT-4 水平", content_angle="「开源 vs 闭源」对比帖",
             scores=Scores(9,8,9,8), scraped_at=now_iso()),
        Item(id="mock02", title="browser-use：让 AI 操作你的浏览器", summary="开源项目让 LLM 自动完成浏览器任务，3 天 10K stars", source="github",
             tags=["repo","tool"], read_url="https://github.com/browser-use/browser-use", fork_url="https://github.com/browser-use/browser-use",
             why_it_matters="AI agent 从聊天进入真实操作环境", content_angle="录制 demo 视频「看 AI 自己操作网页」",
             scores=Scores(8,9,10,8), scraped_at=now_iso()),
        Item(id="mock03", title="Dify 1.0：开源 AI 应用平台成熟了", summary="从 workflow builder 进化为完整 AI 应用平台", source="github",
             tags=["tool","product"], read_url="https://dify.ai", try_url="https://dify.ai", fork_url="https://github.com/langgenius/dify",
             why_it_matters="非技术用户也能构建 AI 应用", content_angle="零代码搭建 AI 应用教程",
             scores=Scores(8,8,8,7), scraped_at=now_iso()),
        Item(id="mock04", title="AI 生成内容正在毒化搜索引擎", summary="Reddit 热帖讨论 AI SEO spam 对搜索质量的影响", source="reddit",
             tags=["discussion"], read_url="https://reddit.com/r/MachineLearning", engage_url="https://reddit.com/r/MachineLearning",
             why_it_matters="内容行业的质量危机正在加速", content_angle="「如何在 AI 内容洪流中保持人味」",
             scores=Scores(7,10,3,10), scraped_at=now_iso()),
        Item(id="mock05", title="Phi-4-mini：3.8B 小模型吊打同量级", summary="微软端侧小模型，手机上可流畅运行", source="hf_models",
             tags=["model"], read_url="https://huggingface.co/microsoft/phi-4-mini", fork_url="https://huggingface.co/microsoft/phi-4-mini",
             why_it_matters="小模型真正可用意味着 AI 可以脱离云端", content_angle="「手机上跑 AI 不是梦了」实测帖",
             scores=Scores(8,7,8,6), scraped_at=now_iso()),
        Item(id="mock06", title="CrewAI：多 Agent 协作框架", summary="让多个 AI agent 像团队一样协作完成复杂任务", source="github",
             tags=["tool","repo"], read_url="https://crewai.com", fork_url="https://github.com/crewAIInc/crewAI",
             why_it_matters="从单一 agent 到多 agent 系统的进化", content_angle="「AI 不只是一个助手，而是一个团队」",
             scores=Scores(8,7,8,6), scraped_at=now_iso()),
    ]

    trends = generate_trends(mock_items)
    insights = generate_insights(mock_items)

    output = {
        "meta": {
            "updated_at": now_iso(),
            "total_items": len(mock_items),
            "sources_scraped": ["mock"],
            "version": "1.0",
        },
        "trends": trends,
        "cover": [i.to_dict() for i in mock_items],
        "sections": {
            "models": [i.to_dict() for i in mock_items if "model" in i.tags],
            "products": [i.to_dict() for i in mock_items if "product" in i.tags],
            "opensource": [i.to_dict() for i in mock_items if "repo" in i.tags],
            "tools": [i.to_dict() for i in mock_items if "tool" in i.tags],
            "discussions": [i.to_dict() for i in mock_items if "discussion" in i.tags],
        },
        "insights": insights,
        "all_items": [i.to_dict() for i in mock_items],
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    log.info(f"Mock data written to {OUTPUT_PATH}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI Magazine OS — Daily Updater")
    parser.add_argument("--source", choices=["hf", "github", "pwc", "reddit"],
                        nargs="*", help="Scrape specific sources only")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--mock", action="store_true", help="Generate mock data only")
    args = parser.parse_args()

    if args.mock:
        generate_mock_data()
    else:
        run_update(sources=args.source, dry_run=args.dry_run)


# ---------------------------------------------------------------------------
# README: Scheduling
# ---------------------------------------------------------------------------
#
# === CRON (Linux/Mac) ===
# crontab -e
# 0 7 * * * cd /path/to/ai-magazine-os && /usr/bin/python3 update.py >> logs/cron.log 2>&1
#
# === SYSTEMD TIMER (Linux) ===
# Create /etc/systemd/system/ai-magazine-update.service:
#   [Unit]
#   Description=AI Magazine OS Daily Update
#   [Service]
#   Type=oneshot
#   WorkingDirectory=/path/to/ai-magazine-os
#   ExecStart=/usr/bin/python3 update.py
#
# Create /etc/systemd/system/ai-magazine-update.timer:
#   [Unit]
#   Description=Run AI Magazine OS update daily
#   [Timer]
#   OnCalendar=*-*-* 07:00:00
#   Persistent=true
#   [Install]
#   WantedBy=timers.target
#
# sudo systemctl enable --now ai-magazine-update.timer
#
# === GITHUB ACTIONS ===
# .github/workflows/update.yml:
#   name: Daily Update
#   on:
#     schedule:
#       - cron: '0 7 * * *'
#     workflow_dispatch:
#   jobs:
#     update:
#       runs-on: ubuntu-latest
#       steps:
#         - uses: actions/checkout@v4
#         - uses: actions/setup-python@v5
#           with:
#             python-version: '3.11'
#         - run: pip install requests beautifulsoup4
#         - run: python update.py
#         - run: |
#             git config user.name "AI Magazine Bot"
#             git config user.email "bot@ai-magazine-os"
#             git add data.json
#             git commit -m "Daily update $(date -u +%Y-%m-%d)" || true
#             git push
#
# === WINDOWS TASK SCHEDULER ===
# schtasks /create /tn "AI Magazine OS" /tr "python C:\path\update.py" /sc daily /st 07:00
#
