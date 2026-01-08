"""Web fetch tool for retrieving URL content.

This is a custom tool not provided by OpenHands, used for fetching
external documentation, issue trackers, or reference materials.
"""

import re
import urllib.error
import urllib.request


class WebFetchExecutor:
    """Executor for fetching web content."""

    DEFAULT_TIMEOUT = 30
    MAX_CONTENT_LENGTH = 30000
    USER_AGENT = "Mozilla/5.0 (Research Agent)"

    def __call__(self, url: str) -> str:
        """Fetch content from a URL.

        Args:
            url: URL to fetch (must be http:// or https://)

        Returns:
            Page content with HTML stripped, truncated if too large
        """
        # Validate URL scheme
        if not url.startswith(("http://", "https://")):
            return "Error: Invalid URL scheme. Must be http:// or https://"

        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )

            with urllib.request.urlopen(request, timeout=self.DEFAULT_TIMEOUT) as response:
                content_type = response.headers.get("Content-Type", "")
                raw_content = response.read()

                # Decode content
                text = self._decode_content(raw_content, content_type)

                # Strip HTML for cleaner output
                text = self._strip_html(text)

                # Truncate if too large
                if len(text) > self.MAX_CONTENT_LENGTH:
                    text = text[:self.MAX_CONTENT_LENGTH] + f"\n\n... [truncated, content has {len(text)} chars]"

                return text

        except urllib.error.HTTPError as e:
            return f"Error: HTTP {e.code} - {e.reason}"
        except urllib.error.URLError as e:
            return f"Error: Failed to fetch URL - {e.reason}"
        except TimeoutError:
            return "Error: Request timed out"
        except Exception as e:
            return f"Error fetching URL: {e}"

    def _decode_content(self, raw_content: bytes, content_type: str) -> str:
        """Decode raw content to string."""
        encoding = "utf-8"
        if "charset=" in content_type:
            encoding = content_type.split("charset=")[-1].split(";")[0].strip()

        try:
            return raw_content.decode(encoding, errors="replace")
        except (LookupError, UnicodeDecodeError):
            return raw_content.decode("utf-8", errors="replace")

    def _strip_html(self, text: str) -> str:
        """Remove HTML tags and clean up whitespace."""
        # Remove script and style elements
        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        # Remove HTML tags
        text = re.sub(r"<[^>]+>", " ", text)
        # Clean up whitespace
        text = re.sub(r"\s+", " ", text).strip()
        return text
