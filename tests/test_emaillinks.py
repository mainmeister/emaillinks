"""
Unit and integration tests for the emaillinks application.
"""

import email
import os
import unittest
from unittest.mock import MagicMock, patch

from emaillinks import (
    DEFAULT_EMAIL_USER,
    MASTODON_CHAR_LIMIT,
    EmailConfig,
    EmailLinksApp,
    EmailReader,
    LinkSummarizer,
    MastodonClient,
    MastodonConfig,
    StoryExtractor,
    StoryItem,
    run_workflow_cli,
)


class TestEmailReader(unittest.TestCase):
    """Tests for email parsing and retrieval."""

    def setUp(self):
        self.reader = EmailReader()

    def test_decode_header_str(self):
        self.assertEqual(self.reader._decode_header_str(None), "")
        self.assertEqual(self.reader._decode_header_str("Simple Header"), "Simple Header")
        # Test UTF-8 encoded header
        encoded = "=?UTF-8?B?VGVjaCBXZWVrbHk=?="
        self.assertEqual(self.reader._decode_header_str(encoded), "Tech Weekly")

    def test_parse_raw_email_plain_text(self):
        raw_msg = (
            b"From: editor@tech.dev\r\n"
            b"To: mainmeister@gmail.com\r\n"
            b"Subject: Tech Newsletter #1\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"Check out Python 3.13 JIT at https://python.org\r\n"
        )
        parsed = self.reader.parse_raw_email(raw_msg, msg_id="101")
        self.assertEqual(parsed["id"], "101")
        self.assertEqual(parsed["subject"], "Tech Newsletter #1")
        self.assertEqual(parsed["from"], "editor@tech.dev")
        self.assertIn("https://python.org", parsed["body_text"])

    def test_parse_raw_email_multipart_html(self):
        raw_msg = (
            b"From: digest@devops.org\r\n"
            b"Subject: Rust & Linux\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: multipart/alternative; boundary=\"BOUNDARY\"\r\n"
            b"\r\n"
            b"--BOUNDARY\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"Plain text content: https://kernel.org\r\n"
            b"--BOUNDARY\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"\r\n"
            b"<html><body><p><a href=\"https://lwn.net/rust\">Rust in Linux</a></p></body></html>\r\n"
            b"--BOUNDARY--\r\n"
        )
        parsed = self.reader.parse_raw_email(raw_msg, msg_id="102")
        self.assertEqual(parsed["subject"], "Rust & Linux")
        self.assertIn("https://kernel.org", parsed["body_text"])
        self.assertIn("https://lwn.net/rust", parsed["body_html"])

    def test_get_sample_emails(self):
        samples = self.reader.get_sample_emails()
        self.assertIsInstance(samples, list)
        self.assertGreaterEqual(len(samples), 3)
        self.assertTrue(all("subject" in s and "body_text" in s for s in samples))

    @patch("imaplib.IMAP4_SSL")
    def test_connect_and_fetch_mocked(self, mock_imap_cls):
        mock_imap = MagicMock()
        mock_imap_cls.return_value = mock_imap

        mock_imap.login.return_value = ("OK", [b"Logged in"])
        mock_imap.select.return_value = ("OK", [b"10"])
        mock_imap.search.return_value = ("OK", [b"1 2"])

        raw_msg = (
            b"From: news@ai.org\r\n"
            b"Subject: AI Advancements\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"New LLM paper: https://arxiv.org/abs/2401.00000\r\n"
        )
        mock_imap.fetch.return_value = ("OK", [(b"1 (RFC822 {100})", raw_msg)])

        reader = EmailReader(EmailConfig(password="fake-pass"))
        emails = reader.connect_and_fetch()
        self.assertGreaterEqual(len(emails), 1)
        self.assertEqual(emails[0]["subject"], "AI Advancements")


