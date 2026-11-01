# EmailLinks

## 1. Introduction

EmailLinks is a Python application that automatically scans your email inbox for technical articles and stories, extracts the embedded links, generates concise Mastodon-ready posts (within the 500-character limit), and provides an interactive interface to review, edit, and publish those posts to your Mastodon account.

The motivation is simple: developers receive dozens of newsletters, digests, and technical emails every day. Manually curating and sharing the most interesting links on social media is tedious. EmailLinks automates the discovery and drafting phases, leaving you with a polished, editable list of posts that you can approve with a single click.

Key features:
- **IMAP email scanning** — connects to Gmail (or any IMAP server) and reads recent emails.
- **Intelligent link extraction** — parses both HTML and plain-text email bodies, filters out unsubscribe links, tracking URLs, and non-technical content, and scores each link by technical relevance.
- **Automatic post generation** — fetches each linked webpage, extracts the title, description, and article content, then synthesizes an engaging Mastodon post that strictly respects the 500-character limit.
- **Interactive GUI** — a Tkinter-based desktop application where you can browse all discovered stories, toggle which ones to post, edit descriptions with real-time character counting, and publish everything with one button.
- **CLI mode** — a headless command-line workflow for automation, CI, or server environments without a display.
- **Sample data** — built-in sample emails for testing and demonstration without needing a live email account.

---

## 2. User Documentation

### Prerequisites

- **Python 3.13** or later.
- A **Mastodon account** and an **access token** (see below).
- (Optional) A **Gmail account** with an **App Password** if you want to scan real emails.

### Installation

1. Clone or download the project:
   ```bash
   git clone <repository-url>
   cd emaillinks
   ```

2. Install dependencies using `uv` (recommended) or `pip`:
   ```bash
   # Using uv
   uv sync

   # Or using pip
   pip install -e .
   ```

   The project requires two external libraries: `requests` and `beautifulsoup4`.

### Configuration

EmailLinks reads credentials from environment variables or command-line arguments:

| Environment Variable       | Purpose                              | Example                              |
|----------------------------|--------------------------------------|--------------------------------------|
| `EMAIL_PASSWORD`           | Gmail App Password for IMAP access   | `abcd efgh ijkl mnop`               |
| `GMAIL_APP_PASSWORD`       | Alternative name for email password  | `abcd efgh ijkl mnop`               |
| `MASTODON_ACCESS_TOKEN`    | Mastodon API access token            | `eyJhbGci...`                        |

#### Getting a Mastodon Access Token

1. Log in to your Mastodon instance (e.g., `mastodon.social`).
2. Go to **Preferences → Development → Applications**.
3. Fill in the application form (name it "EmailLinks", select `write:statuses` scope).
4. After creating the application, copy the generated access token.
5. Set it as an environment variable or pass it via `--mastodon-token`.

#### Getting a Gmail App Password

1. Go to your Google Account → **Security**.
2. Enable **2-Step Verification** if not already enabled.
3. Go to **App Passwords** and generate a new one for "Mail".
4. Use the 16-character password with EmailLinks.

### Running the Application

#### GUI Mode (Default)

If a display is available, the application launches the interactive desktop interface:

```bash
python emaillinks.py
```

With custom configuration:

```bash
python emaillinks.py \
  --email yourname@gmail.com \
  --password "your-app-password" \
  --mastodon-token "your-access-token" \
  --mastodon-instance "https://mastodon.social"
```

#### CLI Mode

Run headless without a GUI:

```bash
python emaillinks.py --cli
```

#### Using Sample Data

To try the application without connecting to a real email account:

```bash
python emaillinks.py --samples
```

#### Auto-Posting in CLI Mode

Automatically post all discovered stories (in dry-run mode by default):

```bash
python emaillinks.py --cli --samples --auto-post
```

To actually post (not dry-run), set the `--dry-run` flag to `False` and provide a valid Mastodon token:

```bash
python emaillinks.py --cli --samples --auto-post --mastodon-token "your-token" --dry-run False
```

Note: `--dry-run` is `True` by default for safety.

### Command-Line Reference

