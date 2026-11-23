#!/usr/bin/env python3
"""
Stash Downloader - Python Backend Script

This script handles server-side downloads using yt-dlp.
It's invoked via Stash's runPluginTask GraphQL mutation.

Requirements:
- Python 3.7+
- yt-dlp (pip install "yt-dlp[default,curl-cffi]")
- requests (pip install requests)

Usage:
  Called automatically by the Stash Downloader plugin via runPluginTask.
  Not intended to be run directly.

Result Communication:
  Since runPluginTask doesn't return script output, results are stored in
  a temp file that can be read via the 'read_result' task.
"""

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional, Tuple

# Configure logging
# Stash shows stderr in its logs. Use INFO by default so messages appear correctly.
# Set STASH_DOWNLOADER_DEBUG=1 for verbose DEBUG logging.
DEBUG_MODE = os.environ.get("STASH_DOWNLOADER_DEBUG", "").lower() in ("1", "true", "yes")
logging.basicConfig(
    level=logging.DEBUG if DEBUG_MODE else logging.INFO,
    format="[stash-downloader] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger(__name__)

# Directory for storing task results
# Use plugin's results directory so it's accessible via HTTP at /plugin/stash-downloader/results/
# Fall back to temp directory if plugin directory detection fails
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(SCRIPT_DIR)  # Parent of scripts/
RESULT_DIR = os.path.join(PLUGIN_DIR, "results")  # {pluginDir}/results/

# Ensure results directory exists at startup
os.makedirs(RESULT_DIR, exist_ok=True)
log.debug(f"Results directory: {RESULT_DIR}")


def ensure_result_dir():
    """Ensure the result directory exists."""
    os.makedirs(RESULT_DIR, exist_ok=True)


def get_result_path(result_id: str) -> str:
    """Get the path for a result file."""
    # Sanitize result_id to prevent path traversal
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", result_id)
    return os.path.join(RESULT_DIR, f"{safe_id}.json")


def save_result(result_id: str, data: dict) -> bool:
    """Save a result to a file for later retrieval."""
    try:
        ensure_result_dir()
        result_path = get_result_path(result_id)
        # Write to a temp file in the same dir, then atomically replace, so concurrent
        # readers (progress polling) never see a half-written JSON file
        tmp_path = f"{result_path}.{os.getpid()}.tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(data, f)
            os.replace(tmp_path, result_path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        log.debug(f"Saved result to: {result_path}")
        return True
    except Exception as e:
        log.error(f"Failed to save result: {e}")
        return False


def load_result(result_id: str, silent: bool = False) -> Optional[dict]:
    """Load a result from file.

    Args:
        result_id: ID of the result to load
        silent: If True, don't log warnings for missing files (used for progress polling)
    """
    try:
        result_path = get_result_path(result_id)
        if os.path.exists(result_path):
            with open(result_path, "r") as f:
                return json.load(f)
        else:
            if not silent:
                log.debug(f"Result file not found: {result_path}")
            return None
    except Exception as e:
        log.error(f"Failed to load result: {e}")
        return None


def delete_result(result_id: str) -> bool:
    """Delete a result file."""
    try:
        result_path = get_result_path(result_id)
        if os.path.exists(result_path):
            os.remove(result_path)
            log.debug(f"Deleted result: {result_path}")
        return True
    except Exception as e:
        log.error(f"Failed to delete result: {e}")
        return False


# Read input from stdin (Stash passes JSON)
def read_input() -> dict:
    """Read JSON input from stdin."""
    try:
        input_data = sys.stdin.read()
        if input_data:
            return json.loads(input_data)
    except json.JSONDecodeError as e:
        log.error(f"Failed to parse input JSON: {e}")
    return {}


def write_output(result: dict) -> None:
    """Write JSON output to stdout.

    IMPORTANT: Stash expects output in PluginOutput format:
    {
        "error": "optional error string",
        "output": { ... actual result data ... }
    }

    If the result has a 'result_error' field, we put it in 'error'.
    Otherwise, we wrap the entire result in 'output'.

    Based on Stash source code (pkg/plugin/common/msg.go):
    - PluginOutput struct has Error (*string) and Output (interface{})
    - If JSON doesn't match this structure, Stash may return null
    - Use ensure_ascii=True to avoid Unicode encoding issues
    - Compact JSON (no pretty printing) is recommended
    """
    try:
        # Wrap result in Stash's PluginOutput format
        plugin_output = {}

        # If there's a result_error, put it in the 'error' field
        if "result_error" in result:
            plugin_output["error"] = result["result_error"]
            # Still include the rest of the result in 'output' for context
            result_without_error = {k: v for k, v in result.items() if k != "result_error"}
            if result_without_error:
                plugin_output["output"] = result_without_error
        else:
            # No error, wrap entire result in 'output' field
            plugin_output["output"] = result

        # Use ensure_ascii=True to avoid potential Unicode encoding issues
        # Compact JSON (no separators) for maximum compatibility
        output = json.dumps(plugin_output, ensure_ascii=True, separators=(",", ":"))

        # Validate the JSON can be parsed (catches serialization issues)
        try:
            json.loads(output)
        except json.JSONDecodeError as e:
            log.error(f"Generated invalid JSON: {e}")
            log.error(f"Output that failed: {output[:500]}")
            raise

        log.debug(f"Writing PluginOutput to stdout: {output[:200]}...")  # Log first 200 chars
        log.debug(f"Output length: {len(output)} characters")

        # Write directly to stdout and flush to ensure it's sent immediately
        # Don't use print() as it adds a newline which might confuse Stash
        sys.stdout.write(output)
        sys.stdout.flush()

        # Verify output was written (debugging)
        log.debug("Output written and flushed to stdout")
    except Exception as e:
        log.error(f"Failed to serialize output: {e}", exc_info=True)
        # Fallback: write error in PluginOutput format
        try:
            error_output = json.dumps(
                {"error": f"Failed to serialize output: {str(e)}", "output": {"success": False}},
                ensure_ascii=True,
                separators=(",", ":"),
            )
            sys.stdout.write(error_output)
            sys.stdout.flush()
        except Exception as fallback_error:
            # Last resort: write minimal error in PluginOutput format
            log.error(f"Even fallback JSON serialization failed: {fallback_error}")
            sys.stdout.write('{"error":"Serialization failed","output":{"success":false}}')
            sys.stdout.flush()


def sanitize_filename(filename: str) -> str:
    """Sanitize filename for filesystem."""
    # Remove or replace invalid characters
    sanitized = re.sub(r'[<>:"/\\|?*]', "", filename)
    # Limit length
    return sanitized[:200] if len(sanitized) > 200 else sanitized


# Cached yt-dlp command (resolved once, reused for all calls)
_ytdlp_cmd: Optional[list] = None
_gallery_dl_cmd: Optional[list] = None


def get_ytdlp_cmd(custom_path: Optional[str] = None) -> list:
    """
    Resolve the yt-dlp command to use.

    Resolution order:
    1. Custom path from plugin settings (ytdlpPath)
    2. Python module: sys.executable -m yt_dlp (same Python env, no PATH needed)
    3. CLI binary: yt-dlp on PATH (legacy fallback)

    Returns list of command parts, e.g. ["yt-dlp"] or ["/usr/bin/python3", "-m", "yt_dlp"]
    """
    global _ytdlp_cmd

    # If a custom path is provided, always use it (don't cache - may change between calls)
    if custom_path:
        log.info(f"Using custom yt-dlp path from settings: {custom_path}")
        return [custom_path]

    # Return cached result if available
    if _ytdlp_cmd is not None:
        return _ytdlp_cmd

    # Strategy 1: Python module (most reliable - uses same Python that runs this script)
    # find_spec avoids spawning `python -m yt_dlp --version` (~0.25s) on every invocation.
    # Each Stash call is a new process, so the in-memory cache below never helps across calls.
    try:
        import importlib.util
        if importlib.util.find_spec("yt_dlp") is not None:
            log.debug(f"Found yt-dlp as Python module via {sys.executable}")
            _ytdlp_cmd = [sys.executable, "-m", "yt_dlp"]
            return _ytdlp_cmd
    except Exception as e:
        log.debug(f"Python module check failed: {e}")

    # Strategy 2: CLI binary on PATH (fallback)
    try:
        result = subprocess.run(
            ["yt-dlp", "--version"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            version = result.stdout.strip()
            log.info(f"Found yt-dlp CLI on PATH (v{version})")
            _ytdlp_cmd = ["yt-dlp"]
            return _ytdlp_cmd
    except FileNotFoundError:
        log.debug("yt-dlp CLI not found on PATH")
    except subprocess.TimeoutExpired:
        log.debug("yt-dlp CLI version check timed out")

    # Not found anywhere
    _ytdlp_cmd = None
    return []


def get_gallery_dl_cmd(custom_path: Optional[str] = None) -> list:
    """Resolve gallery-dl from a configured executable, Python module, or PATH."""
    global _gallery_dl_cmd

    if custom_path:
        log.info(f"Using custom gallery-dl path from settings: {custom_path}")
        return [custom_path]

    if _gallery_dl_cmd is not None:
        return _gallery_dl_cmd

    try:
        import importlib.util
        if importlib.util.find_spec("gallery_dl") is not None:
            _gallery_dl_cmd = [sys.executable, "-m", "gallery_dl"]
            return _gallery_dl_cmd
    except (ImportError, ValueError):
        pass

    gallery_dl_path = shutil.which("gallery-dl")
    if gallery_dl_path:
        _gallery_dl_cmd = [gallery_dl_path]
        return _gallery_dl_cmd

    _gallery_dl_cmd = []
    return []


def install_ytdlp() -> bool:
    """Attempt to install yt-dlp using pip, with fallbacks for restricted environments."""
    global _ytdlp_cmd
    log.info("Attempting to install yt-dlp...")

    # Try standard pip install first
    install_commands = [
        [sys.executable, "-m", "pip", "install", "yt-dlp[default,curl-cffi]"],
        # Fallback for externally-managed environments (Docker Alpine, etc.)
        [sys.executable, "-m", "pip", "install", "--user", "yt-dlp[default,curl-cffi]"],
        [sys.executable, "-m", "pip", "install", "--break-system-packages", "yt-dlp[default,curl-cffi]"],
    ]

    for cmd in install_commands:
        try:
            log.debug(f"Trying: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                log.info("yt-dlp installed successfully")
                # Clear cached command so it gets re-resolved
                _ytdlp_cmd = None
                return True
            else:
                log.debug(f"Install attempt failed: {result.stderr[:200]}")
        except Exception as e:
            log.debug(f"Install attempt error: {e}")

    log.error("All yt-dlp installation attempts failed")
    return False


def check_ytdlp(custom_path: Optional[str] = None) -> bool:
    """
    Check if yt-dlp is available, auto-install if missing.

    Args:
        custom_path: Optional custom path to yt-dlp binary (from plugin settings)
    """
    cmd = get_ytdlp_cmd(custom_path)
    if cmd:
        return True

    # Not found - attempt auto-install
    log.warning("yt-dlp not found via Python module or PATH, attempting auto-install...")
    if install_ytdlp():
        # Verify installation worked
        cmd = get_ytdlp_cmd(custom_path)
        if cmd:
            log.info("yt-dlp now available after auto-install")
            return True

    log.error(
        'yt-dlp is not installed. Install it with: pip install "yt-dlp[default,curl-cffi]"\n'
        "Or set a custom path in plugin settings (ytdlpPath)."
    )
    return False


def detect_content_type(metadata: dict) -> str:
    """
    Detect content type from yt-dlp metadata.

    Args:
        metadata: yt-dlp metadata dictionary

    Returns:
        Content type: 'video', 'image', or 'gallery'
    """
    # Check _type field (yt-dlp sets this for playlists, etc.)
    if metadata.get("_type") == "playlist":
        return "gallery"

    # Check file extension
    ext = (metadata.get("ext") or "").lower()
    image_extensions = ["jpg", "jpeg", "png", "gif", "webp", "bmp"]
    if ext in image_extensions:
        log.debug(f"Detected image from extension: {ext}")
        return "image"

    # Check video codec - if 'none' or missing, likely not a video
    vcodec = metadata.get("vcodec")
    acodec = metadata.get("acodec")

    if vcodec == "none" or not vcodec:
        # No video codec - check audio codec
        if acodec == "none" or not acodec:
            # No video, no audio = likely an image
            log.debug("Detected image: no video or audio codecs")
            return "image"
        else:
            # Audio-only content (treat as video for now)
            log.debug("Detected audio-only content (treating as video)")
            return "video"

    # Check formats array for video characteristics
    formats = metadata.get("formats", [])
    has_video_format = False
    for fmt in formats:
        if fmt.get("height") and fmt.get("height") > 0:
            has_video_format = True
            break
        if fmt.get("vcodec") and fmt.get("vcodec") != "none":
            has_video_format = True
            break

    if not has_video_format and formats:
        log.debug("Detected image: no video formats found")
        return "image"

    # Has duration? Definitely a video
    if metadata.get("duration") and metadata.get("duration") > 0:
        log.debug(f"Detected video: has duration of {metadata.get('duration')}s")
        return "video"

    # Check if width/height suggest video dimensions
    width = metadata.get("width", 0)
    height = metadata.get("height", 0)
    if height > 0:
        # Video-like dimensions (most images don't report this in yt-dlp)
        log.debug(f"Detected video: has dimensions {width}x{height}")
        return "video"

    # Default to video (most common case for yt-dlp usage)
    log.debug("Defaulting to video (no clear indicators)")
    return "video"


def extract_metadata(
    url: str,
    proxy: Optional[str] = None,
    ytdlp_path: Optional[str] = None,
    gallery_dl_path: Optional[str] = None,
) -> dict:
    """
    Extract metadata using yt-dlp without downloading.

    Args:
        url: URL to extract metadata from
        proxy: Optional HTTP/HTTPS/SOCKS proxy (e.g., http://proxy.example.com:8080)
        ytdlp_path: Optional custom path to yt-dlp binary
        gallery_dl_path: Optional custom path to gallery-dl binary

    Returns:
        dict with metadata on success, or dict with 'extraction_error' key on failure
    """
    is_tiktok_photo = is_tiktok_photo_url(url)
    is_tiktok = is_tiktok_url(url)
    tiktok_photo_error = None

    def try_tiktok_fallback(previous_error: str) -> dict:
        fallback_metadata, fallback_error = extract_gallery_dl_tiktok_metadata(
            url, proxy=proxy, gallery_dl_path=gallery_dl_path
        )
        if fallback_metadata:
            return fallback_metadata
        return {
            "extraction_error": f"{previous_error}; {fallback_error}",
            "used_proxy": bool(proxy),
        }

    if is_tiktok_photo:
        photo_metadata = extract_tiktok_photo_post(url, proxy=proxy)
        if "extraction_error" not in photo_metadata:
            return photo_metadata
        tiktok_photo_error = photo_metadata["extraction_error"]
        log.warning("Direct TikTok photo-page parsing failed; trying yt-dlp")

    # rule34video: yt-dlp's extractor is broken (see extract_rule34video), so use the
    # built-in page parser first and only fall back to yt-dlp if it returns nothing
    if is_rule34video_url(url):
        log.info("rule34video URL, using built-in page parser first")
        parsed_info = extract_rule34video(url, proxy=proxy)
        if parsed_info:
            return parsed_info
        log.warning("Built-in rule34video parser returned nothing, trying yt-dlp")

    ytdlp_cmd = get_ytdlp_cmd(ytdlp_path)
    if not ytdlp_cmd:
        if is_tiktok:
            previous_error = tiktok_photo_error or "yt-dlp is not installed or accessible"
            return try_tiktok_fallback(previous_error)
        return {"extraction_error": "yt-dlp is not installed or accessible", "used_proxy": bool(proxy)}

    cmd = ytdlp_cmd + [
        "--dump-single-json" if is_tiktok_photo else "--dump-json",
        "--no-download",
    ]
    if not is_tiktok_photo:
        cmd.append("--no-playlist")

    # Add proxy if provided
    if proxy:
        cmd.extend(["--proxy", proxy])
        log.debug(f"✓ Proxy configured for metadata extraction: {proxy}")
    else:
        log.debug("ℹ No proxy configured for metadata extraction - using direct connection")

    cmd.append(url)

    log.info(f"Extracting metadata: {url[:60]}...")
    log.debug(f"yt-dlp command: {' '.join(cmd)}")  # Log full command for debugging

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if result.returncode == 0 and result.stdout:
            log.info("✓ Metadata extracted")
            metadata = json.loads(result.stdout)
            if is_tiktok_photo:
                gallery_metadata = _normalize_tiktok_gallery_metadata(metadata, url)
                if gallery_metadata:
                    return gallery_metadata
                return try_tiktok_fallback(
                    f"{tiktok_photo_error}; yt-dlp returned metadata but no carousel image URLs"
                )
            if is_tiktok and not metadata.get("url") and not metadata.get("formats"):
                return try_tiktok_fallback("yt-dlp returned no usable TikTok media URL")
            return metadata
        else:
            # Enhanced error logging with proxy context
            error_msg = result.stderr or result.stdout or "Unknown error"
            # Clean up yt-dlp error message for display
            clean_error = error_msg.strip().split('\n')[-1] if error_msg else "Unknown error"
            if is_rule34video_url(url):
                log.warning(f"yt-dlp failed on rule34video, retrying built-in page parser: {clean_error[:150]}")
                fallback = extract_rule34video(url, proxy=proxy)
                if fallback:
                    return fallback
            if is_tiktok:
                previous_error = f"{tiktok_photo_error}; yt-dlp fallback failed: {clean_error}" if tiktok_photo_error else f"yt-dlp failed: {clean_error}"
                return try_tiktok_fallback(previous_error)
            if proxy:
                log.error(f"yt-dlp extraction FAILED (proxy {proxy}): {error_msg[:200]}")
            else:
                log.error(f"yt-dlp extraction FAILED (no proxy): {error_msg[:200]}")
            return {"extraction_error": clean_error, "used_proxy": bool(proxy)}
    except subprocess.TimeoutExpired:
        timeout_msg = f"Timed out after 60s"
        if proxy:
            timeout_msg += f" (proxy: {proxy})"
        log.error(f"yt-dlp TIMED OUT: {timeout_msg}")
        if is_tiktok:
            previous_error = f"{tiktok_photo_error}; yt-dlp fallback {timeout_msg}" if tiktok_photo_error else f"yt-dlp {timeout_msg}"
            return try_tiktok_fallback(previous_error)
        return {"extraction_error": timeout_msg, "used_proxy": bool(proxy)}
    except json.JSONDecodeError as e:
        log.error(f"Failed to parse yt-dlp output: {e}", exc_info=True)
        if is_tiktok:
            previous_error = f"{tiktok_photo_error}; yt-dlp returned invalid JSON: {e}" if tiktok_photo_error else f"yt-dlp returned invalid JSON: {e}"
            return try_tiktok_fallback(previous_error)
        return {"extraction_error": f"Failed to parse yt-dlp output: {e}", "used_proxy": bool(proxy)}
    except Exception as e:
        error_msg = f"Extraction error: {e}"
        if proxy:
            error_msg += f" (proxy: {proxy})"
        log.error(error_msg, exc_info=True)
        if is_tiktok:
            previous_error = f"{tiktok_photo_error}; yt-dlp fallback failed: {error_msg}" if tiktok_photo_error else f"yt-dlp failed: {error_msg}"
            return try_tiktok_fallback(previous_error)
        return {"extraction_error": str(e), "used_proxy": bool(proxy)}


def is_direct_file_url(url: str) -> bool:
    """
    Check if URL is a direct file URL (video/image) that doesn't need yt-dlp.

    These are URLs that point directly to media files and should be downloaded
    with a simple HTTP request instead of yt-dlp.
    """
    # Common video/image extensions
    direct_extensions = [
        ".mp4",
        ".webm",
        ".mkv",
        ".avi",
        ".mov",
        ".flv",
        ".wmv",
        ".m4v",
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".bmp",
    ]

    try:
        # Parse URL and check path
        from urllib.parse import urlparse, unquote

        parsed = urlparse(url)
        path = unquote(parsed.path).lower()

        # Check if path ends with a known extension
        for ext in direct_extensions:
            if path.endswith(ext) or f"{ext}?" in path or f"{ext}/" in path:
                return True

        # Check query parameters for download hints
        query = parsed.query.lower()
        if "download=" in query or "download_filename=" in query:
            return True

    except Exception:
        pass

    return False


# =============================================================================
# Site fallbacks (used when a yt-dlp extractor is broken)
# =============================================================================

BROWSER_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

RULE34VIDEO_URL_RE = re.compile(r"^https?://(?:www\.)?rule34video\.(?:com|party)/videos?/(\d+)", re.IGNORECASE)
TIKTOK_PHOTO_URL_RE = re.compile(r"^https?://(?:www\.)?tiktok\.com/@[\w.-]+/photo/(\d+)(?:[/?#]|$)", re.IGNORECASE)
TIKTOK_URL_RE = re.compile(r"^https?://(?:[\w-]+\.)*tiktokv?\.com(?:[:/]|$)", re.IGNORECASE)

QUALITY_MAX_HEIGHT = {"1080p": 1080, "720p": 720, "480p": 480}


def is_tiktok_url(url: Optional[str]) -> bool:
    return bool(url and TIKTOK_URL_RE.match(url))


def is_tiktok_photo_url(url: Optional[str]) -> bool:
    return bool(url and TIKTOK_PHOTO_URL_RE.match(url))


def _solve_tiktok_web_challenge(html: str) -> Optional[dict]:
    class ChallengeElements(HTMLParser):
        def __init__(self):
            super().__init__()
            self.values = {}

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            element_id = attributes.get("id")
            if element_id in {"cs", "wci", "rci", "rs"}:
                self.values[element_id] = attributes.get("class", "")

    parser = ChallengeElements()
    parser.feed(html)
    elements = parser.values
    if not elements.get("cs") or not elements.get("wci"):
        return None

    try:
        challenge = json.loads(base64.b64decode(elements["cs"] + "===").decode("utf-8"))
        expected_digest = base64.b64decode(challenge["v"]["c"] + "===")
        base_digest = hashlib.sha256(base64.b64decode(challenge["v"]["a"] + "==="))
        solution = None
        for value in range(1_000_001):
            candidate = base_digest.copy()
            candidate.update(str(value).encode("ascii"))
            if candidate.digest() == expected_digest:
                solution = value
                break
        if solution is None:
            return None

        challenge["d"] = base64.b64encode(str(solution).encode("ascii")).decode("ascii")
        cookie_value = base64.b64encode(
            json.dumps(challenge, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        cookies = {elements["wci"]: cookie_value}
        if elements.get("rci") and elements.get("rs"):
            cookies[elements["rci"]] = elements["rs"]
        return cookies
    except (binascii.Error, UnicodeDecodeError, KeyError, TypeError, ValueError):
        return None


def _find_tiktok_image_post(value: Any) -> Optional[Tuple[dict, list]]:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key in ("imagePost", "image_post_info", "imagePostInfo", "image_post"):
                image_post = current.get(key)
                if isinstance(image_post, dict):
                    images = image_post.get("images") or image_post.get("image_list") or image_post.get("imageList")
                    if isinstance(images, list) and images:
                        return current, images
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            stack.extend(reversed(current))
    return None


def _tiktok_image_url(image: Any) -> Optional[str]:
    if not isinstance(image, dict):
        return None

    direct_url = image.get("url") or image.get("image_url") or image.get("thumbnail")
    if isinstance(direct_url, str) and direct_url:
        return f"https:{direct_url}" if direct_url.startswith("//") else direct_url

    for key in (
        "imageURL", "image_url", "originImage", "origin_image",
        "displayImage", "display_image", "ownerWatermarkImage", "owner_watermark_image",
    ):
        value = image.get(key)
        if isinstance(value, dict):
            value = value.get("urlList") or value.get("url_list") or value.get("url")
        elif isinstance(value, list):
            value = value[0] if value else None
        if isinstance(value, list):
            value = value[0] if value else None
        if isinstance(value, str) and value:
            return f"https:{value}" if value.startswith("//") else value

    for key in ("urlList", "url_list"):
        value = image.get(key)
        if isinstance(value, list) and value and isinstance(value[0], str):
            url = value[0]
            return f"https:{url}" if url.startswith("//") else url

    return None


def _normalize_tiktok_gallery_metadata(metadata: Any, page_url: str) -> Optional[dict]:
    if not isinstance(metadata, dict):
        return None

    image_entries = metadata.get("gallery_images") or metadata.get("images") or metadata.get("entries")
    if not isinstance(image_entries, list) or not image_entries:
        return None

    url_match = TIKTOK_PHOTO_URL_RE.match(page_url)
    post_id = str(metadata.get("id") or (url_match.group(1) if url_match else "post"))
    gallery_images = []
    seen_urls = set()
    for index, image_entry in enumerate(image_entries, start=1):
        image_url = image_entry if isinstance(image_entry, str) else _tiktok_image_url(image_entry)
        if not image_url or not image_url.startswith(("http://", "https://")) or image_url in seen_urls:
            continue
        seen_urls.add(image_url)

        extension = os.path.splitext(image_url.split("?", 1)[0].split("#", 1)[0])[1].lower()
        if extension not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
            entry_extension = image_entry.get("ext") if isinstance(image_entry, dict) else None
            extension = f".{entry_extension.lower()}" if isinstance(entry_extension, str) else ".jpg"
        if extension not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
            extension = ".jpg"

        image_info = image_entry if isinstance(image_entry, dict) else {}
        gallery_images.append({
            "url": image_url,
            "thumbnail_url": image_info.get("thumbnail") or image_url,
            "filename": image_info.get("filename") or f"tiktok-{post_id}-{index:02d}{extension}",
            "width": image_info.get("width"),
            "height": image_info.get("height"),
        })

    if not gallery_images:
        return None

    normalized = dict(metadata)
    normalized["gallery_images"] = gallery_images
    normalized["content_type"] = "gallery"
    normalized["webpage_url"] = normalized.get("webpage_url") or page_url
    normalized["thumbnail"] = normalized.get("thumbnail") or gallery_images[0]["url"]
    return normalized


def _normalize_gallery_dl_tiktok_metadata(messages: Any, page_url: str) -> Optional[dict]:
    if not isinstance(messages, list):
        return None

    post_metadata = {}
    image_entries = []
    video_entry = None
    for message in messages:
        if not isinstance(message, list) or not message:
            continue
        if message[0] == 2 and len(message) > 1 and isinstance(message[1], dict):
            post_metadata = message[1]
        elif message[0] == 3 and len(message) > 2 and isinstance(message[2], dict):
            media_metadata = message[2]
            media_type = media_metadata.get("type")
            if media_type == "image" and isinstance(message[1], str):
                image_entries.append((message[1], media_metadata))
            elif media_type == "video" and isinstance(message[1], str):
                video_entry = (message[1], media_metadata)

    if not post_metadata and not image_entries and not video_entry:
        return None

    author = post_metadata.get("author")
    if not isinstance(author, dict):
        author = {}
    uploader = post_metadata.get("user") or author.get("uniqueId") or author.get("nickname")
    creator = author.get("nickname") or uploader
    post_id = str(post_metadata.get("id") or "post")
    title = post_metadata.get("desc") or post_metadata.get("title")

    if image_entries:
        gallery_images = []
        seen_urls = set()
        for index, (image_url, media_metadata) in enumerate(image_entries, start=1):
            if not image_url.startswith(("http://", "https://")) or image_url in seen_urls:
                continue
            seen_urls.add(image_url)
            extension = (media_metadata.get("extension") or "jpg").lower().lstrip(".")
            if extension not in {"jpg", "jpeg", "png", "gif", "webp", "bmp"}:
                extension = "jpg"
            gallery_images.append({
                "url": image_url,
                "thumbnail_url": image_url,
                "filename": f"tiktok-{post_id}-{index:02d}.{extension}",
                "width": media_metadata.get("width"),
                "height": media_metadata.get("height"),
            })

        if gallery_images:
            return {
                "id": post_metadata.get("id"),
                "title": title or f"TikTok photo #{post_id}",
                "description": post_metadata.get("desc"),
                "uploader": uploader,
                "creator": creator,
                "upload_date": post_metadata.get("date"),
                "thumbnail": gallery_images[0]["url"],
                "content_type": "gallery",
                "gallery_images": gallery_images,
                "webpage_url": page_url,
            }

    if video_entry:
        video_url, media_metadata = video_entry
        return {
            "id": post_metadata.get("id"),
            "title": title or media_metadata.get("title"),
            "description": post_metadata.get("desc"),
            "uploader": uploader,
            "creator": creator,
            "upload_date": post_metadata.get("date"),
            "duration": media_metadata.get("duration"),
            "thumbnail": media_metadata.get("image"),
            "content_type": "video",
            "url": video_url,
            "webpage_url": page_url,
            "width": media_metadata.get("width"),
            "height": media_metadata.get("height"),
        }

    return None


def extract_gallery_dl_tiktok_metadata(
    url: str, proxy: Optional[str] = None, gallery_dl_path: Optional[str] = None
) -> Tuple[Optional[dict], Optional[str]]:
    """Extract TikTok post metadata and media URLs with gallery-dl."""
    cmd = get_gallery_dl_cmd(gallery_dl_path).copy()
    if not cmd:
        return None, "gallery-dl is not installed; run `python -m pip install gallery-dl` or set galleryDlPath"

    cmd += ["--dump-json", "--no-input", "-o", "output.jsonl=false"]
    if proxy:
        cmd.extend(["--proxy", proxy])
    cmd.append(url)

    log.info("Trying gallery-dl as a TikTok fallback")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return None, "gallery-dl metadata extraction timed out after 90s"
    except OSError as error:
        return None, f"could not start gallery-dl: {error}"

    if result.returncode != 0 or not result.stdout:
        error = (result.stderr or result.stdout or "no output").strip().splitlines()
        return None, f"gallery-dl failed: {error[-1][:300] if error else 'no output'}"

    try:
        messages = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        return None, f"gallery-dl returned invalid JSON: {error}"

    metadata = _normalize_gallery_dl_tiktok_metadata(messages, url)
    if metadata:
        log.info("gallery-dl extracted TikTok metadata")
        return metadata, None
    return None, "gallery-dl returned no usable TikTok media URLs"


def _parse_tiktok_photo_html(html: str, page_url: str) -> Optional[dict]:
    url_match = TIKTOK_PHOTO_URL_RE.match(page_url)
    if not url_match:
        return None

    script_ids = ("__UNIVERSAL_DATA_FOR_REHYDRATION__", "SIGI_STATE")
    for script_id in script_ids:
        pattern = re.compile(
            rf'<script\b[^>]*\bid=["\']{re.escape(script_id)}["\'][^>]*>(.*?)</script\s*>',
            re.IGNORECASE | re.DOTALL,
        )
        for match in pattern.finditer(html):
            try:
                page_data = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue

            found = _find_tiktok_image_post(page_data)
            if not found:
                continue

            item, image_entries = found
            images = []
            seen_urls = set()
            post_id = str(item.get("id") or url_match.group(1))
            for index, image_entry in enumerate(image_entries, start=1):
                image_url = _tiktok_image_url(image_entry)
                if not image_url or not image_url.startswith(("http://", "https://")) or image_url in seen_urls:
                    continue
                seen_urls.add(image_url)

                image_path = image_url.split("?", 1)[0].split("#", 1)[0]
                extension = os.path.splitext(image_path)[1].lower()
                if extension not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
                    extension = ".jpg"

                image_info = image_entry if isinstance(image_entry, dict) else {}
                images.append({
                    "url": image_url,
                    "thumbnail_url": image_url,
                    "filename": f"tiktok-{post_id}-{index:02d}{extension}",
                    "width": image_info.get("imageWidth") or image_info.get("image_width") or image_info.get("width"),
                    "height": image_info.get("imageHeight") or image_info.get("image_height") or image_info.get("height"),
                })

            if not images:
                continue

            author = item.get("author") or item.get("authorInfo") or {}
            if not isinstance(author, dict):
                author = {}
            uploader = author.get("uniqueId") or author.get("unique_id") or author.get("username")
            creator = author.get("nickname") or author.get("displayName") or author.get("name")
            create_time = item.get("createTime") or item.get("create_time")
            upload_date = None
            if create_time:
                try:
                    upload_date = time.strftime("%Y%m%d", time.gmtime(int(create_time)))
                except (TypeError, ValueError, OverflowError):
                    pass

            title = item.get("desc") or item.get("description") or item.get("title") or f"TikTok photo {post_id}"
            return {
                "id": post_id,
                "title": title,
                "description": item.get("desc") or item.get("description") or title,
                "uploader": uploader,
                "creator": creator,
                "upload_date": upload_date,
                "thumbnail": images[0]["url"],
                "gallery_images": images,
                "content_type": "gallery",
                "webpage_url": page_url,
                "extractor": "TikTok",
            }

    return None


def extract_tiktok_photo_post(url: str, proxy: Optional[str] = None) -> dict:
    import requests

    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        session = requests.Session()
        headers = {
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.tiktok.com/",
        }
        response = session.get(
            url,
            headers=headers,
            proxies=proxies,
            timeout=30,
        )
        response.raise_for_status()
        html = response.text
        status_code = response.status_code
        content_type = response.headers.get("Content-Type", "unknown")
        response.close()

        metadata = _parse_tiktok_photo_html(html, url)
        if metadata:
            log.info(f"Extracted {len(metadata['gallery_images'])} TikTok photo(s)")
            return metadata

        challenge_cookies = _solve_tiktok_web_challenge(html)
        challenge_detected = bool(re.search(r'\bid=["\'](?:cs|wci)["\']', html, re.IGNORECASE))
        if challenge_cookies:
            log.info("TikTok returned a web verification challenge; retrying with its challenge cookies")
            for name, value in challenge_cookies.items():
                session.cookies.set(name, value, domain=".tiktok.com", path="/")
            response = session.get(url, headers=headers, proxies=proxies, timeout=30)
            response.raise_for_status()
            metadata = _parse_tiktok_photo_html(response.text, url)
            status_code = response.status_code
            content_type = response.headers.get("Content-Type", "unknown")
            response.close()
            if metadata:
                log.info(f"Extracted {len(metadata['gallery_images'])} TikTok photo(s) after challenge retry")
                return metadata

        if challenge_detected and not challenge_cookies:
            detail = "TikTok served a verification challenge that could not be solved"
        else:
            detail = "TikTok returned no carousel data"
        return {
            "extraction_error": (
                f"{detail} (HTTP {status_code}, content type {content_type})"
            )
        }
    except requests.RequestException as e:
        log.error(f"TikTok photo extraction failed: {e}")
        return {"extraction_error": f"TikTok photo extraction failed: {e}"}


def is_rule34video_url(url: Optional[str]) -> bool:
    return bool(url and RULE34VIDEO_URL_RE.match(url))


def _parse_iso_duration(value: Optional[str]) -> Optional[int]:
    """Parse ISO 8601 durations like PT0H2M01S into seconds."""
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not m or not any(m.groups()):
        return None
    hours, minutes, seconds = (int(g or 0) for g in m.groups())
    return hours * 3600 + minutes * 60 + seconds


def extract_rule34video(url: str, proxy: Optional[str] = None) -> Optional[dict]:
    """
    Parse a rule34video.com page into a yt-dlp style info dict.

    Fallback for yt-dlp's Rule34Video extractor, which (as of yt-dlp 2026.10.26)
    fails with "No video formats found" since the site added a file-size <span>
    inside its download links. The CDN returns 403 for non-browser User-Agents,
    so a browser UA is required for both the page and the video URLs.

    Returns None if the page can't be fetched or has no video URLs.
    """
    import html
    import requests

    m = RULE34VIDEO_URL_RE.match(url)
    if not m:
        return None
    video_id = m.group(1)

    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        response = requests.get(url, headers={"User-Agent": BROWSER_USER_AGENT}, proxies=proxies, timeout=30)
        response.raise_for_status()
    except Exception as e:
        log.error(f"rule34video fallback: failed to fetch page: {e}")
        return None
    page = response.text

    def clean(fragment: str) -> str:
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()

    # Formats keyed by height. Primary source: the Download menu links,
    # e.g. <a href="...?v-acctoken=...&download=true&...">MP4 1080p<span>67 MB</span></a>
    formats_by_height: dict = {}
    for href, label in re.findall(r'<a[^>]+href="([^"]+download=true[^"]*)"[^>]*>(.*?)</a>', page, re.DOTALL):
        label_text = clean(label)
        height = re.search(r"\b(\d{3,4})p?\b", label_text)
        if height:
            ext = label_text.split(" ", 1)[0].lower() or "mp4"
            formats_by_height[int(height.group(1))] = {"url": html.unescape(href), "ext": ext}

    # Backup source: player flashvars (video_url / video_alt_urlN + matching _text)
    for key, video_url in re.findall(r"\b(video_url|video_alt_url\d*):\s*'([^']+)'", page):
        label = re.search(rf"\b{key}_text:\s*'(\d{{3,4}})p?", page)
        # KVS sites sometimes obfuscate URLs as "function/0/..." - those need decoding, skip
        if label and not video_url.startswith("function/"):
            formats_by_height.setdefault(int(label.group(1)), {"url": video_url, "ext": "mp4"})

    if not formats_by_height:
        log.error("rule34video fallback: no video URLs found on page")
        return None

    formats = [
        {"format_id": f"{h}p", "url": f["url"], "ext": f["ext"], "height": h}
        for h, f in sorted(formats_by_height.items())
    ]

    # Title, description, thumbnail, date, duration from JSON-LD
    ld: dict = {}
    ld_match = re.search(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.DOTALL)
    if ld_match:
        try:
            ld = json.loads(ld_match.group(1))
        except json.JSONDecodeError:
            log.debug("rule34video fallback: could not parse JSON-LD")

    # Per-video chips: data-item-type is tag (characters + tags), category, or model (artist)
    chips: dict = {"tag": [], "category": [], "model": []}
    for item_type, label in re.findall(
        rf'data-video-id="{video_id}" data-item-type="(tag|category|model)"[^>]*>.*?<a\b[^>]*>(.*?)</a>', page, re.DOTALL
    ):
        name = clean(label)
        if name and name not in chips[item_type]:
            chips[item_type].append(name)

    uploader = uploader_url = None
    uploader_match = re.search(r'Uploaded by</div>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page, re.DOTALL)
    if uploader_match:
        uploader_url, uploader = uploader_match.group(1), clean(uploader_match.group(2)) or None

    title = ld.get("name")
    if not title:
        title_match = re.search(r"video_title:\s*'([^']*)'", page) or re.search(r"<title>(.*?)</title>", page, re.DOTALL)
        title = clean(title_match.group(1)) if title_match else f"rule34video {video_id}"

    thumbnail = ld.get("thumbnailUrl")
    if not thumbnail:
        preview_match = re.search(r"preview_url:\s*'([^']+)'", page)
        thumbnail = preview_match.group(1) if preview_match else None

    best = formats[-1]
    log.info(f"✓ rule34video fallback: {len(formats)} formats (best {best['height']}p), {len(chips['tag'])} tags")
    return {
        "id": video_id,
        "extractor": "rule34video-fallback",
        "title": html.unescape(title),
        "description": html.unescape(ld.get("description") or "") or None,
        "duration": _parse_iso_duration(ld.get("duration")),
        "thumbnail": thumbnail,
        "upload_date": (ld.get("uploadDate") or "").replace("-", "")[:8] or None,
        "uploader": uploader,
        "uploader_url": uploader_url,
        "tags": chips["tag"],
        "categories": chips["category"],
        "creators": chips["model"],
        "formats": formats,
        "url": best["url"],
        "height": best["height"],
        "webpage_url": url,
        "original_url": url,
    }


def select_format_url(info: Optional[dict], quality: str = "best") -> Optional[str]:
    """Pick the highest format at or below the requested quality (or the lowest if none fit)."""
    formats = sorted((info or {}).get("formats") or [], key=lambda f: f.get("height") or 0)
    if not formats:
        return None
    max_height = QUALITY_MAX_HEIGHT.get(quality)
    fitting = [f for f in formats if not max_height or (f.get("height") or 0) <= max_height]
    return (fitting[-1] if fitting else formats[0]).get("url")


# Max Range-resume attempts after a mid-stream failure in download_direct_file
MAX_RESUME_RETRIES = 3


def download_direct_file(
    url: str, output_dir: str, filename: Optional[str] = None, proxy: Optional[str] = None,
    progress_id: Optional[str] = None
) -> Optional[str]:
    """
    Download a direct file URL using requests (no yt-dlp).

    Args:
        url: Direct file URL to download
        output_dir: Directory to save the file
        filename: Optional custom filename (will extract from URL if not provided)
        proxy: Optional HTTP/HTTPS/SOCKS proxy
        progress_id: Optional ID for progress file updates (for frontend polling)

    Returns:
        Path to downloaded file, or None if failed
    """
    import requests
    from urllib.parse import urlparse, unquote, parse_qs

    log.debug(f"Direct file download: {url}")

    try:
        # Ensure output directory exists
        os.makedirs(output_dir, exist_ok=True)

        # Parse URL (needed for filename extraction and Referer header)
        parsed = urlparse(url)

        # Determine filename
        if not filename:
            # Try to get filename from query params (e.g., download_filename=...)
            query_params = parse_qs(parsed.query)
            if "download_filename" in query_params:
                filename = query_params["download_filename"][0]
            else:
                # Extract from path
                path_filename = unquote(parsed.path.split("/")[-1])
                # Remove query string artifacts
                if "?" in path_filename:
                    path_filename = path_filename.split("?")[0]
                filename = path_filename if path_filename else "downloaded_file"

        # Sanitize filename
        filename = sanitize_filename(filename)

        # Ensure filename has an extension
        if "." not in filename:
            # Keep the media type when a caller supplies a title without an extension.
            path_extension = os.path.splitext(parsed.path)[1].lower()
            image_extensions = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
            filename += path_extension if path_extension in image_extensions else ".mp4"

        output_path = os.path.join(output_dir, filename)

        # Configure proxy
        proxies = None
        if proxy:
            log.debug(f"Using proxy for direct download: {proxy}")
            proxies = {
                "http": proxy,
                "https": proxy,
            }

        # Download with streaming
        log.debug(f"Downloading to: {output_path}")

        # Build Referer header from the URL's origin to prevent hotlink protection
        referer = f"{parsed.scheme}://{parsed.netloc}/"

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Referer": referer,
            "Origin": f"{parsed.scheme}://{parsed.netloc}",
        }

        log.debug(f"Using Referer: {referer}")

        # Mid-stream failures that are worth resuming with a Range request
        import urllib3
        resumable_errors = (
            requests.exceptions.ReadTimeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            urllib3.exceptions.ProtocolError,
            urllib3.exceptions.ReadTimeoutError,
        )

        total_size = 0
        downloaded = 0
        last_progress_update = 0.0
        progress_update_interval = 0.5  # Update every 0.5 seconds
        resume_attempts = 0
        wrote_file = False

        def write_progress(status: str, force: bool = False) -> None:
            """Write progress (throttled). downloaded_bytes is always written, even without Content-Length."""
            nonlocal last_progress_update
            if not progress_id:
                return
            now = time.time()
            if not force and (now - last_progress_update) < progress_update_interval:
                return
            save_result(f"progress-{progress_id}", {
                "status": status,
                "percentage": (downloaded / total_size) * 100 if total_size > 0 else 0,
                "downloaded_bytes": downloaded,
                "total_bytes": total_size,
                "last_updated": now,
            })
            last_progress_update = now

        def discard_partial() -> None:
            if wrote_file and os.path.exists(output_path):
                try:
                    os.remove(output_path)
                    log.debug(f"Deleted partial file: {output_path}")
                except OSError as rm_err:
                    log.warning(f"Could not delete partial file {output_path}: {rm_err}")

        try:
            while True:
                req_headers = dict(headers)
                resuming = downloaded > 0
                if resuming:
                    req_headers["Range"] = f"bytes={downloaded}-"
                    # Byte offsets must refer to the raw stream, not a compressed one
                    req_headers["Accept-Encoding"] = "identity"

                response = None
                try:
                    response = requests.get(
                        url,
                        headers=req_headers,
                        proxies=proxies,
                        stream=True,
                        timeout=(15, 60),  # (connect, per-read): a stalled stream fails fast and is resumed
                        allow_redirects=True,
                    )

                    if resuming and response.status_code in (403, 404, 410):
                        # e.g. expired access token - retrying the same URL won't help
                        log.error(f"Resume rejected with HTTP {response.status_code}, giving up")
                        discard_partial()
                        return None
                    if resuming and response.status_code == 416 and total_size > 0 and downloaded >= total_size:
                        break  # Already have everything
                    response.raise_for_status()

                    if resuming and response.status_code == 206:
                        file_mode = "ab"
                    else:
                        if resuming:
                            log.warning("Server ignored Range request (HTTP 200), restarting from 0")
                        file_mode = "wb"
                        downloaded = 0
                        total_size = int(response.headers.get("content-length", 0))
                        if not resuming:
                            log.debug(f"Creating progress file for direct download: progress-{progress_id}")
                            write_progress("starting", force=True)

                    wrote_file = True
                    with open(output_path, file_mode) as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                write_progress("downloading")

                    # A clean close short of Content-Length is a truncated transfer
                    if total_size > 0 and downloaded < total_size:
                        raise requests.exceptions.ChunkedEncodingError(
                            f"Connection closed early ({downloaded}/{total_size} bytes)"
                        )
                    break
                except resumable_errors as e:
                    if downloaded == 0:
                        raise  # Nothing to resume from - treat as a normal failure
                    resume_attempts += 1
                    if resume_attempts > MAX_RESUME_RETRIES:
                        log.error(f"Direct download failed after {MAX_RESUME_RETRIES} resume attempts: {e}")
                        discard_partial()
                        return None
                    # Trust the file on disk for the resume offset
                    downloaded = os.path.getsize(output_path) if os.path.exists(output_path) else 0
                    log.warning(
                        f"Download interrupted at {downloaded} bytes ({e}); "
                        f"resuming (attempt {resume_attempts}/{MAX_RESUME_RETRIES})"
                    )
                    time.sleep(min(2 * resume_attempts, 5))
                finally:
                    if response is not None:
                        response.close()

            write_progress("downloading", force=True)
            log.debug(f"Direct download complete: {output_path} ({downloaded} bytes)")
            return output_path
        except BaseException:
            # Never leave a partial file behind on failure
            discard_partial()
            raise

    except requests.exceptions.RequestException as e:
        log.error(f"Direct download failed (network error): {e}")
        return None
    except Exception as e:
        log.error(f"Direct download failed: {e}", exc_info=True)
        return None


def parse_ytdlp_progress(line: str) -> Optional[dict]:
    """
    Parse yt-dlp progress output line.

    Example lines:
    [download]  50.0% of 100.00MiB at 5.00MiB/s ETA 00:10
    [download]  50.0% of ~100.00MiB at 5.00MiB/s ETA 00:10 (frag 5/10)
    [download] Destination: /path/to/file.mp4

    Returns dict with progress info or None if not a progress line.
    """
    if not line.startswith("[download]"):
        return None

    # Match percentage progress line
    # Pattern: [download]  XX.X% of [~]XXX.XXMIB at XXX.XXMIB/s ETA XX:XX
    import re

    progress_match = re.search(
        r"\[download\]\s+(\d+\.?\d*)%\s+of\s+~?(\d+\.?\d*)(Ki?B|Mi?B|Gi?B)",
        line,
    )
    if progress_match:
        percentage = float(progress_match.group(1))
        size_value = float(progress_match.group(2))
        size_unit = progress_match.group(3)

        # Convert to bytes
        multipliers = {"KiB": 1024, "KB": 1000, "MiB": 1024 * 1024, "MB": 1000 * 1000, "GiB": 1024 * 1024 * 1024, "GB": 1000 * 1000 * 1000}
        total_bytes = int(size_value * multipliers.get(size_unit, 1))
        downloaded_bytes = int(total_bytes * percentage / 100)

        result = {
            "percentage": percentage,
            "downloaded_bytes": downloaded_bytes,
            "total_bytes": total_bytes,
        }

        # Try to parse speed
        speed_match = re.search(r"at\s+(\d+\.?\d*)(Ki?B|Mi?B|Gi?B)/s", line)
        if speed_match:
            speed_value = float(speed_match.group(1))
            speed_unit = speed_match.group(2)
            result["speed"] = int(speed_value * multipliers.get(speed_unit, 1))

        # Try to parse ETA
        eta_match = re.search(r"ETA\s+(\d+):(\d+)", line)
        if eta_match:
            minutes = int(eta_match.group(1))
            seconds = int(eta_match.group(2))
            result["eta"] = minutes * 60 + seconds

        return result

    # Percentage without a known size (e.g. "12.3% of N/A" for HLS/DASH): still report
    # the percentage so frontend stall detection sees the download advancing
    pct_match = re.search(r"\[download\]\s+(\d+\.?\d*)%", line)
    if pct_match:
        return {"percentage": float(pct_match.group(1)), "downloaded_bytes": 0, "total_bytes": 0}

    return None


def download_video(
    url: str,
    output_dir: str,
    filename_template: str = "%(title)s.%(ext)s",
    quality: str = "best",
    progress_callback: Optional[callable] = None,
    proxy: Optional[str] = None,
    progress_id: Optional[str] = None,
    ytdlp_path: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Download video using yt-dlp with real-time progress reporting.

    Args:
        url: Video URL to download
        output_dir: Directory to save the video
        filename_template: yt-dlp output template
        quality: Quality preference (best, 1080p, 720p, 480p)
        progress_callback: Optional callback for progress updates
        proxy: Optional HTTP/HTTPS/SOCKS proxy (e.g., http://proxy.example.com:8080)
        progress_id: Optional ID for progress file updates (for frontend polling)
        ytdlp_path: Optional custom path to yt-dlp binary

    Returns:
        Downloaded file path and no error, or None and the yt-dlp failure reason
    """
    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)

    # Build format string based on quality preference
    format_str = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    if quality == "1080p":
        format_str = "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best"
    elif quality == "720p":
        format_str = "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best"
    elif quality == "480p":
        format_str = "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best"

    output_template = os.path.join(output_dir, filename_template)

    cmd = get_ytdlp_cmd(ytdlp_path) + [
        "-f",
        format_str,
        "-o",
        output_template,
        "--no-playlist",
        "--newline",  # Output progress on new lines for parsing
        "--progress",  # Show progress bar
        # Use progress template to ensure consistent output format
        "--progress-template", "download:[download] %(progress._percent_str)s of %(progress._total_bytes_str)s at %(progress._speed_str)s ETA %(progress._eta_str)s",
        "--print",
        "after_move:filepath",  # Print final path
    ]

    # Set TERM environment variable to trick yt-dlp into outputting progress
    # (yt-dlp suppresses progress when not in a TTY; this makes it think it is)
    env = os.environ.copy()
    env["TERM"] = "xterm"

    # Add proxy if provided (yt-dlp supports http://, https://, socks4://, socks5://)
    if proxy:
        cmd.extend(["--proxy", proxy])
        log.debug(f"✓ Proxy configured: {proxy}")
    else:
        log.debug("ℹ No proxy configured - using direct connection")

    cmd.append(url)

    log.info(f"Downloading: {url[:60]}...")
    log.debug(f"Output directory: {output_dir}")
    log.debug(f"Quality: {quality}")
    log.debug(f"yt-dlp command: {' '.join(cmd)}")

    # Create initial progress file so frontend knows download has started
    if progress_id:
        progress_file_id = f"progress-{progress_id}"
        log.info(f"Creating progress file: {progress_file_id}")
        initial_progress = {
            "status": "starting",
            "percentage": 0,
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "last_updated": time.time(),
        }
        save_success = save_result(progress_file_id, initial_progress)
        log.info(f"Progress file created: {save_success}")
        # Verify file was created
        result_path = get_result_path(progress_file_id)
        log.info(f"Progress file path: {result_path}, exists: {os.path.exists(result_path)}")
    else:
        log.warning("No progress_id provided - progress tracking disabled")

    try:
        # Use Popen for streaming output to get real-time progress
        # Merge stderr into stdout so we get all output in one stream
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # Merge stderr into stdout
            env=env,  # Pass environment with TERM=xterm to enable progress output
        )

        stdout_lines = []  # To capture the final file path
        error_lines = []
        last_progress_update = 0
        progress_update_interval = 0.5  # Update progress file every 0.5 seconds

        # Read combined stdout/stderr for progress updates
        # yt-dlp uses \r (carriage return) to update progress in place
        # We need to read in chunks and split on both \r and \n
        lines_read = 0
        progress_updates = 0
        log.info("Reading yt-dlp output for progress updates...")

        output_buffer = ""
        if process.stdout:
            while True:
                # Read in small chunks to handle progress updates in real-time
                chunk = process.stdout.read(256)
                if not chunk:
                    break

                # Decode and add to buffer
                output_buffer += chunk.decode('utf-8', errors='replace')

                # Split on both \r and \n to get progress lines
                # yt-dlp uses \r to overwrite progress lines
                while '\r' in output_buffer or '\n' in output_buffer:
                    # Find the first line terminator
                    r_pos = output_buffer.find('\r')
                    n_pos = output_buffer.find('\n')

                    if r_pos == -1:
                        split_pos = n_pos
                    elif n_pos == -1:
                        split_pos = r_pos
                    else:
                        split_pos = min(r_pos, n_pos)

                    line = output_buffer[:split_pos].strip()
                    output_buffer = output_buffer[split_pos + 1:]

                    if not line:
                        continue
                    lines_read += 1

                    # Check if this is the final file path (from --print after_move:filepath)
                    if line.startswith('/') or (len(line) > 2 and line[1] == ':'):  # Unix or Windows path
                        stdout_lines.append(line)
                        log.debug(f"Captured output path: {line}")
                        continue

                    # Parse progress from yt-dlp output
                    progress = parse_ytdlp_progress(line)
                    if progress:
                        progress_updates += 1
                        current_time = time.time()
                        # Update progress file periodically (not on every line)
                        if progress_id and (current_time - last_progress_update) >= progress_update_interval:
                            progress["status"] = "downloading"
                            progress["last_updated"] = current_time
                            save_result(f"progress-{progress_id}", progress)
                            last_progress_update = current_time
                            log.debug(f"Progress: {progress['percentage']:.1f}% ({progress.get('downloaded_bytes', 0)}/{progress.get('total_bytes', 0)} bytes)")

                        if progress_callback:
                            progress_callback(progress)
                    else:
                        # Log non-progress lines at debug level
                        if "[download]" in line or "[info]" in line:
                            log.debug(f"yt-dlp: {line}")
                            # Update heartbeat even without progress data (for HLS/DASH streams)
                            current_time = time.time()
                            if progress_id and (current_time - last_progress_update) >= progress_update_interval:
                                save_result(f"progress-{progress_id}", {
                                    "status": "downloading",
                                    "last_updated": current_time,
                                    "message": line[:100],  # Include last status message
                                })
                                last_progress_update = current_time
                        elif "ERROR" in line or "error" in line.lower():
                            log.error(f"yt-dlp: {line}")
                            error_lines.append(line[:400])

        log.info(f"Finished reading output: {lines_read} lines, {progress_updates} progress updates")

        # Wait for process to complete
        process.wait()

        if process.returncode == 0:
            # Last line of stdout should be the file path
            file_path = stdout_lines[-1] if stdout_lines else None

            if file_path and os.path.exists(file_path):
                log.info(f"✓ Download complete: {os.path.basename(file_path)}")
                # Update progress to 100%
                if progress_id:
                    save_result(f"progress-{progress_id}", {
                        "status": "complete",
                        "percentage": 100,
                        "file_path": file_path,
                        "last_updated": time.time(),
                    })
                return file_path, None
            else:
                # Try to find the file in output directory
                log.warning("Could not determine output path, searching directory...")
                files = list(Path(output_dir).glob("*"))
                if files:
                    newest = max(files, key=os.path.getmtime)
                    log.debug(f"Found downloaded file: {newest}")
                    if progress_id:
                        save_result(f"progress-{progress_id}", {
                            "status": "complete",
                            "percentage": 100,
                            "file_path": str(newest),
                            "last_updated": time.time(),
                        })
                    return str(newest), None

        # Failed - update progress with error
        error_msg = "\n".join(error_lines[-3:]) or f"yt-dlp exited with status {process.returncode}"
        if progress_id:
            save_result(f"progress-{progress_id}", {
                "status": "error",
                "error": error_msg,
                "last_updated": time.time(),
            })

        if proxy:
            log.error(f"yt-dlp download failed (using proxy {proxy})")
        else:
            log.error(f"yt-dlp download failed (no proxy)")
        return None, error_msg

    except subprocess.TimeoutExpired:
        timeout_msg = f"Download timed out after 1 hour"
        if proxy:
            timeout_msg += f" (using proxy {proxy})"
        log.error(timeout_msg)
        if progress_id:
            save_result(f"progress-{progress_id}", {"status": "error", "error": timeout_msg, "last_updated": time.time()})
        return None, timeout_msg
    except Exception as e:
        error_msg = f"Download error: {e}"
        if proxy:
            error_msg += f" (using proxy {proxy})"
        log.error(error_msg, exc_info=True)
        if progress_id:
            save_result(f"progress-{progress_id}", {"status": "error", "error": str(e), "last_updated": time.time()})
        return None, error_msg


def download_with_gallery_dl(
    url: str,
    output_dir: str,
    filename: Optional[str] = None,
    proxy: Optional[str] = None,
    progress_id: Optional[str] = None,
    gallery_index: Optional[int] = None,
    gallery_dl_path: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Download a TikTok post through gallery-dl, returning one media file."""
    cmd = get_gallery_dl_cmd(gallery_dl_path).copy()
    if not cmd:
        return None, "gallery-dl is not installed; run `python -m pip install gallery-dl` or set galleryDlPath"

    os.makedirs(output_dir, exist_ok=True)
    if progress_id:
        save_result(f"progress-{progress_id}", {
            "status": "starting",
            "last_updated": time.time(),
            "message": "Starting gallery-dl fallback",
        })

    try:
        with tempfile.TemporaryDirectory(prefix=".gallery-dl-", dir=output_dir) as temp_dir:
            log_path = os.path.join(temp_dir, "gallery-dl.log")
            cmd.extend([
                "--no-input",
                "--no-mtime",
                "-d", temp_dir,
                "-f", "{id}_{num}.{extension}",
            ])
            if proxy:
                cmd.extend(["--proxy", proxy])
            cmd.append(url)
            log.info("Trying gallery-dl as a TikTok download fallback")

            started_at = time.time()
            last_progress_at = 0.0
            with open(log_path, "w+", encoding="utf-8", errors="replace") as log_file:
                process = subprocess.Popen(
                    cmd,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )
                while process.poll() is None:
                    now = time.time()
                    if now - started_at >= 6 * 60 * 60:
                        process.kill()
                        process.wait()
                        raise subprocess.TimeoutExpired(cmd, 6 * 60 * 60)

                    if progress_id and now - last_progress_at >= 10:
                        downloaded_bytes = 0
                        for path in Path(temp_dir).rglob("*"):
                            if path.is_file() and path != Path(log_path) and not path.name.endswith(".part"):
                                try:
                                    downloaded_bytes += path.stat().st_size
                                except OSError:
                                    continue
                        save_result(f"progress-{progress_id}", {
                            "status": "downloading",
                            "downloaded_bytes": downloaded_bytes,
                            "total_bytes": 0,
                            "percentage": 0,
                            "last_updated": now,
                            "message": "Downloading with gallery-dl",
                        })
                        last_progress_at = now
                    time.sleep(1)

                log_file.flush()
                log_file.seek(0)
                output = log_file.read()

            if process.returncode != 0:
                error_lines = [line for line in output.splitlines() if line.strip()]
                error_detail = error_lines[-1] if error_lines else f"gallery-dl exited with status {process.returncode}"
                error_detail = re.sub(r"https?://\S+", "[URL]", error_detail)[:300]
                return None, error_detail

            files = sorted(
                (
                    path for path in Path(temp_dir).rglob("*")
                    if path.is_file() and path != Path(log_path) and not path.name.endswith(".part")
                ),
                key=lambda path: (
                    0,
                    int(re.search(r"_(\d+)$", path.stem).group(1)),
                    path.name,
                ) if re.search(r"_(\d+)$", path.stem) else (1, 0, path.name),
            )
            if not files:
                return None, "gallery-dl completed without downloading a media file"

            try:
                selected_index = int(gallery_index or 1)
            except (TypeError, ValueError):
                return None, "Invalid gallery image index"
            if selected_index < 1:
                return None, "Gallery image index must be 1 or greater"
            if selected_index > len(files):
                return None, f"gallery-dl returned {len(files)} media file(s), not image {selected_index}"
            selected_file = files[selected_index - 1]

            safe_filename = sanitize_filename(filename) if filename else ""
            if safe_filename:
                final_name = safe_filename
                if not os.path.splitext(final_name)[1]:
                    final_name += selected_file.suffix
            else:
                final_name = selected_file.name
            relative_directory = selected_file.relative_to(temp_dir).parent
            final_directory = os.path.join(output_dir, str(relative_directory))
            os.makedirs(final_directory, exist_ok=True)
            final_path = os.path.join(final_directory, final_name)
            shutil.move(str(selected_file), final_path)

            if progress_id:
                save_result(f"progress-{progress_id}", {
                    "status": "complete",
                    "percentage": 100,
                    "downloaded_bytes": os.path.getsize(final_path),
                    "file_path": final_path,
                    "last_updated": time.time(),
                })
            log.info(f"gallery-dl download complete: {os.path.basename(final_path)}")
            return final_path, None
    except subprocess.TimeoutExpired:
        error = "gallery-dl download timed out after 6 hours"
        if progress_id:
            save_result(f"progress-{progress_id}", {"status": "error", "error": error, "last_updated": time.time()})
        return None, error
    except OSError as error:
        message = f"Could not run gallery-dl: {error}"
        if progress_id:
            save_result(f"progress-{progress_id}", {"status": "error", "error": message, "last_updated": time.time()})
        return None, message


def task_download(args: dict) -> dict:
    """
    Handle download task.

    Args:
        url: URL to download (may be direct file URL or page URL)
        fallback_url: Original page URL for site-specific fallback (if url is direct file)
        output_dir: Directory to save file (defaults to /data/StashDownloader)
        filename: Optional custom filename
        quality: Quality preference (best, 1080p, 720p, 480p)
        proxy: Optional HTTP/HTTPS/SOCKS proxy (e.g., http://proxy.example.com:8080)
        gallery_dl_path: Optional custom path to gallery-dl binary
        gallery_dl_download_path: Optional directory for gallery-dl fallback files
        result_id: Optional ID for storing result (for async retrieval)
        progress_id: Optional ID for progress updates (frontend can poll this)
        gallery_index: Optional 1-based image index for TikTok photo-post fallback

    Returns:
        Dict with file_path on success, error on failure.
        If result_id is provided, also saves to file for later retrieval.
    """
    url = args.get("url")
    fallback_url = args.get("fallback_url")  # Original page URL for yt-dlp fallback
    # Default to /data/StashDownloader (configurable via plugin settings).
    # Use `or` so an empty string (no setting + no library path) also falls back.
    output_dir = args.get("output_dir") or "/data/StashDownloader"
    filename = args.get("filename")
    quality = args.get("quality", "best")
    proxy = args.get("proxy")  # Optional proxy for bypassing restrictions
    ytdlp_path = args.get("ytdlp_path")  # Optional custom path to yt-dlp binary
    gallery_dl_path = args.get("gallery_dl_path")
    gallery_dl_download_path = args.get("gallery_dl_download_path")
    gallery_index = args.get("gallery_index")
    force_direct = bool(args.get("force_direct"))
    result_id = args.get("result_id")
    progress_id = args.get("progress_id")  # For real-time progress updates

    # Log all received args for debugging
    log.info(f"Download task received args: {list(args.keys())}")
    log.info(f"Download task started: progress_id={progress_id}, result_id={result_id}")
    log.debug(f"[task_download] URL: {url}")
    if fallback_url:
        log.debug(f"[task_download] Fallback URL for yt-dlp: {fallback_url}")
    if proxy:
        log.debug(f"[task_download] Proxy configured: {proxy}")
    else:
        log.debug("[task_download] No proxy configured - using direct connection")

    if not url:
        result = {"result_error": "No URL provided", "success": False}
        if result_id:
            save_result(result_id, result)
        return result

    gallery_output_dir = gallery_dl_download_path or os.path.join(output_dir, "gallery-dl")

    # Track which URL to use for yt-dlp fallback
    ytdlp_url = url

    # Check if this is a direct file URL (try direct download first, fallback to yt-dlp)
    if force_direct or is_direct_file_url(url):
        log.info(f"Detected direct file URL, trying direct download first")
        file_path = download_direct_file(url, output_dir, filename, proxy=proxy, progress_id=progress_id)

        if file_path:
            file_size = os.path.getsize(file_path)
            result = {"file_path": file_path, "file_size": file_size, "success": True}
            if result_id:
                save_result(result_id, result)
                log.debug(f"Direct download result saved with result_id: {result_id}")
            return result
        else:
            if force_direct:
                if is_tiktok_photo_url(fallback_url):
                    gallery_file, gallery_error = download_with_gallery_dl(
                        fallback_url,
                        gallery_output_dir,
                        filename=filename,
                        proxy=proxy,
                        progress_id=progress_id,
                        gallery_index=gallery_index,
                        gallery_dl_path=gallery_dl_path,
                    )
                    if gallery_file:
                        result = {
                            "file_path": gallery_file,
                            "file_size": os.path.getsize(gallery_file),
                            "success": True,
                        }
                        if result_id:
                            save_result(result_id, result)
                        return result
                    result = {
                        "result_error": f"Direct image download failed; gallery-dl fallback failed: {gallery_error}",
                        "success": False,
                    }
                    if result_id:
                        save_result(result_id, result)
                    return result
                result = {"result_error": "Direct image download failed", "success": False}
                if result_id:
                    save_result(result_id, result)
                return result
            # Direct download failed (likely 403/hotlink protection)
            # Fall back to yt-dlp with the original page URL (if provided)
            if fallback_url:
                log.info(f"Direct download failed, using yt-dlp fallback")
                ytdlp_url = fallback_url
            else:
                log.warning("[task_download] Direct download failed, no fallback URL - trying yt-dlp with direct URL")

    # rule34video: video URLs carry short-lived access tokens and yt-dlp's extractor
    # is broken, so re-parse the page for fresh URLs instead of relying on yt-dlp
    if is_rule34video_url(ytdlp_url):
        log.info("Fetching fresh rule34video URL from page")
        fresh_url = select_format_url(extract_rule34video(ytdlp_url, proxy=proxy), quality)
        if fresh_url:
            file_path = download_direct_file(fresh_url, output_dir, filename, proxy=proxy, progress_id=progress_id)
            if file_path:
                result = {"file_path": file_path, "file_size": os.path.getsize(file_path), "success": True}
                if result_id:
                    save_result(result_id, result)
                return result
            log.warning("rule34video direct download failed, trying yt-dlp")

    is_tiktok_download = is_tiktok_url(ytdlp_url)
    # Keep yt-dlp's existing auto-install behavior for other sites. TikTok can
    # proceed directly to gallery-dl when yt-dlp is unavailable.
    ytdlp_available = bool(get_ytdlp_cmd(ytdlp_path)) if is_tiktok_download else check_ytdlp(ytdlp_path)
    if not ytdlp_available and not is_tiktok_download:
        result = {"result_error": 'yt-dlp is not installed or not accessible. Install with: pip install "yt-dlp[default,curl-cffi]" (or set ytdlpPath in plugin settings)', "success": False}
        if result_id:
            save_result(result_id, result)
        return result

    # Build filename template for yt-dlp
    if filename:
        # Sanitize custom filename
        safe_name = sanitize_filename(filename)
        template = f"{safe_name}.%(ext)s"
    else:
        template = "%(title)s.%(ext)s"

    # Download using yt-dlp (with proxy if provided)
    # Use ytdlp_url which may be the original page URL if direct download failed
    if ytdlp_available:
        file_path, download_error = download_video(
            ytdlp_url, output_dir, template, quality,
            proxy=proxy, progress_id=progress_id, ytdlp_path=ytdlp_path,
        )
    else:
        file_path, download_error = None, "yt-dlp is not installed or accessible"

    if file_path:
        file_size = os.path.getsize(file_path)
        result = {"file_path": file_path, "file_size": file_size, "success": True}
        # Save result if result_id provided (for async retrieval)
        if result_id:
            save_result(result_id, result)
            log.debug(f"Download result saved with result_id: {result_id}")
        return result

    error_msg = download_error or "Download failed"
    if is_tiktok_download:
        gallery_file, gallery_error = download_with_gallery_dl(
            ytdlp_url,
            gallery_output_dir,
            filename=filename,
            proxy=proxy,
            progress_id=progress_id,
            gallery_index=gallery_index,
            gallery_dl_path=gallery_dl_path,
        )
        if gallery_file:
            result = {
                "file_path": gallery_file,
                "file_size": os.path.getsize(gallery_file),
                "success": True,
            }
            if result_id:
                save_result(result_id, result)
            return result
        error_msg += f"; gallery-dl fallback failed: {gallery_error}"
        if ytdlp_path:
            error_msg += " Update the configured yt-dlp binary and ensure curl-cffi is installed in its Python environment."
        else:
            error_msg += ' Update yt-dlp on the Stash server with: pip install -U "yt-dlp[default,curl-cffi]".'

    result = {"result_error": error_msg, "success": False}
    if result_id:
        save_result(result_id, result)
    return result


def task_extract_metadata(args: dict) -> dict:
    """
    Extract metadata without downloading.

    Args:
        url: URL to extract metadata from
        proxy: Optional HTTP/HTTPS/SOCKS proxy (e.g., http://proxy.example.com:8080)
        result_id: Optional ID for storing result (for async retrieval)
        gallery_dl_path: Optional custom path to gallery-dl binary

    Returns:
        Dict with metadata on success, error on failure.
        If result_id is provided, also saves to file for later retrieval.
    """
    url = args.get("url")
    proxy = args.get("proxy")  # Optional proxy for bypassing restrictions
    ytdlp_path = args.get("ytdlp_path")  # Optional custom path to yt-dlp binary
    gallery_dl_path = args.get("gallery_dl_path")
    result_id = args.get("result_id")

    # Log proxy configuration for troubleshooting
    if proxy:
        log.debug(f"[task_extract_metadata] Proxy configured: {proxy}")
    else:
        log.debug("[task_extract_metadata] No proxy configured - using direct connection")

    if not url:
        result = {"result_error": "No URL provided", "success": False}
        if result_id:
            save_result(result_id, result)
        return result

    if not is_tiktok_url(url) and not check_ytdlp(ytdlp_path):
        result = {"result_error": 'yt-dlp is not installed. Install with: pip install "yt-dlp[default,curl-cffi]" (or set ytdlpPath in plugin settings)', "success": False}
        if result_id:
            save_result(result_id, result)
        return result

    metadata = extract_metadata(
        url,
        proxy=proxy,
        ytdlp_path=ytdlp_path,
        gallery_dl_path=gallery_dl_path,
    )

    # Check if extraction returned an error
    if "extraction_error" in metadata:
        error_msg = metadata["extraction_error"]
        # Add hint about proxy if not used
        if not metadata.get("used_proxy") and not proxy:
            error_msg += " (tip: try configuring a proxy in settings)"
        result = {"result_error": error_msg, "success": False}
        if result_id:
            save_result(result_id, result)
        return result

    if metadata:
        # Extract all formats with their URLs (important for HLS streams)
        # For HLS, yt-dlp may include URLs in formats or only at top level
        formats_list = []
        for f in metadata.get("formats", []):
            # Include format if it has video characteristics or is explicitly a video format
            # Don't filter too aggressively - include all formats that might be video
            is_video_format = (
                f.get("height")  # Has height = likely video
                or (f.get("vcodec") and f.get("vcodec") != "none")  # Has video codec
                or (
                    f.get("format_id")
                    and any(
                        keyword in f.get("format_id", "").lower() for keyword in ["hls", "mp4", "video", "dash", "http"]
                    )
                )  # Format ID suggests video
            )

            if is_video_format:
                format_data = {
                    "format_id": f.get("format_id"),
                    "ext": f.get("ext"),
                    "height": f.get("height"),
                    "width": f.get("width"),
                    "filesize": f.get("filesize"),
                    "vcodec": f.get("vcodec"),
                    "acodec": f.get("acodec"),
                }
                # Include URL if available (for HLS, this might be manifest_url or url)
                # yt-dlp may provide URLs in various fields for HLS streams
                if f.get("url"):
                    format_data["url"] = f.get("url")
                if f.get("manifest_url"):
                    format_data["manifest_url"] = f.get("manifest_url")
                if f.get("fragment_base_url"):
                    format_data["fragment_base_url"] = f.get("fragment_base_url")
                formats_list.append(format_data)

        result = {
            "success": True,
            "title": metadata.get("title"),
            "description": metadata.get("description"),
            "duration": metadata.get("duration"),
            "uploader": metadata.get("uploader"),
            "creator": metadata.get("creator"),
            "channel": metadata.get("channel"),  # Channel name (fallback for studio)
            "upload_date": metadata.get("upload_date"),
            "thumbnail": metadata.get("thumbnail"),
            "content_type": metadata.get("content_type", "video"),
            "gallery_images": metadata.get("gallery_images", []),
            # Tags and categories (site-specific, may be empty)
            "tags": metadata.get("tags", []),  # List of tags
            "categories": metadata.get("categories", []),  # List of categories
            # Performers (site-specific, may be empty)
            "cast": metadata.get("cast", []),  # List of cast members (Pornhub, etc.)
            "creators": metadata.get("creators", []),  # List of creators
            "artist": metadata.get("artist"),  # Artist name (music videos)
            # Include direct video URL (important for download service)
            # For HLS streams, this is typically the best quality manifest URL
            # yt-dlp's top-level 'url' is usually the best quality available
            "url": metadata.get("url"),  # Direct video URL from yt-dlp (best quality for HLS)
            "webpage_url": metadata.get("webpage_url"),  # Original page URL
            "original_url": metadata.get("original_url"),  # Fallback URL
            "height": metadata.get("height"),  # Best quality height (if available)
            "width": metadata.get("width"),  # Best quality width (if available)
            "formats": formats_list,  # All video formats with URLs
        }

        log.debug(f"Extracted {len(formats_list)} video formats from yt-dlp output")
        if result.get("url"):
            log.debug(f"Top-level URL available: {result['url'][:100]}...")
        elif result.get("content_type") != "gallery":
            log.warning("No top-level URL found in yt-dlp output")

    # Save result for async retrieval if result_id provided
    if result_id:
        save_result(result_id, result)
        log.debug(f"Metadata saved with result_id: {result_id}")

    return result


def task_read_result(args: dict) -> dict:
    """
    Read a previously saved result.

    Args:
        result_id: ID of the result to read

    Returns:
        The saved result data, or error if not found

    Note: When using runPluginOperation, Stash treats any top-level 'error' field
    as a GraphQL error. So we wrap errors in 'result_error' instead and always
    return success: true for the read operation itself.
    """
    result_id = args.get("result_id")

    # Only log progress file reads at debug level (they're frequent during download)
    is_progress_read = result_id and result_id.startswith("progress-")
    if is_progress_read:
        log.debug(f"Reading progress file: {result_id}")
    else:
        log.debug(f"Reading result for result_id: {result_id}")

    if not result_id:
        log.error("No result_id provided to read_result")
        # Don't use 'error' field - Stash treats it as GraphQL error
        return {"success": False, "result_error": "No result_id provided", "retrieved": False}

    # For progress files, missing file is expected (download may not have started yet)
    # Don't log errors for these - just return not found quietly
    is_progress_file = result_id.startswith("progress-")

    # Check if result directory exists
    if not os.path.exists(RESULT_DIR):
        if not is_progress_file:
            log.error(f"Result directory does not exist: {RESULT_DIR}")
        return {"success": True, "retrieved": False, "not_found": True}

    data = load_result(result_id, silent=is_progress_file)
    if data:
        log.debug(f"Successfully loaded result for {result_id}")

        # Return the data as-is, but ensure we don't have any field that would
        # cause write_output to set the 'error' field in PluginOutput
        # (which Stash interprets as a GraphQL error, returning null)
        result = {**data, "retrieved": True}

        # IMPORTANT: Rename 'result_error' to 'task_error' to prevent write_output
        # from promoting it to PluginOutput.error (which causes GraphQL errors)
        # The caller should check 'task_error' field instead
        if "result_error" in result:
            result["task_error"] = result.pop("result_error")

        # Also rename 'error' field if present
        if "error" in result:
            result["task_error"] = result.pop("error")

        # Ensure we always have a 'success' field for consistency
        if "success" not in result:
            # If there's a task_error, success should be False, otherwise True
            result["success"] = "task_error" not in result

        return result
    else:
        # File not found - for progress files this is expected, don't log error
        if not is_progress_file:
            log.debug(f"Result not found for result_id: {result_id}")
        # Return success:true but retrieved:false - this is not an error condition
        return {"success": True, "retrieved": False, "not_found": True}


def task_cleanup_result(args: dict) -> dict:
    """
    Delete a saved result file.

    Args:
        result_id: ID of the result to delete

    Returns:
        Success status
    """
    result_id = args.get("result_id")

    if not result_id:
        return {"result_error": "No result_id provided", "success": False}

    if delete_result(result_id):
        return {"success": True, "deleted": True}
    else:
        return {"result_error": "Failed to delete result", "success": False}


def task_check_ytdlp(args: dict) -> dict:
    """Check if yt-dlp is available and return version info.

    Note: We use 'status_message' instead of 'result_error' here because
    this is a status check, not an error condition. Using 'result_error'
    would cause write_output to set PluginOutput.error, which Stash
    interprets as a GraphQL error and returns null data.
    """
    custom_path = args.get("ytdlp_path")
    cmd = get_ytdlp_cmd(custom_path)

    if not cmd:
        log.warning("yt-dlp not found via Python module or PATH")
        return {
            "available": False,
            "success": True,
            "version": None,
            "status_message": 'yt-dlp not installed. Install with: pip install "yt-dlp[default,curl-cffi]"',
            "resolution_method": None,
        }

    try:
        result = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            version = result.stdout.strip()
            # Determine how yt-dlp was found for status reporting
            if custom_path:
                method = f"custom path ({custom_path})"
            elif cmd[0] == sys.executable:
                method = f"Python module ({sys.executable})"
            else:
                method = "CLI on PATH"
            log.debug(f"yt-dlp version: {version} (via {method})")
            return {
                "available": True,
                "success": True,
                "version": version,
                "resolution_method": method,
            }
        else:
            return {"available": False, "success": True, "version": None, "status_message": "yt-dlp check failed"}
    except subprocess.TimeoutExpired:
        log.error("yt-dlp version check timed out")
        return {"available": False, "success": True, "version": None, "status_message": "yt-dlp check timed out"}


def task_check_gallery_dl(args: dict) -> dict:
    """Check gallery-dl availability, version, and how its executable was resolved."""
    custom_path = args.get("gallery_dl_path")
    cmd = get_gallery_dl_cmd(custom_path)
    if not cmd:
        return {
            "available": False,
            "success": True,
            "version": None,
            "resolved_path": None,
            "resolution_method": None,
            "status_message": "gallery-dl not found in the Stash Python environment or PATH",
        }

    if custom_path:
        resolved_path = custom_path
        method = "custom path"
    elif cmd[0] == sys.executable:
        resolved_path = f"{sys.executable} -m gallery_dl"
        method = "Python module"
    else:
        resolved_path = cmd[0]
        method = "CLI on PATH"

    try:
        result = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return {
                "available": True,
                "success": True,
                "version": result.stdout.strip(),
                "resolved_path": resolved_path,
                "resolution_method": method,
            }
        return {
            "available": False,
            "success": True,
            "version": None,
            "resolved_path": resolved_path,
            "resolution_method": method,
            "status_message": (result.stderr or result.stdout or "gallery-dl version check failed").strip()[-300:],
        }
    except subprocess.TimeoutExpired:
        return {
            "available": False,
            "success": True,
            "version": None,
            "resolved_path": resolved_path,
            "resolution_method": method,
            "status_message": "gallery-dl version check timed out",
        }
    except OSError as error:
        return {
            "available": False,
            "success": True,
            "version": None,
            "resolved_path": resolved_path,
            "resolution_method": method,
            "status_message": f"Could not run gallery-dl: {error}",
        }


def task_test_proxy(args: dict) -> dict:
    """
    Test proxy connectivity by fetching a URL through the proxy.

    Args:
        url: URL to fetch (default: https://www.google.com)
        proxy: HTTP/HTTPS/SOCKS proxy URL

    Returns:
        Dict with success status and response info
    """
    url = args.get("url", "https://www.google.com")
    proxy = args.get("proxy")

    if not proxy:
        return {"result_error": "No proxy URL provided", "success": False}

    log.debug(f"Testing proxy {proxy} with URL {url}")

    try:
        # Use yt-dlp to test the proxy (it handles SOCKS proxies natively)
        ytdlp_path = args.get("ytdlp_path")
        cmd = get_ytdlp_cmd(ytdlp_path) + [
            "--proxy",
            proxy,
            "--no-download",
            "--no-playlist",
            "--dump-json",
            "--no-check-certificate",  # Skip SSL verification for proxy
            url,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode == 0:
            log.debug(f"✓ Proxy test successful for {url}")
            return {"success": True, "url": url, "proxy": proxy, "message": f"Successfully connected through proxy"}
        else:
            error_msg = result.stderr or result.stdout or "Unknown error"
            log.error(f"Proxy test failed: {error_msg}")
            return {
                "result_error": f"Proxy connection failed: {error_msg[:200]}",
                "success": False,
                "url": url,
                "proxy": proxy,
            }
    except subprocess.TimeoutExpired:
        log.error("Proxy test timed out")
        return {
            "result_error": "Proxy connection timed out after 30 seconds",
            "success": False,
            "url": url,
            "proxy": proxy,
        }
    except Exception as e:
        log.error(f"Proxy test error: {e}")
        return {"result_error": f"Proxy test error: {str(e)}", "success": False, "url": url, "proxy": proxy}


def task_fetch_image(args: dict) -> dict:
    """
    Fetch an image URL and return as base64.
    Used for setting cover images from thumbnails (bypasses browser CSP).

    Args:
        url: Image URL to fetch
        proxy: Optional HTTP/HTTPS/SOCKS proxy URL

    Returns:
        {success: True, image_base64: "data:image/...;base64,..."}
        or {success: False, result_error: "..."}
    """
    import base64
    import requests
    from urllib.parse import urlparse

    url = args.get("url")
    proxy = args.get("proxy")

    if not url:
        return {"result_error": "No URL provided", "success": False}

    log.info(f"Fetching image: {url[:100]}...")

    # Build headers
    parsed = urlparse(url)
    referer = f"{parsed.scheme}://{parsed.netloc}/"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "image/*,*/*",
        "Accept-Encoding": "gzip, deflate",
        "Referer": referer,
    }

    def do_fetch(use_proxy: bool) -> requests.Response:
        """Helper to fetch with or without proxy."""
        proxies = None
        if use_proxy and proxy:
            log.debug(f"Using proxy for image fetch: {proxy}")
            proxies = {"http": proxy, "https": proxy}
        return requests.get(
            url,
            headers=headers,
            proxies=proxies,
            timeout=30,
            allow_redirects=True,
        )

    try:
        # Try with proxy first (if provided)
        response = do_fetch(use_proxy=bool(proxy))
        response.raise_for_status()

    except Exception as e:
        error_msg = str(e).lower()
        # If SOCKS proxy failed (missing PySocks), retry without proxy
        # Image CDNs usually don't need proxies anyway
        if proxy and ("socks" in error_msg or "missing dependencies" in error_msg):
            log.warning(f"Proxy failed for image fetch (SOCKS issue), retrying without proxy...")
            try:
                response = do_fetch(use_proxy=False)
                response.raise_for_status()
                log.info("✓ Image fetched successfully without proxy")
            except Exception as retry_e:
                log.error(f"Failed to fetch image (retry without proxy): {retry_e}")
                return {"result_error": f"Failed to fetch image: {str(retry_e)}", "success": False}
        else:
            log.error(f"Failed to fetch image: {e}")
            return {"result_error": f"Failed to fetch image: {str(e)}", "success": False}

    try:
        # Get content type
        content_type = response.headers.get("Content-Type", "image/jpeg")
        if ";" in content_type:
            content_type = content_type.split(";")[0].strip()

        # Convert to base64
        image_data = base64.b64encode(response.content).decode("utf-8")
        data_url = f"data:{content_type};base64,{image_data}"

        log.info(f"✓ Image fetched successfully ({len(response.content)} bytes)")
        return {"success": True, "image_base64": data_url}

    except Exception as e:
        log.error(f"Failed to process image: {e}")
        return {"result_error": f"Failed to process image: {str(e)}", "success": False}


def main():
    """Main entry point."""
    try:
        input_data = read_input()

        # Debug: log the raw input received
        log.debug(f"Raw input received: {json.dumps(input_data, default=str)[:500]}")

        # Get task name and arguments
        # Stash sends JSON to stdin: {"args": {"mode": "...", ...}, "server_connection": {...}}
        # Using "mode" key matches community plugin patterns (FileMonitor, etc.)

        # Check for nested args first (standard Stash format)
        if "args" in input_data and isinstance(input_data.get("args"), dict):
            args = input_data["args"]
            # Try 'mode' first (community pattern), then 'task' for backwards compat
            task_name = args.get("mode") or args.get("task", "download")
        else:
            # Direct format (runPluginOperation or direct call)
            args = input_data
            task_name = input_data.get("mode") or input_data.get("task", "download")

        log.debug(f"Detected task: {task_name}")
        log.debug(f"Arguments: {json.dumps(args, default=str)[:200]}")

        # Route to appropriate task handler
        tasks = {
            "download": task_download,
            "extract_metadata": task_extract_metadata,
            "read_result": task_read_result,
            "cleanup_result": task_cleanup_result,
            "check_ytdlp": task_check_ytdlp,
            "check_gallery_dl": task_check_gallery_dl,
            "test_proxy": task_test_proxy,
            "fetch_image": task_fetch_image,
        }

        handler = tasks.get(task_name)
        if handler:
            result = handler(args)
        else:
            # Use 'result_error' instead of 'error' to prevent Stash from treating it as GraphQL error
            result = {"result_error": f"Unknown task: {task_name}", "success": False}

        write_output(result)
    except Exception as e:
        log.error(f"Unhandled exception in main: {e}", exc_info=True)
        # Use 'result_error' instead of 'error' to prevent Stash from treating it as GraphQL error
        error_result = {"result_error": f"Internal error: {str(e)}", "success": False}
        write_output(error_result)
        sys.exit(1)


if __name__ == "__main__":
    main()
