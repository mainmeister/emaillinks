"""
EmailLinks - Scan emails for technical stories, summarize links for Mastodon,
and provide an interactive interface to review, edit, and publish posts.
"""

from __future__ import annotations

import argparse
import email
from email.header import decode_header
import html
import imaplib
import logging
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
import webbrowser

import requests
from bs4 import BeautifulSoup

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("emaillinks")

# Ensure UTF-8 output on standard streams where possible
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MASTODON_CHAR_LIMIT = 500
DEFAULT_EMAIL_USER = "mainmeister@gmail.com"
DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_MASTODON_INSTANCE = "https://mastodon.social"


@dataclass
class MastodonConfig:
    """Configuration for Mastodon publishing."""
    instance_url: str = DEFAULT_MASTODON_INSTANCE
    access_token: str = field(
        default_factory=lambda: os.getenv("MASTODON_ACCESS_TOKEN", "")
    )
    visibility: str = "public"  # public, unlisted, private, direct


@dataclass
class EmailConfig:
    """Configuration for IMAP email retrieval."""
    username: str = DEFAULT_EMAIL_USER
    password: str = field(
        default_factory=lambda: os.getenv(
            "EMAIL_PASSWORD", os.getenv("GMAIL_APP_PASSWORD", "")
        )
    )
    host: str = DEFAULT_IMAP_HOST
    port: int = DEFAULT_IMAP_PORT
    folder: str = "INBOX"
    search_criteria: str = "ALL"


@dataclass
class StoryItem:
    """Data model representing a discovered technical story with link."""
    url: str
    title: str = ""
    source_email_subject: str = ""
    source_email_from: str = ""
    context_snippet: str = ""
    web_title: str = ""
    web_description: str = ""
    post_description: str = ""
    selected: bool = True  # Yes/No toggle for posting
    is_fetched: bool = False
    status: str = "pending"  # pending, fetched, posted, error, skipped
    posted_status_id: Optional[str] = None
    error_message: Optional[str] = None

    def character_count(self) -> int:
        """Return the length of the Mastodon post description."""
        return len(self.post_description)

    def is_within_limit(self) -> bool:
        """Check if post is within Mastodon's 500-character limit."""
        return len(self.post_description) <= MASTODON_CHAR_LIMIT


# ---------------------------------------------------------------------------
# Email Reading
# ---------------------------------------------------------------------------