class TestStoryExtractor(unittest.TestCase):
    """Tests for technical story discovery and link filtering."""

    def setUp(self):
        self.extractor = StoryExtractor()

    def test_clean_url(self):
        raw = "https://github.com/vllm-project/vllm?utm_source=newsletter&utm_medium=email"
        cleaned = self.extractor.clean_url(raw)
        self.assertEqual(cleaned, "https://github.com/vllm-project/vllm")

    def test_clean_url_redirect_unwrapping(self):
        wrapped = "https://click.track.com/redirect?url=https%3A%2F%2Flwn.net%2FArticles%2Frust"
        cleaned = self.extractor.clean_url(wrapped)
        self.assertEqual(cleaned, "https://lwn.net/Articles/rust")

    def test_should_ignore_url(self):
        self.assertTrue(self.extractor.should_ignore_url("mailto:editor@tech.com"))
        self.assertTrue(self.extractor.should_ignore_url("javascript:void(0)"))
        self.assertTrue(self.extractor.should_ignore_url("https://example.com/unsubscribe?id=123"))
        self.assertTrue(self.extractor.should_ignore_url("https://example.com/privacy-policy"))
        self.assertTrue(self.extractor.should_ignore_url("https://example.com/banner.png"))
        self.assertFalse(self.extractor.should_ignore_url("https://github.com/fastapi/fastapi"))
        self.assertFalse(self.extractor.should_ignore_url("https://docs.python.org/3/whatsnew/3.13.html"))

    def test_technical_relevance_scoring(self):
        score_tech = self.extractor.score_technical_relevance(
            "https://github.com/rust-lang/rust",
            "Rust compiler release notes",
            "Modern compiler infrastructure in Rust",
            "Weekly Programming Update"
        )
        self.assertGreater(score_tech, 5)

        score_non_tech = self.extractor.score_technical_relevance(
            "https://shoes.com/deals",
            "Buy shoes today",
            "Discount on running shoes",
            "Shoe Store Sale"
        )
        self.assertEqual(score_non_tech, 0)

    def test_extract_stories_from_html_and_text(self):
        email_data = {
            "subject": "Tech Digest",
            "from": "editor@dev.io",
            "body_html": (
                "<html><body>"
                "<p><a href='https://kubernetes.io/docs/concepts/'>Kubernetes Multi-Cluster Architecture</a></p>"
                "<p><a href='https://dev.io/unsubscribe'>Unsubscribe</a></p>"
                "</body></html>"
            ),
            "body_text": "Also check PostgreSQL scaling at https://www.postgresql.org/docs/high-availability",
        }
        stories = self.extractor.extract_from_email(email_data)
        urls = [s.url for s in stories]
        self.assertIn("https://kubernetes.io/docs/concepts/", urls)
        self.assertIn("https://www.postgresql.org/docs/high-availability", urls)
        self.assertNotIn("https://dev.io/unsubscribe", urls)

    def test_extract_stories_deduplication(self):
        emails = [
            {
                "subject": "Email 1",
                "body_html": "<a href='https://github.com/python/cpython'>Python Repo</a>",
            },
            {
                "subject": "Email 2",
                "body_html": "<a href='https://github.com/python/cpython'>Python Repo Again</a>",
            },
        ]
        stories = self.extractor.extract_stories(emails)
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0].url, "https://github.com/python/cpython")