| Flag                   | Description                                      | Default                  |
|------------------------|--------------------------------------------------|--------------------------|
| `--email`              | Email address to read from                       | `mainmeister@gmail.com`  |
| `--password`           | Email App Password                               | (from env var)           |
| `--mastodon-token`     | Mastodon API access token                        | (from env var)           |
| `--mastodon-instance`  | Mastodon instance URL                            | `https://mastodon.social`|
| `--samples`            | Use built-in sample emails                       | `False`                  |
| `--cli`                | Run in CLI mode (no GUI)                         | `False`                  |
| `--auto-post`          | Automatically post in CLI mode                   | `False`                  |
| `--dry-run`            | Simulate posting without actually publishing     | `True`                   |

### Using the GUI

When the GUI opens, you will see:

1. **Header bar** — shows your email address and a summary of found stories.
2. **Control buttons** (top row):
   - **Fetch from Gmail** — connects to your email and extracts technical links.
   - **Load Sample Stories** — loads built-in sample data for testing.
   - **Select All (Yes)** — marks all stories for posting.
   - **Deselect All (No)** — unmarks all stories.
   - **Fetch All Link Summaries** — fetches webpage content and generates Mastodon post descriptions for all selected stories.
3. **Story cards** (scrollable middle area) — each card shows:
   - A **Yes/No checkbox** to toggle whether the story should be posted.
   - An **Open Link** button to visit the article in your browser.
   - The **URL** (clickable).
   - The **source email subject**.
   - A **text editor** with the auto-generated Mastodon post description.
   - A **real-time character counter** (green = within limit, red = over 500 characters).
   - A **Re-fetch** button to regenerate the post description.
4. **Bottom action bar**:
   - **Status bar** — shows current operation progress.
   - **Post Selected to Mastodon** — publishes all selected stories.
   - **Cancel** — closes the application without posting.

### Workflow

1. Click **Fetch from Gmail** or **Load Sample Stories**.
2. Click **Fetch All Link Summaries** to generate post descriptions.
3. Review each story card, edit descriptions as needed, and toggle Yes/No.
4. Click **Post Selected to Mastodon** to publish.

---

## 3. Technical Discussion

### Architecture Overview

EmailLinks is a single-module Python application (`emaillinks.py`) organized into distinct, loosely-coupled classes, each responsible for one stage of the pipeline:

```
Email Reader → Story Extractor → Link Summarizer → Mastodon Client
                                                        ↑
                                              Interactive GUI / CLI
```

The application supports two execution modes:
- **GUI mode** (`EmailLinksApp`) — a Tkinter-based desktop application.
- **CLI mode** (`run_workflow_cli`) — a headless command-line workflow.

Both modes share the same core classes and data models.

### Data Model

#### `StoryItem` (dataclass)

The central data structure representing a discovered technical story:

| Field                  | Type               | Description                                                    |
|------------------------|---------------------|----------------------------------------------------------------|
| `url`                  | `str`              | The cleaned, normalized URL of the technical story.            |
| `title`                | `str`              | Title extracted from anchor text or email context.             |
| `source_email_subject` | `str`              | Subject line of the email where the link was found.            |
| `source_email_from`    | `str`              | Sender address of the source email.                            |
| `context_snippet`      | `str`              | Surrounding text from the email body near the link.            |
| `web_title`            | `str`              | Title extracted from the linked webpage (Open Graph / HTML).   |
| `web_description`      | `str`              | Description extracted from the linked webpage.                 |
| `post_description`     | `str`              | The final Mastodon post text (≤ 500 characters).               |
| `selected`             | `bool`             | Whether the user has chosen to post this story.                |
| `is_fetched`           | `bool`             | Whether the webpage content has been fetched.                  |
| `status`               | `str`              | Status: `pending`, `ready`, `fetched`, `posted`, `error`, `skipped`. |
| `posted_status_id`     | `Optional[str]`    | Mastodon status ID after successful posting.                   |
| `error_message`        | `Optional[str]`    | Error message if posting failed.                               |

Key methods:
- `character_count()` — returns the length of `post_description`.
- `is_within_limit()` — checks whether the post is within Mastodon's 500-character limit.