class EmailReader:
    """Reads and parses emails via IMAP or local samples."""

    def __init__(self, config: Optional[EmailConfig] = None):
        self.config = config or EmailConfig()

    def connect_and_fetch(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        folder: Optional[str] = None,
        search_criteria: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Connect to IMAP server and fetch parsed emails."""
        user = username or self.config.username
        pwd = password or self.config.password
        server_host = host or self.config.host
        server_port = port or self.config.port
        target_folder = folder or self.config.folder
        criteria = search_criteria or self.config.search_criteria

        if not pwd:
            logger.warning(
                "No email password provided. Set EMAIL_PASSWORD or GMAIL_APP_PASSWORD."
            )
            raise ValueError(
                "Email password/app password is required to connect to IMAP."
            )

        logger.info(f"Connecting to {server_host}:{server_port} for {user}...")
        mail = imaplib.IMAP4_SSL(server_host, server_port)
        try:
            mail.login(user, pwd)
            mail.select(target_folder, readonly=True)

            status, search_data = mail.search(None, criteria)
            if status != "OK" or not search_data or not search_data[0]:
                logger.info(f"No emails found matching criteria: {criteria}")
                return []

            email_ids = search_data[0].split()
            # Fetch most recent emails up to limit
            selected_ids = email_ids[-limit:] if len(email_ids) > limit else email_ids
            selected_ids.reverse()  # Newest first

            results: List[Dict[str, Any]] = []
            for eid in selected_ids:
                try:
                    res, msg_data = mail.fetch(eid, "(RFC822)")
                    if res == "OK" and msg_data and msg_data[0]:
                        raw_email = msg_data[0][1]
                        if isinstance(raw_email, bytes):
                            parsed = self.parse_raw_email(raw_email, msg_id=eid.decode("utf-8", errors="ignore"))
                            results.append(parsed)
                except Exception as e:
                    logger.error(f"Error fetching email ID {eid}: {e}")
            return results
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    @staticmethod
    def _decode_header_str(header_value: Optional[str]) -> str:
        """Decode MIME encoded-word header strings."""
        if not header_value:
            return ""
        decoded_parts = decode_header(header_value)
        text_parts = []
        for part, encoding in decoded_parts:
            if isinstance(part, bytes):
                enc = encoding or "utf-8"
                try:
                    text_parts.append(part.decode(enc, errors="replace"))
                except (LookupError, UnicodeDecodeError):
                    text_parts.append(part.decode("utf-8", errors="replace"))
            else:
                text_parts.append(str(part))
        return "".join(text_parts)

    def parse_raw_email(self, raw_bytes: bytes, msg_id: str = "") -> Dict[str, Any]:
        """Parse raw RFC822 email bytes into a structured dictionary."""
        msg = email.message_from_bytes(raw_bytes)
        subject = self._decode_header_str(msg.get("Subject", "(No Subject)"))
        from_addr = self._decode_header_str(msg.get("From", ""))
        date_str = self._decode_header_str(msg.get("Date", ""))

        body_text_parts: List[str] = []
        body_html_parts: List[str] = []

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))
                if "attachment" in content_disposition.lower():
                    continue

                payload = part.get_payload(decode=True)
                if not payload:
                    continue

                charset = part.get_content_charset() or "utf-8"
                try:
                    decoded = payload.decode(charset, errors="replace")
                except (LookupError, UnicodeDecodeError):
                    decoded = payload.decode("utf-8", errors="replace")

                if content_type == "text/plain":
                    body_text_parts.append(decoded)
                elif content_type == "text/html":
                    body_html_parts.append(decoded)
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                try:
                    decoded = payload.decode(charset, errors="replace")
                except (LookupError, UnicodeDecodeError):
                    decoded = payload.decode("utf-8", errors="replace")
                if msg.get_content_type() == "text/html":
                    body_html_parts.append(decoded)
                else:
                    body_text_parts.append(decoded)

        return {
            "id": msg_id,
            "subject": subject,
            "from": from_addr,
            "date": date_str,
            "body_text": "\n".join(body_text_parts),
            "body_html": "\n".join(body_html_parts),
        }

    @staticmethod
    def get_sample_emails() -> List[Dict[str, Any]]:
        """Return realistic sample emails containing technical stories and links."""
        return [
            {
                "id": "sample-1",
                "subject": "Tech Weekly: Python 3.13 JIT, Rust in the Linux Kernel, and LLM Optimizations",
                "from": "editor@techweekly.dev",
                "date": "Wed, 14 Oct 2026 09:30:00 -0400",
                "body_text": (
                    "Welcome to this week's edition of Tech Weekly!\n\n"
                    "Top stories for today:\n\n"
                    "1. Python 3.13 introduces an experimental JIT compiler that boosts CPU-bound workloads.\n"
                    "Read more: https://docs.python.org/3/whatsnew/3.13.html\n\n"
                    "2. Rust continues expanding inside the Linux kernel with new device driver abstractions.\n"
                    "Link: https://lwn.net/Articles/rust-linux-kernel/\n\n"
                    "3. Open Source LLM inference optimization techniques reducing memory footprint.\n"
                    "Deep dive: https://github.com/vllm-project/vllm\n\n"
                    "To unsubscribe, visit https://techweekly.dev/unsubscribe?id=123"
                ),
                "body_html": (
                    "<html><body>"
                    "<h1>Tech Weekly</h1>"
                    "<p>Top technical highlights for developers:</p>"
                    "<ul>"
                    "<li><a href='https://docs.python.org/3/whatsnew/3.13.html'>Python 3.13 Release Notes: Experimental JIT Compiler</a></li>"
                    "<li><a href='https://lwn.net/Articles/rust-linux-kernel/'>Rust in the Linux Kernel: Practical Driver Development</a></li>"
                    "<li><a href='https://github.com/vllm-project/vllm'>vLLM: High-Throughput and Memory-Efficient LLM Serving</a></li>"
                    "</ul>"
                    "<p><a href='https://techweekly.dev/unsubscribe?id=123'>Unsubscribe</a></p>"
                    "</body></html>"
                ),
            },
            {
                "id": "sample-2",
                "subject": "DevOps & Cloud Digest: Modern Kubernetes Architecture and PostgreSQL Scaling",
                "from": "digest@cloudnative.io",
                "date": "Wed, 14 Oct 2026 14:15:00 -0400",
                "body_text": (
                    "Hi mainmeister,\n\n"
                    "Here are the top engineering articles selected for you:\n\n"
                    "Scaling PostgreSQL to 100,000 queries per second with connection pooling and logical replication:\n"
                    "https://www.postgresql.org/docs/current/high-availability.html\n\n"
                    "Kubernetes Multi-Cluster Networking Patterns and Service Meshes:\n"
                    "https://kubernetes.io/docs/concepts/services-networking/\n\n"
                    "Best,\nCloud Native Team"
                ),
                "body_html": (
                    "<html><body>"
                    "<h2>Cloud Native Digest</h2>"
                    "<p>Check out these articles on backend scalability:</p>"
                    "<p><a href='https://www.postgresql.org/docs/current/high-availability.html'>High Availability and Scaling in PostgreSQL</a></p>"
                    "<p><a href='https://kubernetes.io/docs/concepts/services-networking/'>Kubernetes Networking and Service Meshes</a></p>"
                    "</body></html>"
                ),
            },
            {
                "id": "sample-3",
                "subject": "Cybersecurity & Systems Update: Hardware Vulnerabilities and Zero-Trust Networks",
                "from": "security@sysbulletin.org",
                "date": "Wed, 14 Oct 2026 16:00:00 -0400",
                "body_text": (
                    "Latest research on CPU side-channel attacks and memory safety:\n"
                    "https://cve.org/cverecord\n\n"
                    "Mozilla's guide to modern WebAssembly security isolation:\n"
                    "https://developer.mozilla.org/en-US/docs/WebAssembly\n"
                ),
                "body_html": (
                    "<html><body>"
                    "<p><a href='https://developer.mozilla.org/en-US/docs/WebAssembly'>WebAssembly Security and Sandbox Architecture</a></p>"
                    "</body></html>"
                ),
            },
        ]


# ---------------------------------------------------------------------------
# Technical Story & Link Extraction
# ---------------------------------------------------------------------------

class StoryExtractor:
    """Extracts and filters technical stories and URLs from emails."""

    # Keywords indicative of technical stories and engineering topics
    TECH_KEYWORDS = {
        "python", "rust", "golang", "go", "javascript", "typescript", "c++", "c#",
        "java", "linux", "kernel", "docker", "kubernetes", "k8s", "cloud", "aws",
        "azure", "gcp", "ai", "llm", "machine learning", "deep learning", "neural",
        "database", "sql", "postgres", "postgresql", "sqlite", "redis", "nosql",
        "api", "rest", "graphql", "grpc", "microservices", "architecture",
        "security", "vulnerability", "cve", "cryptography", "compiler", "llvm",
        "wasm", "webassembly", "open source", "devops", "ci/cd", "performance",
        "benchmark", "algorithm", "data structure", "frontend", "backend",
        "framework", "library", "git", "github", "gitlab", "serverless",
        "hardware", "cpu", "gpu", "networking", "protocol", "engineering",
        "software", "programming", "code", "developer", "sysadmin", "tech"
    }

    # Domains commonly associated with technical content
    TECH_DOMAINS = {
        "github.com", "gitlab.com", "dev.to", "medium.com", "news.ycombinator.com",
        "lwn.net", "stackoverflow.com", "arstechnica.com", "theverge.com",
        "techcrunch.com", "infoq.com", "thenewstack.io", "lobste.rs",
        "slashdot.org", "bleepingcomputer.com", "tomshardware.com", "anandtech.com",
        "docs.python.org", "rust-lang.org", "golang.org", "kubernetes.io",
        "postgresql.org", "developer.mozilla.org", "kernel.org", "arxiv.org",
        "huggingface.co", "apache.org", "w3.org", "ietf.org", "eff.org"
    }

    # Substrings or query parameters indicating junk / non-story links
    IGNORE_PATTERNS = [
        r"unsubscribe",
        r"optout",
        r"opt-out",
        r"email-preferences",
        r"manage-preferences",
        r"privacy-policy",
        r"terms-of-service",
        r"terms-and-conditions",
        r"view-in-browser",
        r"click\.track",
        r"list-manage\.com",
        r"twitter\.com/intent",
        r"facebook\.com/sharer",
        r"linkedin\.com/shareArticle",
        r"mailto:",
        r"javascript:",
        r"tel:",
        r"\.(?:png|jpg|jpeg|gif|svg|webp|ico|css|js|woff|woff2|ttf|eot)$",
    ]

    URL_REGEX = re.compile(
        r"https?://(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?::\d+)?(?:/[^\s<>'\"()]*)?",
        re.IGNORECASE
    )

    @classmethod
    def clean_url(cls, raw_url: str) -> str:
        """Clean and normalize URL, unwrapping common redirects and stripping tracking query parameters."""
        url = raw_url.strip().rstrip(".,;:!?)>\"'")
        parsed = urllib.parse.urlparse(url)

        # Check for wrapped redirects (e.g. ?url=... or ?u=...)
        query_params = urllib.parse.parse_qs(parsed.query)
        for key in ["url", "u", "dest", "target", "link"]:
            if key in query_params and query_params[key]:
                target = query_params[key][0]
                if target.startswith("http://") or target.startswith("https://"):
                    return cls.clean_url(target)

        # Remove tracking query parameters
        tracking_params = {
            "utm_source", "utm_medium", "utm_campaign", "utm_term",
            "utm_content", "ref", "source", "fbclid", "gclid", "mc_cid", "mc_eid"
        }
        filtered_query = [
            (k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in tracking_params
        ]
        new_query = urllib.parse.urlencode(filtered_query)
        cleaned = urllib.parse.urlunparse((
            parsed.scheme,
            parsed.netloc.lower(),
            parsed.path,
            parsed.params,
            new_query,
            ""  # Strip fragment
        ))
        return cleaned

    @classmethod
    def should_ignore_url(cls, url: str) -> bool:
        """Determine whether a URL should be filtered out as non-story."""
        if not (url.startswith("http://") or url.startswith("https://")):
            return True
        for pattern in cls.IGNORE_PATTERNS:
            if re.search(pattern, url, re.IGNORECASE):
                return True
        return False

    @classmethod
    def score_technical_relevance(
        cls, url: str, anchor_text: str, context: str, email_subject: str
    ) -> int:
        """Calculate a technical relevance score for a given link."""
        score = 0
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc.lower()

        # Domain matches
        for td in cls.TECH_DOMAINS:
            if td in domain:
                score += 5
                break
        if any(domain.endswith(tld) for tld in [".io", ".dev", ".ai", ".tech", ".org"]):
            score += 2

        combined_text = f"{url} {anchor_text} {context} {email_subject}".lower()
        for kw in cls.TECH_KEYWORDS:
            # Word boundary matching for short keywords
            if len(kw) <= 3:
                if re.search(rf"\b{re.escape(kw)}\b", combined_text):
                    score += 2
            else:
                if kw in combined_text:
                    score += 2

        return score

    def extract_from_email(self, email_dict: Dict[str, Any]) -> List[StoryItem]:
        """Extract all technical story links from a single email."""
        found_links: Dict[str, StoryItem] = {}
        subject = email_dict.get("subject", "")
        from_addr = email_dict.get("from", "")
        body_html = email_dict.get("body_html", "")
        body_text = email_dict.get("body_text", "")

        # 1. Parse HTML links if available
        if body_html:
            try:
                soup = BeautifulSoup(body_html, "html.parser")
                for a_tag in soup.find_all("a", href=True):
                    raw_href = a_tag["href"]
                    cleaned = self.clean_url(raw_href)
                    if self.should_ignore_url(cleaned):
                        continue

                    anchor_text = a_tag.get_text(separator=" ", strip=True)
                    # Extract parent paragraph or container as context
                    parent_text = ""
                    parent = a_tag.parent
                    if parent and parent.name in ["p", "li", "div", "td", "h1", "h2", "h3", "h4"]:
                        parent_text = parent.get_text(separator=" ", strip=True)

                    score = self.score_technical_relevance(cleaned, anchor_text, parent_text, subject)
                    if score > 0 and cleaned not in found_links:
                        title = anchor_text if len(anchor_text) > 3 else parent_text[:80]
                        found_links[cleaned] = StoryItem(
                            url=cleaned,
                            title=title or "Technical Story",
                            source_email_subject=subject,
                            source_email_from=from_addr,
                            context_snippet=parent_text or anchor_text,
                        )
            except Exception as e:
                logger.error(f"Error parsing HTML in email '{subject}': {e}")

        # 2. Parse plain text links
        if body_text:
            lines = body_text.splitlines()
            for i, line in enumerate(lines):
                matches = self.URL_REGEX.findall(line)
                for raw_url in matches:
                    cleaned = self.clean_url(raw_url)
                    if self.should_ignore_url(cleaned) or cleaned in found_links:
                        continue

                    # Context is current line and neighboring lines
                    surrounding = []
                    if i > 0:
                        surrounding.append(lines[i - 1].strip())
                    surrounding.append(line.strip())
                    if i + 1 < len(lines):
                        surrounding.append(lines[i + 1].strip())
                    context_str = " ".join([s for s in surrounding if s])

                    score = self.score_technical_relevance(cleaned, "", context_str, subject)
                    if score > 0 and cleaned not in found_links:
                        found_links[cleaned] = StoryItem(
                            url=cleaned,
                            title=lines[i - 1].strip() if i > 0 and len(lines[i - 1].strip()) > 5 else "Technical Story",
                            source_email_subject=subject,
                            source_email_from=from_addr,
                            context_snippet=context_str,
                        )

        return list(found_links.values())

    def extract_stories(self, emails: List[Dict[str, Any]]) -> List[StoryItem]:
        """Extract all technical story links across all provided emails."""
        all_stories: List[StoryItem] = []
        seen_urls = set()

        for em in emails:
            stories = self.extract_from_email(em)
            for story in stories:
                if story.url not in seen_urls:
                    seen_urls.add(story.url)
                    all_stories.append(story)

        logger.info(f"Extracted {len(all_stories)} technical stories from {len(emails)} emails.")
        return all_stories


# ---------------------------------------------------------------------------
# Link Summarization & 500-Character Mastodon Post Generator
# ---------------------------------------------------------------------------

class LinkSummarizer:
    """Fetches web pages and generates high-utility Mastodon posts <= 500 chars."""

    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )

    def __init__(self, timeout: int = 10, session: Optional[requests.Session] = None):
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": self.DEFAULT_USER_AGENT})

    def fetch_page_content(self, url: str) -> Tuple[str, str, str]:
        """
        Fetch webpage and extract title, description, and article summary text.
        Returns (title, description, article_snippet).
        """
        try:
            resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            resp.raise_for_status()
            html_text = resp.text
            soup = BeautifulSoup(html_text, "html.parser")

            # Extract title
            title = ""
            og_title = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "twitter:title"})
            if og_title and og_title.get("content"):
                title = og_title["content"].strip()
            elif soup.title and soup.title.string:
                title = soup.title.string.strip()
            elif soup.find("h1"):
                title = soup.find("h1").get_text(strip=True)

            title = html.unescape(title)
            # Clean up common title suffixes (e.g. " | Hacker News", " - GitHub")
            title = re.sub(r"\s+[\|\-–—]\s+[^\|\-–—]+$", "", title).strip()

            # Extract description
            desc = ""
            og_desc = (
                soup.find("meta", property="og:description")
                or soup.find("meta", attrs={"name": "twitter:description"})
                or soup.find("meta", attrs={"name": "description"})
            )
            if og_desc and og_desc.get("content"):
                desc = html.unescape(og_desc["content"].strip())

            # Extract article content snippet
            paragraphs: List[str] = []
            article_tag = soup.find("article") or soup.find("main") or soup
            for p in article_tag.find_all("p"):
                p_text = html.unescape(p.get_text(separator=" ", strip=True))
                if len(p_text) > 40:
                    paragraphs.append(p_text)

            article_snippet = " ".join(paragraphs[:3]) if paragraphs else ""
            return title or "Technical Story", desc, article_snippet
        except Exception as e:
            logger.warning(f"Could not fetch or parse {url}: {e}")
            return "", "", ""

    @staticmethod
    def generate_hashtags(title: str, text: str, url: str) -> str:
        """Generate relevant tech hashtags based on content."""
        combined = f"{title} {text} {url}".lower()
        tags = ["#Tech"]
        tag_map = [
            ("python", "#Python"),
            ("rust", "#RustLang"),
            ("golang", "#Golang"),
            ("linux", "#Linux"),
            ("docker", "#Docker"),
            ("kubernetes", "#Kubernetes"),
            ("k8s", "#Kubernetes"),
            ("ai", "#AI"),
            ("llm", "#AI"),
            ("machine learning", "#MachineLearning"),
            ("postgres", "#PostgreSQL"),
            ("database", "#Database"),
            ("security", "#Security"),
            ("github", "#OpenSource"),
            ("open source", "#OpenSource"),
            ("webassembly", "#Wasm"),
            ("wasm", "#Wasm"),
            ("devops", "#DevOps"),
            ("architecture", "#SoftwareEngineering"),
            ("programming", "#Programming"),
        ]
        for keyword, tag in tag_map:
            if keyword in combined and tag not in tags:
                tags.append(tag)
            if len(tags) >= 4:
                break

        if len(tags) < 2:
            tags.append("#Programming")
        return " ".join(tags)

    def create_mastodon_post(
        self,
        url: str,
        title: str = "",
        description: str = "",
        snippet: str = "",
        max_chars: int = MASTODON_CHAR_LIMIT,
    ) -> str:
        """
        Synthesize an engaging, high-utility Mastodon post strictly <= 500 characters.
        """
        clean_title = (title or "Technical Story").strip()
        body_summary = (description or snippet or "").strip()
        # Clean multi-spaces and newlines
        body_summary = re.sub(r"\s+", " ", body_summary)

        hashtags = self.generate_hashtags(clean_title, body_summary, url)

        # Base post structure:
        # Title
        #
        # Summary
        #
        # URL
        # Hashtags

        template_fixed_len = len(clean_title) + len(url) + len(hashtags) + 6  # newline separators

        # If title alone is too long, truncate it
        if template_fixed_len > max_chars:
            excess = template_fixed_len - max_chars + 3
            clean_title = clean_title[:-excess] + "..."
            template_fixed_len = len(clean_title) + len(url) + len(hashtags) + 6

        available_for_summary = max_chars - template_fixed_len

        if available_for_summary > 20 and body_summary:
            if len(body_summary) > available_for_summary:
                # Trim cleanly at word boundary
                trimmed = body_summary[: available_for_summary - 3]
                last_space = trimmed.rfind(" ")
                if last_space > 20:
                    trimmed = trimmed[:last_space]
                body_summary = trimmed.rstrip(".,;:- ") + "..."

            post = f"{clean_title}\n\n{body_summary}\n\n{url}\n{hashtags}"
        else:
            post = f"{clean_title}\n\n{url}\n{hashtags}"

        # Double check hard limit
        if len(post) > max_chars:
            post = post[: max_chars - 3] + "..."

        return post

    def summarize_story(self, story: StoryItem) -> StoryItem:
        """Fetch URL content and generate a 500-character Mastodon post for the story."""
        logger.info(f"Opening link and creating Mastodon description for: {story.url}")
        web_title, web_desc, web_snippet = self.fetch_page_content(story.url)

        story.web_title = web_title
        story.web_description = web_desc
        effective_title = web_title or story.title or "Technical Story"
        effective_desc = web_desc or web_snippet or story.context_snippet

        story.post_description = self.create_mastodon_post(
            url=story.url,
            title=effective_title,
            description=effective_desc,
            snippet=web_snippet or story.context_snippet,
        )
        story.is_fetched = True
        story.status = "ready"
        return story


# ---------------------------------------------------------------------------
# Mastodon API Client
# ---------------------------------------------------------------------------

class MastodonClient:
    """Client for publishing statuses to Mastodon."""

    def __init__(self, config: Optional[MastodonConfig] = None):
        self.config = config or MastodonConfig()

    def post_status(
        self,
        status_text: str,
        instance_url: Optional[str] = None,
        access_token: Optional[str] = None,
        visibility: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Post a single status update to Mastodon."""
        instance = (instance_url or self.config.instance_url).rstrip("/")
        token = access_token or self.config.access_token
        vis = visibility or self.config.visibility

        if len(status_text) > MASTODON_CHAR_LIMIT:
            raise ValueError(
                f"Status exceeds Mastodon limit ({len(status_text)} > {MASTODON_CHAR_LIMIT} chars)."
            )

        if dry_run or not token:
            logger.info(f"[DRY-RUN / MOCK POST to {instance}]:\n{status_text}")
            return {
                "id": "mock-status-" + str(abs(hash(status_text)) % 100000),
                "url": f"{instance}/@user/mock-{abs(hash(status_text)) % 100000}",
                "content": status_text,
                "visibility": vis,
                "created_at": "2026-10-14T20:11:00.000Z",
                "mock": True,
            }

        endpoint = f"{instance}/api/v1/statuses"
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "EmailLinksMastodonBot/1.0",
        }
        payload = {
            "status": status_text,
            "visibility": vis,
        }

        resp = requests.post(endpoint, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def post_selected_stories(
        self,
        stories: List[StoryItem],
        dry_run: bool = False,
        progress_callback: Optional[Callable[[int, int, StoryItem], None]] = None,
    ) -> Dict[str, Any]:
        """Publish all stories where selected is True."""
        selected_stories = [s for s in stories if s.selected]
        total = len(selected_stories)
        logger.info(f"Publishing {total} selected stories to Mastodon...")

        posted: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []

        for idx, story in enumerate(selected_stories, 1):
            if not story.post_description.strip():
                story.status = "error"
                story.error_message = "Post description is empty."
                errors.append({"story": story, "error": "Empty description"})
                continue

            try:
                result = self.post_status(
                    status_text=story.post_description,
                    dry_run=dry_run,
                )
                story.status = "posted"
                story.posted_status_id = str(result.get("id", ""))
                story.error_message = None
                posted.append({"story": story, "result": result})
                logger.info(f"Successfully posted story: {story.url}")
            except Exception as e:
                story.status = "error"
                story.error_message = str(e)
                errors.append({"story": story, "error": str(e)})
                logger.error(f"Failed to post story {story.url}: {e}")

            if progress_callback:
                progress_callback(idx, total, story)

        return {
            "total_selected": total,
            "posted_count": len(posted),
            "error_count": len(errors),
            "posted": posted,
            "errors": errors,
        }


# ---------------------------------------------------------------------------
# Interactive User Interface (Tkinter GUI)
# ---------------------------------------------------------------------------

class EmailLinksApp:
    """
    Tkinter-based User Interface allowing the user to:
    1. Peruse technical stories with links extracted from emails
    2. Toggle Yes/No for each post
    3. Open links and review auto-generated 500-char Mastodon descriptions
    4. Interactively edit descriptions with real-time character counting
    5. Click a single 'Post' button to create all selected posts, or 'Cancel' to close.
    """

    def __init__(
        self,
        root: Any,
        stories: Optional[List[StoryItem]] = None,
        email_config: Optional[EmailConfig] = None,
        mastodon_config: Optional[MastodonConfig] = None,
        dry_run: bool = False,
    ):
        self.root = root
        self.root.title("Email Links - Technical Stories Mastodon Publisher")
        self.root.geometry("960x780")
        self.root.minsize(700, 500)

        self.email_config = email_config or EmailConfig()
        self.mastodon_config = mastodon_config or MastodonConfig()
        self.dry_run = dry_run

        self.reader = EmailReader(self.email_config)
        self.extractor = StoryExtractor()
        self.summarizer = LinkSummarizer()
        self.mastodon = MastodonClient(self.mastodon_config)

        self.stories: List[StoryItem] = stories or []
        self.story_widgets: List[Dict[str, Any]] = []

        self._build_ui()

        if self.stories:
            self._render_story_list()
        else:
            self.load_sample_stories()

    def _build_ui(self) -> None:
        """Construct all UI components."""
        import tkinter as tk
        from tkinter import ttk, messagebox

        # Top Header & Source Bar
        header_frame = ttk.Frame(self.root, padding="10")
        header_frame.pack(fill=tk.X, side=tk.TOP)

        title_label = ttk.Label(
            header_frame,
            text="Technical Stories from Email for Mastodon",
            font=("Helvetica", 15, "bold")
        )
        title_label.pack(anchor=tk.W)

        subtitle_label = ttk.Label(
            header_frame,
            text=f"Reading emails for: {self.email_config.username} | Review, edit, and toggle posts below.",
            font=("Helvetica", 9),
            foreground="#555555"
        )
        subtitle_label.pack(anchor=tk.W, pady=(2, 8))

        # Controls row
        ctrl_frame = ttk.Frame(header_frame)
        ctrl_frame.pack(fill=tk.X)

        self.fetch_email_btn = ttk.Button(
            ctrl_frame, text="Fetch from Gmail", command=self._prompt_and_fetch_emails
        )
        self.fetch_email_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.load_sample_btn = ttk.Button(
            ctrl_frame, text="Load Sample Stories", command=self.load_sample_stories
        )
        self.load_sample_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.select_all_btn = ttk.Button(
            ctrl_frame, text="Select All (Yes)", command=self._select_all
        )
        self.select_all_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.deselect_all_btn = ttk.Button(
            ctrl_frame, text="Deselect All (No)", command=self._deselect_all
        )
        self.deselect_all_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.fetch_all_summaries_btn = ttk.Button(
            ctrl_frame, text="Fetch All Link Summaries", command=self._fetch_all_summaries
        )
        self.fetch_all_summaries_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.stats_label = ttk.Label(ctrl_frame, text="0 stories found", font=("Helvetica", 9, "bold"))
        self.stats_label.pack(side=tk.RIGHT, padx=5)

        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=10, pady=4)

        # Scrollable Story List Area
        container = ttk.Frame(self.root)
        container.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        self.canvas = tk.Canvas(container, highlightthickness=0, bg="#f8f9fa")
        self.scrollbar = ttk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas)

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        self.canvas_window = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfig(self.canvas_window, width=e.width)
        )
        self.canvas.configure(xscrollcommand=None, yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Mousewheel scrolling
        def _on_mousewheel(event: Any) -> None:
            if event.num == 5 or event.delta < 0:
                self.canvas.yview_scroll(1, "units")
            elif event.num == 4 or event.delta > 0:
                self.canvas.yview_scroll(-1, "units")

        self.canvas.bind_all("<MouseWheel>", _on_mousewheel)
        self.canvas.bind_all("<Button-4>", _on_mousewheel)
        self.canvas.bind_all("<Button-5>", _on_mousewheel)

        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=10, pady=4)

        # Bottom Action Bar
        bottom_frame = ttk.Frame(self.root, padding="10")
        bottom_frame.pack(fill=tk.X, side=tk.BOTTOM)

        self.status_bar_label = ttk.Label(
            bottom_frame, text="Ready.", font=("Helvetica", 9), foreground="#333333"
        )
        self.status_bar_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.cancel_btn = ttk.Button(
            bottom_frame, text="Cancel", command=self.cancel_and_close
        )
        self.cancel_btn.pack(side=tk.RIGHT, padx=(6, 0))

        self.post_btn = ttk.Button(
            bottom_frame,
            text="Post Selected to Mastodon",
            command=self.post_all_selected,
            style="Accent.TButton" if "Accent.TButton" in ttk.Style().theme_names() else "TButton"
        )
        self.post_btn.pack(side=tk.RIGHT, padx=(0, 6))

    def _update_stats(self) -> None:
        """Update header and footer counts."""
        total = len(self.stories)
        selected_count = sum(1 for s in self.stories if s.selected)
        self.stats_label.config(
            text=f"{total} stories found | {selected_count} selected (Yes)"
        )

    def _render_story_list(self) -> None:
        """Render all story cards in the scrollable view."""
        import tkinter as tk
        from tkinter import ttk

        # Clear existing widgets
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()
        self.story_widgets.clear()

        if not self.stories:
            empty_lbl = ttk.Label(
                self.scrollable_frame,
                text="No technical stories found yet. Click 'Fetch from Gmail' or 'Load Sample Stories'.",
                font=("Helvetica", 11, "italic"),
                padding=20
            )
            empty_lbl.pack(anchor=tk.CENTER, pady=30)
            self._update_stats()
            return

        for idx, story in enumerate(self.stories):
            card = ttk.LabelFrame(
                self.scrollable_frame,
                text=f"Story #{idx + 1}: {story.title[:60] if story.title else 'Technical Story'}",
                padding="8"
            )
            card.pack(fill=tk.X, expand=True, padx=8, pady=6)

            # Top row inside card: Yes/No toggle + Link Button + Email Source
            top_row = ttk.Frame(card)
            top_row.pack(fill=tk.X, pady=(0, 4))

            # Toggle Yes/No
            toggle_var = tk.BooleanVar(value=story.selected)

            def make_toggle_handler(s: StoryItem, var: tk.BooleanVar):
                def handler():
                    s.selected = var.get()
                    self._update_stats()
                return handler

            toggle_cb = ttk.Checkbutton(
                top_row,
                text="Yes (Post to Mastodon)",
                variable=toggle_var,
                command=make_toggle_handler(story, toggle_var)
            )
            toggle_cb.pack(side=tk.LEFT, padx=(0, 10))

            # Open link button
            def make_open_link_handler(url: str):
                return lambda: webbrowser.open(url)

            open_link_btn = ttk.Button(
                top_row,
                text="Open Link",
                command=make_open_link_handler(story.url)
            )
            open_link_btn.pack(side=tk.LEFT, padx=(0, 8))

            # URL display
            url_lbl = ttk.Label(
                top_row,
                text=story.url,
                font=("Helvetica", 8),
                foreground="#0066cc",
                cursor="hand2"
            )
            url_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
            url_lbl.bind("<Button-1>", lambda e, u=story.url: webbrowser.open(u))

            # Email source label
            if story.source_email_subject:
                src_lbl = ttk.Label(
                    card,
                    text=f"Source: {story.source_email_subject}",
                    font=("Helvetica", 8, "italic"),
                    foreground="#666666"
                )
                src_lbl.pack(anchor=tk.W, pady=(0, 4))

            # Middle: Mastodon Post Description editor
            desc_frame = ttk.Frame(card)
            desc_frame.pack(fill=tk.BOTH, expand=True, pady=(2, 2))

            desc_label_frame = ttk.Frame(desc_frame)
            desc_label_frame.pack(fill=tk.X)

            ttk.Label(
                desc_label_frame,
                text="Mastodon Post Description (≤ 500 chars):",
                font=("Helvetica", 9, "bold")
            ).pack(side=tk.LEFT)

            char_counter_lbl = ttk.Label(
                desc_label_frame,
                text=f"{story.character_count()}/{MASTODON_CHAR_LIMIT}",
                font=("Helvetica", 9, "bold"),
                foreground="#28a745" if story.is_within_limit() else "#dc3545"
            )
            char_counter_lbl.pack(side=tk.RIGHT)

            text_box = tk.Text(
                desc_frame,
                height=4,
                wrap=tk.WORD,
                font=("Courier", 9),
                padx=4,
                pady=4,
                relief=tk.SOLID,
                borderwidth=1
            )
            text_box.insert("1.0", story.post_description)
            text_box.pack(fill=tk.BOTH, expand=True, pady=(2, 4))

            def make_text_change_handler(s: StoryItem, tb: tk.Text, counter_lbl: ttk.Label):
                def handler(event=None):
                    content = tb.get("1.0", "end-1c")
                    s.post_description = content
                    count = len(content)
                    counter_lbl.config(
                        text=f"{count}/{MASTODON_CHAR_LIMIT}",
                        foreground="#28a745" if count <= MASTODON_CHAR_LIMIT else "#dc3545"
                    )
                return handler

            text_handler = make_text_change_handler(story, text_box, char_counter_lbl)
            text_box.bind("<KeyRelease>", text_handler)

            # Card bottom actions: Re-fetch summary button
            card_actions = ttk.Frame(card)
            card_actions.pack(fill=tk.X, pady=(2, 0))

            def make_refetch_handler(s: StoryItem, tb: tk.Text, counter_lbl: ttk.Label):
                def handler():
                    self.status_bar_label.config(text=f"Fetching content for {s.url}...")
                    self.root.update_idletasks()
                    self.summarizer.summarize_story(s)
                    tb.delete("1.0", tk.END)
                    tb.insert("1.0", s.post_description)
                    count = len(s.post_description)
                    counter_lbl.config(
                        text=f"{count}/{MASTODON_CHAR_LIMIT}",
                        foreground="#28a745" if count <= MASTODON_CHAR_LIMIT else "#dc3545"
                    )
                    self.status_bar_label.config(text=f"Summary updated for {s.url}.")
                return handler

            refetch_btn = ttk.Button(
                card_actions,
                text="Re-fetch & Generate 500-char Post",
                command=make_refetch_handler(story, text_box, char_counter_lbl)
            )
            refetch_btn.pack(side=tk.LEFT)

            self.story_widgets.append({
                "story": story,
                "toggle_var": toggle_var,
                "text_box": text_box,
                "char_counter_lbl": char_counter_lbl,
            })

        self._update_stats()

    def _select_all(self) -> None:
        """Toggle all stories to Yes (selected)."""
        for item in self.story_widgets:
            item["story"].selected = True
            item["toggle_var"].set(True)
        self._update_stats()

    def _deselect_all(self) -> None:
        """Toggle all stories to No (deselected)."""
        for item in self.story_widgets:
            item["story"].selected = False
            item["toggle_var"].set(False)
        self._update_stats()

    def _fetch_all_summaries(self) -> None:
        """Fetch web summaries for all selected stories that need description generation."""
        import tkinter as tk
        selected = [item for item in self.story_widgets if item["story"].selected]
        if not selected:
            self.status_bar_label.config(text="No stories selected for fetching.")
            return

        for idx, item in enumerate(selected, 1):
            story = item["story"]
            self.status_bar_label.config(
                text=f"Fetching link [{idx}/{len(selected)}]: {story.url}..."
            )
            self.root.update_idletasks()
            self.summarizer.summarize_story(story)
            tb = item["text_box"]
            tb.delete("1.0", tk.END)
            tb.insert("1.0", story.post_description)
            count = len(story.post_description)
            item["char_counter_lbl"].config(
                text=f"{count}/{MASTODON_CHAR_LIMIT}",
                foreground="#28a745" if count <= MASTODON_CHAR_LIMIT else "#dc3545"
            )

        self.status_bar_label.config(text=f"Fetched summaries for {len(selected)} links.")

    def load_sample_stories(self) -> None:
        """Load sample emails and auto-generate initial 500-char Mastodon descriptions."""
        self.status_bar_label.config(text="Loading sample emails and extracting technical links...")
        self.root.update_idletasks()

        sample_emails = self.reader.get_sample_emails()
        self.stories = self.extractor.extract_stories(sample_emails)

        # Generate initial descriptions for all extracted stories
        for s in self.stories:
            if not s.post_description:
                # Fast local synthesis or fetch
                s.post_description = self.summarizer.create_mastodon_post(
                    url=s.url,
                    title=s.title,
                    snippet=s.context_snippet,
                )
                s.is_fetched = True

        self._render_story_list()
        self.status_bar_label.config(
            text=f"Loaded {len(self.stories)} technical stories from sample emails."
        )

    def _prompt_and_fetch_emails(self) -> None:
        """Prompt for email connection details if needed, and fetch emails."""
        import tkinter as tk
        from tkinter import simpledialog, messagebox

        pwd = self.email_config.password
        if not pwd:
            pwd = simpledialog.askstring(
                "Gmail Password / App Password",
                f"Enter App Password for {self.email_config.username}:",
                show="*",
                parent=self.root
            )
            if not pwd:
                messagebox.showwarning(
                    "Password Required",
                    "Cannot fetch from Gmail without password. You can use 'Load Sample Stories' instead."
                )
                return
            self.email_config.password = pwd

        self.status_bar_label.config(text=f"Connecting to Gmail for {self.email_config.username}...")
        self.root.update_idletasks()

        try:
            emails = self.reader.connect_and_fetch()
            new_stories = self.extractor.extract_stories(emails)
            if not new_stories:
                messagebox.showinfo(
                    "No Links Found",
                    f"Connected successfully, but no technical stories with links were found in the inbox."
                )
                self.status_bar_label.config(text="No technical stories found in inbox.")
                return

            self.stories = new_stories
            # Generate descriptions
            self._fetch_all_summaries()
            self._render_story_list()
            self.status_bar_label.config(
                text=f"Successfully extracted {len(self.stories)} technical stories from Gmail."
            )
        except Exception as e:
            logger.error(f"Gmail fetch failed: {e}")
            messagebox.showerror("Email Fetch Error", f"Failed to fetch emails:\n{e}")
            self.status_bar_label.config(text=f"Error fetching emails: {e}")

    def post_all_selected(self) -> None:
        """Single post button to publish all selected posts to Mastodon."""
        import tkinter as tk
        from tkinter import messagebox

        # Sync text boxes to story models
        for item in self.story_widgets:
            item["story"].post_description = item["text_box"].get("1.0", "end-1c")

        selected = [s for s in self.stories if s.selected]
        if not selected:
            messagebox.showwarning(
                "No Stories Selected",
                "Please toggle 'Yes' for at least one story to post."
            )
            return

        # Check character limits
        over_limit = [s for s in selected if not s.is_within_limit()]
        if over_limit:
            messagebox.showerror(
                "Character Limit Exceeded",
                f"{len(over_limit)} selected post(s) exceed the 500-character Mastodon limit.\n"
                "Please shorten their descriptions before posting."
            )
            return

        confirm = messagebox.askyesno(
            "Confirm Mastodon Posting",
            f"Are you sure you want to publish {len(selected)} post(s) to Mastodon?"
        )
        if not confirm:
            return

        self.status_bar_label.config(text=f"Publishing {len(selected)} post(s) to Mastodon...")
        self.root.update_idletasks()

        results = self.mastodon.post_selected_stories(
            self.stories,
            dry_run=self.dry_run or not self.mastodon_config.access_token
        )

        posted_count = results["posted_count"]
        error_count = results["error_count"]

        if error_count == 0:
            messagebox.showinfo(
                "Posting Complete",
                f"Successfully created {posted_count} Mastodon post(s)!"
            )
            self.status_bar_label.config(text=f"Successfully posted {posted_count} item(s).")
        else:
            messagebox.showwarning(
                "Posting Results",
                f"Posted: {posted_count}\nErrors: {error_count}\nCheck console/logs for details."
            )
            self.status_bar_label.config(
                text=f"Posted {posted_count} item(s) with {error_count} error(s)."
            )

    def cancel_and_close(self) -> None:
        """Cancel and close without posting."""
        logger.info("User clicked Cancel. Closing application without posting.")
        self.root.destroy()