class TestLinkSummarizer(unittest.TestCase):
    """Tests for webpage fetching and 500-char Mastodon post creation."""

    def setUp(self):
        self.summarizer = LinkSummarizer()

    def test_generate_hashtags(self):
        tags = self.summarizer.generate_hashtags("Python 3.13 JIT and AI performance", "LLM inference speedups", "https://python.org")
        self.assertIn("#Python", tags)
        self.assertIn("#AI", tags)
        self.assertIn("#Tech", tags)

    def test_create_mastodon_post_strict_500_char_limit(self):
        url = "https://docs.python.org/3/whatsnew/3.13.html"
        title = "Python 3.13 Release Notes: Experimental JIT Compiler & Free-Threaded CPython"
        description = "A" * 1000  # Extremely long description

        post = self.summarizer.create_mastodon_post(url, title, description)
        self.assertLessEqual(len(post), MASTODON_CHAR_LIMIT)
        self.assertIn(url, post)
        self.assertIn("Python 3.13", post)
        self.assertTrue(post.endswith("... #Tech #Python #Programming") or "#Tech" in post)

    def test_create_mastodon_post_short_description(self):
        url = "https://lwn.net/Articles/rust-linux/"
        title = "Rust in the Linux Kernel"
        desc = "New memory-safe abstractions for kernel device drivers."

        post = self.summarizer.create_mastodon_post(url, title, desc)
        self.assertLessEqual(len(post), MASTODON_CHAR_LIMIT)
        self.assertIn(url, post)
        self.assertIn("Rust in the Linux Kernel", post)
        self.assertIn("memory-safe abstractions", post)

    @patch("requests.Session.get")
    def test_fetch_page_content_mocked(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.text = """
        <html>
            <head>
                <meta property="og:title" content="vLLM: Fast and Easy LLM Serving" />
                <meta property="og:description" content="State-of-the-art serving engine with PagedAttention algorithm." />
            </head>
            <body>
                <article>
                    <p>vLLM delivers 24x higher throughput compared to HuggingFace Transformers.</p>
                </article>
            </body>
        </html>
        """
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        title, desc, snippet = self.summarizer.fetch_page_content("https://github.com/vllm-project/vllm")
        self.assertEqual(title, "vLLM: Fast and Easy LLM Serving")
        self.assertEqual(desc, "State-of-the-art serving engine with PagedAttention algorithm.")
        self.assertIn("24x higher throughput", snippet)

    @patch.object(LinkSummarizer, "fetch_page_content")
    def test_summarize_story(self, mock_fetch):
        mock_fetch.return_value = (
            "Modern Kubernetes",
            "Multi-cluster service mesh architecture.",
            "Deep dive into Envoy and Istio."
        )
        story = StoryItem(url="https://kubernetes.io/docs")
        self.summarizer.summarize_story(story)

        self.assertTrue(story.is_fetched)
        self.assertEqual(story.status, "ready")
        self.assertLessEqual(len(story.post_description), MASTODON_CHAR_LIMIT)
        self.assertIn("https://kubernetes.io/docs", story.post_description)
        self.assertIn("Modern Kubernetes", story.post_description)


class TestMastodonClient(unittest.TestCase):
    """Tests for publishing statuses to Mastodon."""

    def setUp(self):
        self.client = MastodonClient(MastodonConfig(access_token="fake-token"))

    def test_post_status_exceeding_character_limit_raises(self):
        too_long = "X" * 501
        with self.assertRaises(ValueError):
            self.client.post_status(too_long)

    def test_post_status_dry_run(self):
        status_text = "Python 3.13 JIT is amazing! https://python.org #Python #Tech"
        res = self.client.post_status(status_text, dry_run=True)
        self.assertTrue(res.get("mock"))
        self.assertIn("mock-status-", res["id"])
        self.assertEqual(res["content"], status_text)

    @patch("requests.post")
    def test_post_status_live_mocked(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "id": "123456789",
            "url": "https://mastodon.social/@user/123456789",
            "content": "Test status",
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        res = self.client.post_status("Test status", dry_run=False)
        self.assertEqual(res["id"], "123456789")
        mock_post.assert_called_once()

    def test_post_selected_stories(self):
        story1 = StoryItem(
            url="https://python.org",
            post_description="Python 3.13 JIT https://python.org #Python",
            selected=True
        )
        story2 = StoryItem(
            url="https://rust-lang.org",
            post_description="Rust 1.80 https://rust-lang.org #Rust",
            selected=False  # Skipped
        )
        story3 = StoryItem(
            url="https://kubernetes.io",
            post_description="",  # Empty description -> error
            selected=True
        )

        stories = [story1, story2, story3]
        results = self.client.post_selected_stories(stories, dry_run=True)

        self.assertEqual(results["total_selected"], 2)
        self.assertEqual(results["posted_count"], 1)
        self.assertEqual(results["error_count"], 1)
        self.assertEqual(story1.status, "posted")
        self.assertEqual(story2.status, "pending")
        self.assertEqual(story3.status, "error")


class TestStoryItem(unittest.TestCase):
    """Tests for the StoryItem data model."""

    def test_story_item_character_count_and_limit(self):
        story = StoryItem(url="https://test.com", post_description="Hello World")
        self.assertEqual(story.character_count(), 11)
        self.assertTrue(story.is_within_limit())

        story.post_description = "A" * 501
        self.assertEqual(story.character_count(), 501)
        self.assertFalse(story.is_within_limit())


class TestEmailLinksAppAndCLI(unittest.TestCase):
    """Tests for the interactive application UI and CLI flow."""

    @patch.object(LinkSummarizer, "fetch_page_content")
    def test_cli_workflow_run(self, mock_fetch):
        mock_fetch.return_value = (
            "Python 3.13 JIT",
            "Experimental JIT compiler details and improvements.",
            "Detailed article body and analysis."
        )
        stories = run_workflow_cli(
            email_user="mainmeister@gmail.com",
            use_samples=True,
            auto_post=True,
            dry_run=True
        )
        self.assertGreaterEqual(len(stories), 3)
        self.assertTrue(all(s.is_fetched for s in stories))
        self.assertTrue(all(len(s.post_description) <= MASTODON_CHAR_LIMIT for s in stories))

    def test_app_gui_structure_and_controls(self):
        """Test GUI state management, toggling, editing, and actions."""
        mock_root = MagicMock()
        mock_root.winfo_children.return_value = []

        stories = [
            StoryItem(
                url="https://python.org",
                title="Python 3.13 JIT",
                source_email_subject="Tech Newsletter",
                post_description="Python JIT compiler https://python.org #Python",
                selected=True
            ),
            StoryItem(
                url="https://rust-lang.org",
                title="Rust in Linux",
                source_email_subject="Kernel Digest",
                post_description="Rust Linux Kernel https://rust-lang.org #Rust",
                selected=False
            )
        ]

        with patch("tkinter.ttk.LabelFrame"), \
             patch("tkinter.ttk.Frame"), \
             patch("tkinter.ttk.Label"), \
             patch("tkinter.ttk.Button"), \
             patch("tkinter.ttk.Checkbutton"), \
             patch("tkinter.ttk.Scrollbar"), \
             patch("tkinter.ttk.Separator"), \
             patch("tkinter.Canvas"), \
             patch("tkinter.Text") as mock_text_cls, \
             patch("tkinter.BooleanVar") as mock_bool_var_cls:

            mock_var1 = MagicMock()
            mock_var1.get.return_value = True
            mock_var2 = MagicMock()
            mock_var2.get.return_value = False
            mock_bool_var_cls.side_effect = [mock_var1, mock_var2, MagicMock(), MagicMock()]

            mock_tb1 = MagicMock()
            mock_tb1.get.return_value = "Custom edited post https://python.org #Python"
            mock_tb2 = MagicMock()
            mock_tb2.get.return_value = "Rust status https://rust-lang.org #Rust"
            mock_text_cls.side_effect = [mock_tb1, mock_tb2, MagicMock(), MagicMock()]

            app = EmailLinksApp(mock_root, stories=stories, dry_run=True)

            self.assertEqual(len(app.stories), 2)
            self.assertEqual(len(app.story_widgets), 2)

            # Test select all
            app._select_all()
            self.assertTrue(all(s.selected for s in app.stories))
            mock_var1.set.assert_called_with(True)
            mock_var2.set.assert_called_with(True)

            # Test deselect all
            app._deselect_all()
            self.assertTrue(all(not s.selected for s in app.stories))
            mock_var1.set.assert_called_with(False)
            mock_var2.set.assert_called_with(False)

            # Test editing and posting selected
            app.stories[0].selected = True
            with patch("tkinter.messagebox.askyesno", return_value=True), \
                 patch("tkinter.messagebox.showinfo"):
                app.post_all_selected()
                self.assertEqual(app.stories[0].status, "posted")

    def test_post_when_none_selected_shows_warning(self):
        """Test that clicking post when no stories are selected warns the user."""
        mock_root = MagicMock()
        mock_root.winfo_children.return_value = []
        stories = [
            StoryItem(url="https://test.com", post_description="Test", selected=False)
        ]
        with patch("tkinter.ttk.LabelFrame"), \
             patch("tkinter.ttk.Frame"), \
             patch("tkinter.ttk.Label"), \
             patch("tkinter.ttk.Button"), \
             patch("tkinter.ttk.Checkbutton"), \
             patch("tkinter.ttk.Scrollbar"), \
             patch("tkinter.ttk.Separator"), \
             patch("tkinter.Canvas"), \
             patch("tkinter.Text"), \
             patch("tkinter.BooleanVar"):
            app = EmailLinksApp(mock_root, stories=stories, dry_run=True)
            with patch("tkinter.messagebox.showwarning") as mock_warn:
                app.post_all_selected()
                mock_warn.assert_called_once()

    def test_post_when_over_limit_shows_error(self):
        """Test that exceeding 500 characters prevents posting and displays error."""
        mock_root = MagicMock()
        mock_root.winfo_children.return_value = []
        stories = [
            StoryItem(url="https://test.com", post_description="X" * 501, selected=True)
        ]
        with patch("tkinter.ttk.LabelFrame"), \
             patch("tkinter.ttk.Frame"), \
             patch("tkinter.ttk.Label"), \
             patch("tkinter.ttk.Button"), \
             patch("tkinter.ttk.Checkbutton"), \
             patch("tkinter.ttk.Scrollbar"), \
             patch("tkinter.ttk.Separator"), \
             patch("tkinter.Canvas"), \
             patch("tkinter.Text") as mock_text, \
             patch("tkinter.BooleanVar"):
            mock_tb = MagicMock()
            mock_tb.get.return_value = "X" * 501
            mock_text.return_value = mock_tb
            app = EmailLinksApp(mock_root, stories=stories, dry_run=True)
            with patch("tkinter.messagebox.showerror") as mock_err:
                app.post_all_selected()
                mock_err.assert_called_once()

    def test_summarizer_network_error_fallback(self):
        """Test that link summarizer gracefully falls back when page fetch fails."""
        summarizer = LinkSummarizer()
        with patch("requests.Session.get", side_effect=Exception("Connection refused")):
            story = StoryItem(
                url="https://offline-tech-site.org/post",
                title="Offline Tech Article",
                context_snippet="Discussion about compiler optimizations."
            )
            summarizer.summarize_story(story)
            self.assertTrue(story.is_fetched)
            self.assertIn("Offline Tech Article", story.post_description)
            self.assertIn("https://offline-tech-site.org/post", story.post_description)
            self.assertLessEqual(len(story.post_description), MASTODON_CHAR_LIMIT)


if __name__ == "__main__":
    unittest.main()