#### Configuration Dataclasses

- **`EmailConfig`** — IMAP connection settings (username, password, host, port, folder, search criteria). Password defaults to the `EMAIL_PASSWORD` or `GMAIL_APP_PASSWORD` environment variable.
- **`MastodonConfig`** — Mastodon API settings (instance URL, access token, visibility). Token defaults to the `MASTODON_ACCESS_TOKEN` environment variable.

### Core Components

#### 1. `EmailReader` — Email Retrieval and Parsing

**Purpose:** Connect to an IMAP server, fetch emails, and parse them into structured dictionaries.

**Key methods:**

- `connect_and_fetch()` — establishes an SSL connection to the IMAP server, authenticates, selects the target folder (default: `INBOX`), searches for messages, and fetches the most recent emails up to a configurable limit (default: 50). Emails are returned newest-first. The connection is always closed in a `finally` block.

- `parse_raw_email(raw_bytes, msg_id)` — parses raw RFC822 email bytes into a dictionary with keys: `id`, `subject`, `from`, `date`, `body_text`, `body_html`. Handles:
  - MIME-encoded headers via `decode_header()`.
  - Multipart messages (walks all parts, separates `text/plain` and `text/html`, skips attachments).
  - Charset detection and fallback to UTF-8 with error replacement.
  - Non-multipart messages.

- `_decode_header_str(header_value)` — decodes MIME encoded-word headers (e.g., `=?UTF-8?B?...?=`). Handles both byte and string parts, with encoding fallback.

- `get_sample_emails()` — returns three realistic sample email dictionaries containing technical stories about Python, Rust, Kubernetes, PostgreSQL, and WebAssembly. Used for testing and demonstration.

**Error handling:** If no password is provided, raises `ValueError`. Individual email fetch errors are logged but don't stop the batch fetch.

#### 2. `StoryExtractor` — Technical Story Discovery and Link Filtering

**Purpose:** Extract URLs from email bodies, filter out non-technical and junk links, and score remaining links by technical relevance.

**Filtering strategy:**

The extractor uses a multi-layered approach:

1. **URL Cleaning (`clean_url`):**
   - Strips trailing punctuation (`,`, `.`, `;`, `:`, `)`, `>`, `"`, `'`).
   - Unwraps redirect URLs by checking query parameters (`url`, `u`, `dest`, `target`, `link`) for nested HTTP(S) URLs. Recursively cleans the unwrapped URL.
   - Removes tracking query parameters (`utm_source`, `utm_medium`, `utm_campaign`, `utm_term`, `utm_content`, `ref`, `source`, `fbclid`, `gclid`, `mc_cid`, `mc_eid`).
   - Lowercases the domain and strips URL fragments.

2. **Junk URL Filtering (`should_ignore_url`):**
   - Rejects non-HTTP(S) URLs.
   - Matches against a list of `IGNORE_PATTERNS` (regex): `unsubscribe`, `optout`, `privacy-policy`, `terms-of-service`, `view-in-browser`, `click.track`, `list-manage.com`, social sharing URLs, `mailto:`, `javascript:`, `tel:`, and static asset extensions (`.png`, `.jpg`, `.css`, `.js`, etc.).

3. **Technical Relevance Scoring (`score_technical_relevance`):**
   - **Domain matching:** +5 points if the URL domain matches a known technical domain (e.g., `github.com`, `lwn.net`, `kubernetes.io`). +2 points for TLDs like `.io`, `.dev`, `.ai`, `.tech`, `.org`.
   - **Keyword matching:** +2 points per technical keyword found in the combined text of URL, anchor text, surrounding context, and email subject. Short keywords (≤3 characters) use word-boundary matching; longer keywords use substring matching.
   - Keywords include programming languages, frameworks, tools, and concepts (Python, Rust, Docker, Kubernetes, AI, LLM, PostgreSQL, security, etc.).

**Extraction process (`extract_from_email`):**

1. **HTML parsing:** If the email has an HTML body, uses BeautifulSoup to find all `<a>` tags with `href` attributes. For each link:
   - Cleans and filters the URL.
   - Extracts anchor text and parent container text as context.
   - Scores the link. If score > 0 and URL is not already found, creates a `StoryItem`.

