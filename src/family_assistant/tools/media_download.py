"""Tools for downloading media from URLs using yt-dlp."""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import DownloadError, sanitize_filename

from family_assistant.tools.types import (
    ToolAttachment,
    ToolExecutionContext,
    ToolResult,
    get_attachment_limits,
)

logger = logging.getLogger(__name__)

# Allowed URL schemes - only http(s) to prevent local file access via file:// etc.
ALLOWED_URL_SCHEMES = {"http", "https"}


def _validate_url(url: str) -> str | None:
    """
    Validate that a URL is safe to download from.

    Args:
        url: The URL to validate.

    Returns:
        None if valid, error message string if invalid.
    """
    try:
        parsed = urlparse(url)
        if not parsed.scheme:
            return "URL must include a scheme (e.g., https://)"
        if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
            return f"URL scheme '{parsed.scheme}' not allowed. Only http/https URLs are supported."
        if not parsed.netloc:
            return "URL must include a host"
        return None
    except Exception as e:
        return f"Invalid URL format: {e}"


def _sanitize_title(title: str) -> str:
    """
    Sanitize a title for use as a filename.

    Uses yt-dlp's sanitize_filename and handles edge cases like
    leading/trailing dots and consecutive dots.
    """
    # Use yt-dlp's sanitization first
    safe = sanitize_filename(title, restricted=False)
    # Remove leading/trailing dots and spaces
    safe = safe.strip(". ")
    # Collapse multiple dots to single dot
    safe = re.sub(r"\.{2,}", ".", safe)
    # If empty or contains no alphanumeric characters (yt-dlp converts some chars
    # to Unicode look-alikes like ⧸ for /), use fallback
    if not safe or not any(c.isalnum() for c in safe):
        return "download"
    return safe


def _format_duration(duration_seconds: int | None) -> str:
    """Format duration in seconds to human-readable string like '1h 2m 3s'."""
    if not duration_seconds:
        return ""
    minutes, seconds = divmod(int(duration_seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


# Format selection with generous fallbacks for various sites
# Prefers 720p mp4 but falls back to best available for sites like Instagram
# that don't provide separate streams or standard formats
VIDEO_FORMAT = (
    "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"  # Best: separate streams, mp4
    "bestvideo[height<=720]+bestaudio/"  # Fallback: any format separate streams
    "best[height<=720][ext=mp4]/"  # Combined stream mp4
    "best[height<=720]/"  # Combined stream any format
    "best"  # Ultimate fallback: best available
)
AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"


class MediaMetadata(TypedDict, total=False):
    """Metadata extracted from a media URL."""

    title: str | None
    duration: int | None
    uploader: str | None
    upload_date: str | None
    view_count: int | None
    description: str | None
    thumbnail: str | None
    webpage_url: str | None
    extractor: str | None


# ast-grep-ignore: no-dict-any - OpenAI function calling spec requires untyped dict for JSON schema
MEDIA_DOWNLOAD_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "download_media",
            "description": (
                "Downloads video or audio from a URL using yt-dlp. "
                "Supports YouTube and 1000+ other sites. "
                "Returns the downloaded file as an attachment. "
                "Use metadata_only=true to get information without downloading."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to download from (YouTube, Vimeo, etc.)",
                    },
                    "audio_only": {
                        "type": "boolean",
                        "description": "Download audio only (no video). Defaults to false.",
                        "default": False,
                    },
                    "metadata_only": {
                        "type": "boolean",
                        "description": "Extract metadata without downloading the file. Useful for getting video info first.",
                        "default": False,
                    },
                },
                "required": ["url"],
            },
        },
    },
]