# ---------------------------------------------------------------------------
# CLI Workflow / Programmatic Interface
# ---------------------------------------------------------------------------

def run_workflow_cli(
    email_user: str = DEFAULT_EMAIL_USER,
    email_pass: str = "",
    use_samples: bool = False,
    auto_post: bool = False,
    dry_run: bool = True,
    mastodon_token: str = "",
    mastodon_instance: str = DEFAULT_MASTODON_INSTANCE,
) -> List[StoryItem]:
    """Execute the end-to-end flow from the command line."""
    print("=" * 70)
    print(f"Reading emails for {email_user}...")
    reader = EmailReader(EmailConfig(username=email_user, password=email_pass))

    if use_samples or not email_pass:
        print("Using sample email data...")
        emails = reader.get_sample_emails()
    else:
        emails = reader.connect_and_fetch()

    print(f"Loaded {len(emails)} emails.")

    extractor = StoryExtractor()
    stories = extractor.extract_stories(emails)
    print(f"Discovered {len(stories)} technical stories with links.\n")

    summarizer = LinkSummarizer()
    for idx, story in enumerate(stories, 1):
        print(f"[{idx}] {story.title}")
        print(f"    URL: {story.url}")
        print(f"    Source: {story.source_email_subject}")
        # Generate 500-char Mastodon post
        summarizer.summarize_story(story)
        print(f"    Mastodon Post ({story.character_count()}/500 chars):\n")
        print("    " + story.post_description.replace("\n", "\n    "))
        print("-" * 70)

    if auto_post:
        print(f"\nPublishing selected posts to Mastodon ({mastodon_instance})...")
        client = MastodonClient(
            MastodonConfig(instance_url=mastodon_instance, access_token=mastodon_token)
        )
        res = client.post_selected_stories(stories, dry_run=dry_run)
        print(f"Publishing complete: {res['posted_count']} posted, {res['error_count']} errors.")

    return stories