2. **Plain text parsing:** If the email has a text body, uses a regex (`URL_REGEX`) to find all HTTP(S) URLs. For each match:
   - Gathers surrounding lines (previous, current, next) as context.
   - Cleans, filters, and scores the link.
   - Creates a `StoryItem` if score > 0 and URL is not already found.

3. **Deduplication:** Within a single email, URLs are deduplicated using a dictionary keyed by cleaned URL. Across emails, `extract_stories()` deduplicates using a set of seen URLs.

**Key constants:**
- `TECH_KEYWORDS` — a set of ~60 technical keywords.
- `TECH_DOMAINS` — a set of ~30 known technical domains.
- `IGNORE_PATTERNS` — a list of ~18 regex patterns for junk URLs.
- `URL_REGEX` — a compiled regex for matching HTTP(S) URLs in plain text.

#### 3. `LinkSummarizer` — Webpage Fetching and Post Generation

**Purpose:** Fetch linked webpages, extract metadata, and generate Mastodon posts within the 500-character limit.

**Key methods:**

- `fetch_page_content(url)` — performs an HTTP GET request with a browser-like User-Agent header and a configurable timeout (default: 10 seconds). Extracts:
  - **Title:** from Open Graph (`og:title`), Twitter Card (`twitter:title`), `<title>` tag, or `<h1>` tag, in that order. Cleans common suffixes (e.g., ` | Hacker News`).
  - **Description:** from Open Graph (`og:description`), Twitter Card (`twitter:description`), or `<meta name="description">`.
  - **Article snippet:** finds the `<article>` or `<main>` tag (falls back to `<html>`), extracts the first 3 paragraphs longer than 40 characters, and joins them.
  - Returns `(title, description, article_snippet)`. On failure, returns empty strings.

- `generate_hashtags(title, text, url)` — generates up to 3 topic hashtags plus `#Tech`. Uses a keyword-to-hashtag mapping (e.g., `python` → `#Python`, `rust` → `#RustLang`, `kubernetes` → `#Kubernetes`). Falls back to `#Programming` if fewer than 2 tags are found.

- `create_mastodon_post(url, title, description, snippet, max_chars)` — synthesizes a Mastodon post with the structure:
  ```
  Title

  Summary (if space allows)

  URL
  Hashtags
  ```
  The algorithm:
  1. Calculates the fixed-length portion (title + URL + hashtags + newline separators).
  2. If the fixed portion alone exceeds the limit, truncates the title.
  3. Calculates remaining space for the summary.
  4. If at least 20 characters remain and a summary exists, trims the summary to fit at a word boundary.
  5. Assembles the final post.
  6. Performs a final hard-limit check and truncates if necessary.
  7. Guarantees the result is ≤ `max_chars` (default: 500).

- `summarize_story(story)` — orchestrates the full summarization pipeline for a single `StoryItem`:
  1. Calls `fetch_page_content(story.url)`.
  2. Updates `story.web_title` and `story.web_description`.
  3. Uses the web title (falling back to email-extracted title) and web description/snippet to generate the post.
  4. Sets `story.is_fetched = True` and `story.status = "ready"`.

**Error handling:** Network failures are caught and logged. The post generator falls back to email-extracted data (title, context snippet) when webpage fetch fails.

#### 4. `MastodonClient` — Mastodon API Integration

**Purpose:** Publish status updates to Mastodon via the REST API.

**Key methods:**

- `post_status(status_text, instance_url, access_token, visibility, dry_run)` — posts a single status:
  - Validates the character limit (raises `ValueError` if exceeded).
  - In dry-run mode or without a token, logs the post and returns a mock response.
  - In live mode, sends a POST request to `{instance}/api/v1/statuses` with Bearer token authentication.
  - Returns the JSON response from Mastodon.

- `post_selected_stories(stories, dry_run, progress_callback)` — iterates over stories where `selected == True`:
  - Skips stories with empty descriptions (marks as error).
  - Calls `post_status()` for each story.
  - Updates story status (`posted` or `error`) and records the status ID or error message.
  - Returns a summary dictionary with counts and detailed results.