def _extract_metadata(url: str) -> MediaMetadata:
    """
    Extract metadata from a URL without downloading.

    Args:
        url: The URL to extract metadata from.

    Returns:
        Dictionary containing video metadata.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,  # Get full metadata
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # pyright: ignore[reportArgumentType] - yt_dlp lacks type stubs
        info = ydl.extract_info(url, download=False)
        if info is None:
            raise ValueError(f"Could not extract metadata from {url}")

        description = info.get("description", "") or ""
        return MediaMetadata(
            title=info.get("title"),
            duration=info.get("duration"),
            uploader=info.get("uploader"),
            upload_date=info.get("upload_date"),
            view_count=info.get("view_count"),
            description=description[:500],  # Truncate long descriptions
            thumbnail=info.get("thumbnail"),
            webpage_url=info.get("webpage_url"),
            extractor=info.get("extractor"),
        )


def _download_media(
    url: str,
    output_dir: str,
    audio_only: bool,
    max_filesize: int,
) -> tuple[Path, MediaMetadata]:
    """
    Download media from a URL.

    Args:
        url: The URL to download from.
        output_dir: Directory to save the downloaded file.
        audio_only: Whether to download audio only.
        max_filesize: Maximum file size in bytes.

    Returns:
        Tuple of (file_path, metadata_dict).
    """
    # Use fixed quality format
    format_spec = AUDIO_FORMAT if audio_only else VIDEO_FORMAT

    postprocessors: list[dict[str, str]] = []

    # For audio-only, extract audio and convert to m4a
    if audio_only:
        postprocessors.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": "m4a",
            "preferredquality": "192",
        })

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": format_spec,
        "outtmpl": str(Path(output_dir) / "%(title).100s.%(ext)s"),
        "max_filesize": max_filesize,
        # Merge into mp4 for video, m4a for audio
        "merge_output_format": "mp4" if not audio_only else None,
        "postprocessors": postprocessors,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # pyright: ignore[reportArgumentType] - yt_dlp lacks type stubs
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise ValueError(f"Could not download from {url}")

        # Find the downloaded file
        # yt-dlp stores the final filename in different places depending on post-processing
        filepath: str | None = None
        requested_downloads = info.get("requested_downloads")
        if requested_downloads and len(requested_downloads) > 0:
            filepath = requested_downloads[0].get("filepath")

        if filepath is None:
            # Fallback: construct from template
            ext = "m4a" if audio_only else info.get("ext", "mp4")
            title = info.get("title", "download")
            title_truncated = str(title)[:100] if title else "download"
            safe_title = _sanitize_title(title_truncated)
            filepath = str(Path(output_dir) / f"{safe_title}.{ext}")

        # Remember expected stem for matching
        expected_stem = Path(filepath).stem if filepath else None
        file_path = Path(filepath)

        # Find the actual file if the exact path doesn't exist
        if not file_path.exists():
            output_path = Path(output_dir)
            candidates: list[Path] = []
            # Look for any media file in the output directory
            for ext in ["mp4", "m4a", "webm", "mkv", "mp3", "opus"]:
                candidates.extend(output_path.glob(f"*.{ext}"))

            # Prefer files whose stem matches the expected stem
            if expected_stem and candidates:
                stem_matches = [p for p in candidates if p.stem == expected_stem]
                if stem_matches:
                    candidates = stem_matches

            if candidates:
                if len(candidates) > 1:
                    logger.warning(
                        "Multiple downloaded media files found in %s; "
                        "choosing the most recently modified. Candidates: %s",
                        output_dir,
                        [str(p) for p in candidates],
                    )
                # Choose the most recently modified candidate
                file_path = max(candidates, key=lambda p: p.stat().st_mtime)

        if not file_path.exists():
            raise FileNotFoundError(f"Downloaded file not found at {filepath}")

        metadata = MediaMetadata(
            title=info.get("title"),
            duration=info.get("duration"),
            uploader=info.get("uploader"),
            upload_date=info.get("upload_date"),
            webpage_url=info.get("webpage_url"),
            extractor=info.get("extractor"),
        )

        return file_path, metadata


async def download_media_tool(
    exec_context: ToolExecutionContext,
    url: str,
    audio_only: bool = False,
    metadata_only: bool = False,
) -> ToolResult:
    """
    Downloads media from a URL using yt-dlp.

    Args:
        exec_context: The tool execution context.
        url: The URL to download from.
        audio_only: Download audio only (no video).
        metadata_only: Extract metadata without downloading.

    Returns:
        ToolResult containing the downloaded file as an attachment or metadata.
    """
    logger.info(
        f"download_media_tool called: url={url}, audio_only={audio_only}, metadata_only={metadata_only}"
    )

    # Validate URL scheme to prevent local file access
    url_error = _validate_url(url)
    if url_error:
        return ToolResult(
            text=f"Invalid URL: {url_error}",
            data={
                "error": "invalid_url",
                "error_type": "validation_failed",
                "message": url_error,
            },
        )

    # Get file size limits from config
    max_file_size, _ = get_attachment_limits(exec_context)

    try:
        if metadata_only:
            # Just extract metadata without downloading
            metadata = await asyncio.to_thread(_extract_metadata, url)

            duration_str = _format_duration(metadata.get("duration"))

            return ToolResult(
                text=f"Media info for: {metadata.get('title', 'Unknown')}\n"
                f"Duration: {duration_str or 'Unknown'}\n"
                f"Uploader: {metadata.get('uploader', 'Unknown')}\n"
                f"Source: {metadata.get('extractor', 'Unknown')}",
                data={
                    "status": "success",
                    "metadata": dict(metadata),
                },
            )

        # Download the media
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path, metadata = await asyncio.to_thread(
                _download_media,
                url,
                temp_dir,
                audio_only,
                max_file_size,
            )

            # Check file size
            file_size = file_path.stat().st_size
            if file_size > max_file_size:
                return ToolResult(
                    text=f"Downloaded file exceeds size limit ({file_size / 1024 / 1024:.1f}MB > {max_file_size / 1024 / 1024:.1f}MB)",
                    data={
                        "error": "file_too_large",
                        "file_size": file_size,
                        "max_size": max_file_size,
                    },
                )

            # Read file content
            content = await asyncio.to_thread(file_path.read_bytes)

            # Determine MIME type
            ext = file_path.suffix.lower()
            mime_type_map = {
                ".mp4": "video/mp4",
                ".m4a": "audio/mp4",
                ".webm": "video/webm",
                ".mkv": "video/x-matroska",
                ".mp3": "audio/mpeg",
                ".opus": "audio/opus",
            }
            mime_type = mime_type_map.get(ext, "application/octet-stream")

            # Create attachment
            title = metadata.get("title") or "download"
            attachment = ToolAttachment(
                content=content,
                mime_type=mime_type,
                description=f"{'Audio' if audio_only else 'Video'}: {title[:100]}",
            )

            duration = metadata.get("duration")
            duration_str = _format_duration(duration)

            return ToolResult(
                text=f"Downloaded {'audio' if audio_only else 'video'}: {title}\n"
                f"Duration: {duration_str or 'Unknown'}\n"
                f"Size: {file_size / 1024 / 1024:.1f}MB",
                attachments=[attachment],
                data={
                    "status": "success",
                    "title": title,
                    "duration": duration,
                    "file_size": file_size,
                    "mime_type": mime_type,
                },
            )

    except DownloadError as e:
        error_msg = str(e)
        logger.warning(f"yt-dlp download error: {error_msg}")
        return ToolResult(
            text=f"Download failed: {error_msg}",
            data={
                "error": "download_failed",
                "error_type": "yt_dlp_error",
                "message": error_msg,
            },
        )
    except ValueError as e:
        error_msg = str(e)
        logger.warning(f"Value error in download_media_tool: {error_msg}")
        # Determine specific error type
        if "metadata" in error_msg.lower():
            error_type = "metadata_extraction_failed"
        elif "download" in error_msg.lower():
            error_type = "download_failed"
        else:
            error_type = "validation_error"
        return ToolResult(
            text=f"Failed: {error_msg}",
            data={
                "error": error_msg,
                "error_type": error_type,
            },
        )
    except FileNotFoundError as e:
        error_msg = str(e)
        logger.warning(f"File not found in download_media_tool: {error_msg}")
        return ToolResult(
            text=f"Download failed: {error_msg}",
            data={
                "error": "file_not_found",
                "error_type": "post_download_error",
                "message": error_msg,
            },
        )
    except Exception as e:
        logger.error(f"Error in download_media_tool: {e}", exc_info=True)
        return ToolResult(
            text=f"An error occurred: {str(e)}",
            data={
                "error": str(e),
                "error_type": "unexpected_error",
            },
        )