def main() -> None:
    """Entry point for CLI and GUI modes."""
    parser = argparse.ArgumentParser(description="Scan emails for tech stories & publish to Mastodon")
    parser.add_argument("--email", default=DEFAULT_EMAIL_USER, help="Email address to read from")
    parser.add_argument("--password", default=os.getenv("EMAIL_PASSWORD", ""), help="Email password/app password")
    parser.add_argument("--mastodon-token", default=os.getenv("MASTODON_ACCESS_TOKEN", ""), help="Mastodon API token")
    parser.add_argument("--mastodon-instance", default=DEFAULT_MASTODON_INSTANCE, help="Mastodon instance URL")
    parser.add_argument("--samples", action="store_true", help="Use sample email data directly")
    parser.add_argument("--cli", action="store_true", help="Run in CLI mode without launching GUI")
    parser.add_argument("--auto-post", action="store_true", help="Auto post selected in CLI mode")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Simulate Mastodon posting")

    args = parser.parse_args()

    if args.cli or "DISPLAY" not in os.environ:
        run_workflow_cli(
            email_user=args.email,
            email_pass=args.password,
            use_samples=args.samples or not args.password,
            auto_post=args.auto_post,
            dry_run=args.dry_run,
            mastodon_token=args.mastodon_token,
            mastodon_instance=args.mastodon_instance,
        )
    else:
        try:
            import tkinter as tk
            root = tk.Tk()
            email_cfg = EmailConfig(username=args.email, password=args.password)
            mastodon_cfg = MastodonConfig(
                instance_url=args.mastodon_instance,
                access_token=args.mastodon_token
            )
            app = EmailLinksApp(
                root,
                email_config=email_cfg,
                mastodon_config=mastodon_cfg,
                dry_run=args.dry_run or not args.mastodon_token,
            )
            root.mainloop()
        except Exception as e:
            logger.warning(f"Could not start GUI ({e}), falling back to CLI mode...")
            run_workflow_cli(
                email_user=args.email,
                email_pass=args.password,
                use_samples=True,
                auto_post=args.auto_post,
                dry_run=args.dry_run,
                mastodon_token=args.mastodon_token,
                mastodon_instance=args.mastodon_instance,
            )


if __name__ == "__main__":
    main()