**API endpoint:** `{instance_url}/api/v1/statuses`

**Request format:**
```json
{
  "status": "post text",
  "visibility": "public"
}
```

**Headers:**
```
Authorization: Bearer {access_token}
User-Agent: EmailLinksMastodonBot/1.0
```

#### 5. `EmailLinksApp` — Tkinter GUI

**Purpose:** Provides an interactive desktop interface for the complete workflow.

**Architecture:**

The GUI is built with Tkinter and organized into three regions:

1. **Header & Controls (top):**
   - Title and subtitle labels.
   - Control buttons: Fetch from Gmail, Load Sample Stories, Select All, Deselect All, Fetch All Link Summaries.
   - Stats label showing total stories and selected count.

2. **Scrollable Story List (middle):**
   - Uses a `Canvas` with a `Scrollbar` for vertical scrolling.
   - Mouse wheel support for both scroll direction conventions.
   - Each story is rendered as a `LabelFrame` card containing:
     - A `Checkbutton` for Yes/No toggle (bound to `story.selected`).
     - An "Open Link" button (uses `webbrowser.open()`).
     - A clickable URL label.
     - A source email subject label.
     - A `Text` widget for editing the post description.
     - A character counter label (green/red color-coded).
     - A "Re-fetch" button to regenerate the summary.
   - All widget interactions are bound via closure factories (`make_toggle_handler`, `make_text_change_handler`, `make_refetch_handler`, `make_open_link_handler`) to capture the correct story reference.

3. **Action Bar (bottom):**
   - Status bar label for progress messages.
   - "Post Selected to Mastodon" button.
   - "Cancel" button.

**Key methods:**

- `_build_ui()` — constructs all UI components.
- `_render_story_list()` — clears and re-renders all story cards. Creates widget-to-story bindings.
- `_update_stats()` — updates the story count display.
- `_select_all()` / `_deselect_all()` — bulk toggle operations.
- `_fetch_all_summaries()` — iterates over selected stories, fetches summaries one at a time, and updates the UI after each fetch.
- `load_sample_stories()` — loads sample emails, extracts stories, and generates initial descriptions.
- `_prompt_and_fetch_emails()` — prompts for password if needed, connects to IMAP, extracts stories, and renders them.
- `post_all_selected()` — syncs text boxes to story models, validates character limits, confirms with the user, and publishes.
- `cancel_and_close()` — destroys the window without posting.

**Thread safety:** The GUI runs all network operations on the main thread with `root.update_idletasks()` calls to keep the UI responsive. This is a simple approach suitable for the small number of requests typical in a single session.

#### 6. CLI Workflow (`run_workflow_cli`)

**Purpose:** Provides a headless, scriptable end-to-end workflow.

**Flow:**
1. Creates an `EmailReader` with the provided configuration.
2. Fetches emails (or uses samples if no password is provided).
3. Creates a `StoryExtractor` and extracts stories.
4. Creates a `LinkSummarizer` and generates Mastodon posts for each story.
5. Prints a formatted summary of each story with its generated post.
6. If `auto_post` is True, creates a `MastodonClient` and publishes selected stories.

**Output format:**
```
======================================================================
Reading emails for user@example.com...
Loaded 3 emails.
Discovered 5 technical stories with links.

[1] Python 3.13 Release Notes
    URL: https://docs.python.org/3/whatsnew/3.13.html
    Source: Tech Weekly
    Mastodon Post (412/500 chars):

    Python 3.13 Release Notes: Experimental JIT Compiler

    The JIT compiler boosts CPU-bound workloads...

    https://docs.python.org/3/whatsnew/3.13.html
    #Tech #Python #Programming
----------------------------------------------------------------------
```

#### 7. Entry Point (`main`)

**Purpose:** Parses command-line arguments and dispatches to either GUI or CLI mode.

**Mode selection logic:**
- If `--cli` is passed or the `DISPLAY` environment variable is not set → CLI mode.
- Otherwise → GUI mode.
- If GUI initialization fails (e.g., Tkinter not available), falls back to CLI mode with sample data.

### Constants

| Constant                    | Value                          | Description                              |
|-----------------------------|--------------------------------|------------------------------------------|
| `MASTODON_CHAR_LIMIT`       | `500`                          | Maximum characters for a Mastodon post.  |
| `DEFAULT_EMAIL_USER`        | `"mainmeister@gmail.com"`      | Default email address.                   |
| `DEFAULT_IMAP_HOST`         | `"imap.gmail.com"`             | Default IMAP server.                     |
| `DEFAULT_IMAP_PORT`         | `993`                          | Default IMAP SSL port.                   |
| `DEFAULT_MASTODON_INSTANCE` | `"https://mastodon.social"`    | Default Mastodon instance.               |

### Dependencies

| Package          | Version    | Purpose                                    |
|------------------|------------|--------------------------------------------|
| `requests`       | ≥ 2.31.0   | HTTP client for Mastodon API and web fetch. |
| `beautifulsoup4` | ≥ 4.12.0   | HTML parsing for email bodies and webpages. |

Standard library modules used: `argparse`, `email`, `html`, `imaplib`, `logging`, `os`, `re`, `sys`, `urllib.parse`, `dataclasses`, `webbrowser`, `tkinter`.

### Testing

The test suite (`tests/test_emaillinks.py`) covers all major components:

- **`TestEmailReader`** — header decoding, raw email parsing (plain text and multipart HTML), sample email loading, mocked IMAP connection.
- **`TestStoryExtractor`** — URL cleaning (tracking parameter removal, redirect unwrapping), junk URL filtering, technical relevance scoring, HTML and text extraction, cross-email deduplication.
- **`TestLinkSummarizer`** — hashtag generation, 500-character limit enforcement (including extreme truncation), short description handling, mocked webpage fetching, story summarization with fallback.
- **`TestMastodonClient`** — character limit validation, dry-run mode, mocked live posting, batch posting with mixed selected/deselected/empty stories.
- **`TestStoryItem`** — character counting and limit checking.
- **`TestEmailLinksAppAndCLI`** — CLI workflow end-to-end, GUI state management (select all, deselect all, editing, posting), warning dialogs for no-selection and over-limit cases, network error fallback.

Run tests with:
```bash
python -m pytest tests/
# or
python -m unittest discover tests/
```

### Design Decisions

1. **Single-file architecture:** The entire application is in one file (`emaillinks.py`) for simplicity and ease of distribution. Classes are clearly separated by region comments.

2. **Dataclass models:** `StoryItem`, `EmailConfig`, and `MastodonConfig` use dataclasses for clean, immutable-by-convention configuration and mutable story state.

3. **Scoring-based filtering:** Instead of a simple keyword filter, the relevance scoring system allows nuanced ranking. Links with higher scores are more likely to be genuine technical content.

4. **500-character guarantee:** The post generator uses a multi-pass approach: calculate fixed portions, allocate remaining space to summary, trim at word boundaries, and perform a final hard-limit truncation. This ensures posts never exceed the Mastodon limit.

5. **Dry-run by default:** For safety, Mastodon posting defaults to dry-run mode. Users must explicitly disable it and provide a valid token to publish.

6. **Graceful degradation:** If webpage fetching fails, the post generator falls back to email-extracted data. If GUI is unavailable, the application falls back to CLI mode.

7. **URL normalization:** Redirect unwrapping and tracking parameter removal ensure that the same article reached through different newsletter wrappers is deduplicated correctly.

### Extensibility

- **Adding new technical domains/keywords:** Edit the `TECH_DOMAINS` and `TECH_KEYWORDS` sets in `StoryExtractor`.
- **Supporting other social platforms:** Implement a new client class following the `MastodonClient` interface and integrate it into the posting workflow.
- **Custom email filters:** Modify the `search_criteria` parameter in `EmailConfig` to use IMAP search expressions (e.g., `FROM "newsletter@example.com"`).
- **Background fetching:** The GUI currently fetches on the main thread. For larger email volumes, move network operations to a background thread with `threading` or `asyncio`.
