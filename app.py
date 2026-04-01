import io
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import streamlit as st
import yt_dlp
from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1
from mutagen.mp3 import MP3
from yt_dlp.utils import DownloadError


APP_DIR = Path(__file__).resolve().parent
TEMP_ROOT = Path(tempfile.gettempdir()) / "joe_music_downloader"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)

DOWNLOAD_UI: Dict[str, Any] = {}
STALE_SESSION_AGE = timedelta(hours=8)


def init_session_state() -> None:
    if "session_id" not in st.session_state:
        st.session_state.session_id = uuid.uuid4().hex
    if "workspace" not in st.session_state:
        workspace = TEMP_ROOT / st.session_state.session_id
        workspace.mkdir(parents=True, exist_ok=True)
        st.session_state.workspace = str(workspace)
    st.session_state.setdefault("session_downloads", [])
    st.session_state.setdefault("last_batch", None)


def get_workspace() -> Path:
    workspace = Path(st.session_state.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def cleanup_stale_workspaces() -> None:
    now = datetime.now()
    for path in TEMP_ROOT.iterdir():
        if not path.is_dir():
            continue
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        if now - modified <= STALE_SESSION_AGE:
            continue
        shutil.rmtree(path, ignore_errors=True)


def sanitize_filename(value: str, fallback: str = "download") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:160] if cleaned else fallback


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def format_seconds(seconds: Optional[float]) -> str:
    if seconds is None:
        return "--"
    total_seconds = max(int(seconds), 0)
    minutes, secs = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def detect_ffmpeg_location() -> Optional[str]:
    env_value = os.environ.get("FFMPEG_PATH")
    if env_value:
        env_path = Path(env_value)
        if env_path.is_file():
            return str(env_path.parent)
        if env_path.exists():
            return str(env_path)

    which_value = shutil.which("ffmpeg")
    if which_value:
        return str(Path(which_value).parent)

    windows_default = Path(
        r"C:\Users\Rahul\ffmpeg-2026-03-30-git-e54e117998-full_build\bin"
    )
    if windows_default.exists():
        return str(windows_default)

    return None


def detect_js_runtime() -> Optional[Tuple[str, str]]:
    for runtime_name, binary_name in (
        ("deno", "deno"),
        ("node", "node"),
        ("quickjs", "qjs"),
        ("bun", "bun"),
    ):
        executable = shutil.which(binary_name)
        if executable:
            return runtime_name, executable
    return None


def build_ydl_base_opts() -> Dict[str, Any]:
    ydl_opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
    }

    ffmpeg_location = detect_ffmpeg_location()
    if ffmpeg_location:
        ydl_opts["ffmpeg_location"] = ffmpeg_location

    js_runtime = detect_js_runtime()
    if js_runtime:
        runtime_name, executable = js_runtime
        ydl_opts["js_runtimes"] = {runtime_name: {"executable": executable}}

    return ydl_opts


def fetch_thumbnail_bytes(thumbnail_url: Optional[str]) -> Optional[bytes]:
    if not thumbnail_url:
        return None
    try:
        with urlopen(thumbnail_url, timeout=10) as response:
            return response.read()
    except (HTTPError, URLError, OSError):
        return None


def embed_metadata(file_path: Path, info: Dict[str, Any]) -> Optional[str]:
    try:
        audio = MP3(file_path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()

        audio.tags.add(TIT2(encoding=3, text=info.get("title", "")))
        audio.tags.add(
            TPE1(
                encoding=3,
                text=info.get("artist") or info.get("uploader") or "Unknown Artist",
            )
        )
        audio.tags.add(TALB(encoding=3, text=info.get("album") or "Unknown Album"))

        thumbnail_bytes = fetch_thumbnail_bytes(info.get("thumbnail"))
        if thumbnail_bytes:
            audio.tags.add(
                APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,
                    desc="Cover",
                    data=thumbnail_bytes,
                )
            )

        audio.save(file_path)
        return None
    except Exception as exc:
        return str(exc)


def clean_percent_string(percent_str: str) -> Optional[float]:
    cleaned = re.sub(r"\x1b\[[0-9;]*m", "", percent_str or "").strip()
    cleaned = cleaned.replace("%", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def get_mime_type(output_format: str) -> str:
    return {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "mp4": "video/mp4",
    }.get(output_format, "application/octet-stream")


def update_progress_state(
    item_title: str,
    item_index: int,
    total_items: int,
    percent_complete: Optional[float] = None,
    detail_text: Optional[str] = None,
    phase: str = "Downloading",
) -> None:
    status_box = DOWNLOAD_UI.get("status_box")
    detail_box = DOWNLOAD_UI.get("detail_box")
    file_progress = DOWNLOAD_UI.get("file_progress")
    overall_progress = DOWNLOAD_UI.get("overall_progress")

    if status_box:
        status_box.markdown(
            f"**{phase}:** {item_title}  \n"
            f"Track {item_index + 1} of {total_items}"
        )
    if detail_box and detail_text:
        detail_box.caption(detail_text)
    if percent_complete is not None and file_progress:
        file_progress.progress(max(0.0, min(percent_complete / 100.0, 1.0)))
    if overall_progress:
        overall_value = (item_index + (percent_complete or 0.0) / 100.0) / max(
            total_items, 1
        )
        overall_progress.progress(max(0.0, min(overall_value, 1.0)))


def progress_hook(data: Dict[str, Any]) -> None:
    item_title = DOWNLOAD_UI.get("current_title", "Current item")
    item_index = DOWNLOAD_UI.get("item_index", 0)
    total_items = DOWNLOAD_UI.get("total_items", 1)

    if data.get("status") == "downloading":
        percent = clean_percent_string(data.get("_percent_str", ""))
        details = [
            f"{percent:.1f}%" if percent is not None else None,
            f"{human_size(int(data.get('downloaded_bytes', 0)))} / {human_size(int(data.get('total_bytes') or data.get('total_bytes_estimate') or 0))}",
            f"{human_size(int(data.get('speed')))} / s" if data.get("speed") else None,
            f"ETA {format_seconds(data.get('eta'))}" if data.get("eta") is not None else None,
        ]
        update_progress_state(
            item_title,
            item_index,
            total_items,
            percent_complete=percent,
            detail_text=" | ".join(part for part in details if part),
        )
    elif data.get("status") == "finished":
        update_progress_state(
            item_title,
            item_index,
            total_items,
            percent_complete=100.0,
            detail_text="Download complete. Preparing final file...",
            phase="Processing",
        )


def get_youtube_queue(url: str) -> Tuple[List[Dict[str, str]], str]:
    ydl_opts = build_ydl_base_opts()
    ydl_opts["extract_flat"] = True
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    title = info.get("title") or "YouTube download"
    entries = info.get("entries") or []
    if not entries:
        return (
            [
                {
                    "title": info.get("title") or "Untitled video",
                    "artist": info.get("channel") or "",
                    "url": url,
                }
            ],
            title,
        )

    queue = []
    for entry in entries:
        if not entry:
            continue
        raw_url = entry.get("url") or entry.get("id")
        if not raw_url:
            continue
        if not str(raw_url).startswith("http"):
            raw_url = f"https://www.youtube.com/watch?v={raw_url}"
        queue.append(
            {
                "title": entry.get("title") or "Untitled video",
                "artist": entry.get("channel") or "",
                "url": raw_url,
            }
        )
    return queue, title


def get_spotify_queue(url: str) -> Tuple[List[Dict[str, str]], str]:
    ydl_opts = build_ydl_base_opts()
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    title = info.get("title") or "Spotify download"
    entries = info.get("entries") or []
    if not entries:
        track_title = info.get("track") or info.get("title") or "Unknown track"
        artist = info.get("artist") or info.get("uploader") or "Unknown artist"
        return (
            [
                {
                    "title": track_title,
                    "artist": artist,
                    "url": f"ytsearch1:{track_title} {artist}",
                }
            ],
            title,
        )

    queue = []
    for entry in entries:
        if not entry:
            continue
        track_title = entry.get("title") or entry.get("track") or "Unknown track"
        artist = entry.get("artist") or entry.get("uploader") or "Unknown artist"
        queue.append(
            {
                "title": track_title,
                "artist": artist,
                "url": f"ytsearch1:{track_title} {artist}",
            }
        )
    return queue, title


def build_queue(source: str, url: str) -> Tuple[List[Dict[str, str]], str]:
    if source == "Spotify":
        return get_spotify_queue(url)
    return get_youtube_queue(url)


def resolve_output_path(
    info: Dict[str, Any], prepared_path: Path, output_format: str
) -> Path:
    candidates: List[Path] = []

    requested_downloads = info.get("requested_downloads") or []
    for item in requested_downloads:
        filepath = item.get("filepath")
        if filepath:
            candidates.append(Path(filepath))

    for field in ("filepath", "_filename"):
        filepath = info.get(field)
        if filepath:
            candidates.append(Path(filepath))

    candidates.append(prepared_path)
    candidates.append(prepared_path.with_suffix(f".{output_format}"))
    candidates.append(prepared_path.with_suffix(".webm"))
    candidates.append(prepared_path.with_suffix(".m4a"))
    candidates.append(prepared_path.with_suffix(".mkv"))

    for candidate in candidates:
        if candidate.exists():
            if output_format == "mp4" and candidate.suffix.lower() != ".mp4":
                converted = candidate.with_suffix(".mp4")
                if converted.exists():
                    return converted
            return candidate

    fallback = prepared_path.parent / f"{prepared_path.stem}.{output_format}"
    return fallback


def build_download_filename(
    info: Dict[str, Any], output_format: str, item_index: int
) -> str:
    artist = info.get("artist") or info.get("uploader") or ""
    title = info.get("title") or f"track-{item_index + 1}"
    base = f"{artist} - {title}" if artist else title
    return f"{sanitize_filename(base, fallback=f'track-{item_index + 1}')}.{output_format}"


def download_media(
    url: str,
    output_format: str,
    quality: str,
    item_index: int,
    embed_tags: bool,
) -> Tuple[Path, Dict[str, Any], str]:
    download_dir = get_workspace()
    ydl_opts = build_ydl_base_opts()
    ydl_opts.update(
        {
        "outtmpl": str(download_dir / "%(title).180B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "progress_hooks": [progress_hook],
        }
    )

    if output_format == "mp4":
        ydl_opts["format"] = "bestvideo*+bestaudio/best"
        ydl_opts["merge_output_format"] = "mp4"
    elif output_format == "wav":
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "wav"}
        ]
    else:
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": quality,
            }
        ]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info.get("entries"):
            info = info["entries"][0]
        prepared_path = Path(ydl.prepare_filename(info))

    output_path = resolve_output_path(info, prepared_path, output_format)
    download_name = build_download_filename(info, output_format, item_index)

    if output_path.exists():
        final_path = output_path.with_name(download_name)
        if final_path != output_path:
            if final_path.exists():
                final_path.unlink()
            output_path.rename(final_path)
            output_path = final_path

    if output_format == "mp3" and output_path.exists() and embed_tags:
        metadata_error = embed_metadata(output_path, info)
        if metadata_error:
            st.warning(
                f"Metadata could not be fully embedded for {info.get('title', 'this file')}: {metadata_error}"
            )

    return output_path, info, download_name


def build_zip_bundle(
    items: List[Dict[str, Any]], collection_title: str, output_format: str
) -> Optional[Tuple[bytes, str]]:
    if len(items) < 2:
        return None

    zip_name = f"{sanitize_filename(collection_title, 'playlist')}-{output_format}.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in items:
            file_path = Path(item["file_path"])
            if not file_path.exists():
                continue
            archive.write(file_path, arcname=item["download_name"])
    buffer.seek(0)
    return buffer.getvalue(), zip_name


def add_session_results(batch: Dict[str, Any]) -> None:
    items = batch.get("items", [])
    existing = st.session_state.session_downloads
    st.session_state.session_downloads = (items + existing)[:18]
    st.session_state.last_batch = batch


def render_download_panel() -> Dict[str, Any]:
    st.markdown("### Download Progress")
    status_box = st.empty()
    detail_box = st.empty()
    file_progress = st.progress(0.0)
    overall_progress = st.progress(0.0)
    return {
        "status_box": status_box,
        "detail_box": detail_box,
        "file_progress": file_progress,
        "overall_progress": overall_progress,
    }


def process_download_request(
    url: str,
    source: str,
    output_format: str,
    quality: str,
    playlist_limit: int,
    embed_tags: bool,
    progress_panel: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    try:
        queue, collection_title = build_queue(source, url)
    except DownloadError as exc:
        st.error(
            "The source rejected the request while reading the link. "
            "This usually means the hosting environment is missing a modern yt-dlp JS runtime, "
            "or YouTube blocked that request path."
        )
        st.caption(str(exc))
        return None
    except Exception as exc:
        st.error(f"Could not inspect that link: {exc}")
        return None

    if not queue:
        st.error("Nothing was found to download from that link.")
        return None

    if len(queue) > playlist_limit:
        st.warning(
            f"Loaded {len(queue)} items. For stability on free hosting, this run will process only the first {playlist_limit}."
        )
        queue = queue[:playlist_limit]

    st.info(f"Loaded {len(queue)} item(s) from {collection_title}.")

    DOWNLOAD_UI.clear()
    DOWNLOAD_UI.update(progress_panel)
    DOWNLOAD_UI["total_items"] = len(queue)

    successes: List[Dict[str, Any]] = []
    failures: List[str] = []

    for item_index, item in enumerate(queue):
        DOWNLOAD_UI["current_title"] = item["title"]
        DOWNLOAD_UI["item_index"] = item_index
        update_progress_state(
            item["title"],
            item_index,
            len(queue),
            percent_complete=0.0,
            detail_text="Waiting for yt-dlp...",
        )
        try:
            file_path, info, download_name = download_media(
                item["url"],
                output_format,
                quality,
                item_index,
                embed_tags,
            )
            if not file_path.exists():
                raise FileNotFoundError(f"Output file missing: {file_path.name}")

            item_result = {
                "title": info.get("title") or item["title"],
                "artist": info.get("artist") or info.get("uploader") or item["artist"],
                "source": source,
                "format": output_format,
                "mime": get_mime_type(output_format),
                "file_path": str(file_path),
                "download_name": download_name,
                "size_bytes": file_path.stat().st_size,
                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "thumbnail": info.get("thumbnail"),
                "web_url": info.get("webpage_url") or item["url"],
            }
            successes.append(item_result)

            update_progress_state(
                item_result["title"],
                item_index,
                len(queue),
                percent_complete=100.0,
                detail_text=f"Ready: {download_name}",
                phase="Completed",
            )
            progress_panel["overall_progress"].progress((item_index + 1) / len(queue))
        except Exception as exc:
            failures.append(f"{item['title']}: {exc}")
            st.error(f"Failed: {item['title']} ({exc})")

    progress_panel["file_progress"].progress(1.0 if successes else 0.0)

    if failures:
        st.warning(f"Finished with {len(failures)} failure(s).")
        for failure in failures[:6]:
            st.caption(failure)
    elif successes:
        progress_panel["detail_box"].caption("All selected items are ready.")

    DOWNLOAD_UI.clear()

    if not successes:
        return None

    zip_bundle = None
    if output_format in {"mp3", "wav"} and len(successes) > 1:
        zip_bundle = build_zip_bundle(successes, collection_title, output_format)

    batch = {
        "title": collection_title,
        "source": source,
        "format": output_format,
        "items": successes,
        "zip_bundle": zip_bundle,
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    add_session_results(batch)
    return batch


def render_browser_save_note() -> None:
    st.info(
        "On Streamlit Community Cloud, the app cannot open your computer's file explorer. "
        "Use the download buttons below. Your browser will save the file to its default Downloads folder "
        "or ask for a location if you enabled that in browser settings."
    )


def render_batch_summary(batch: Optional[Dict[str, Any]]) -> None:
    st.markdown("### Delivery")
    if not batch:
        st.caption("Start a download to generate files for this session.")
        return

    item_count = len(batch["items"])
    total_size = sum(item["size_bytes"] for item in batch["items"])
    st.markdown(
        f"**{batch['title']}**  \n"
        f"{item_count} item(s) | {batch['format'].upper()} | {human_size(total_size)} | {batch['finished_at']}"
    )

    zip_bundle = batch.get("zip_bundle")
    if zip_bundle:
        zip_bytes, zip_name = zip_bundle
        st.download_button(
            "Download whole playlist as ZIP",
            data=zip_bytes,
            file_name=zip_name,
            mime="application/zip",
            use_container_width=True,
            key=f"zip-{zip_name}",
        )
        st.caption("Best option for bulk audio downloads on the cloud version.")


def render_quick_check(items: List[Dict[str, Any]]) -> None:
    st.markdown("### Quick Check")
    if not items:
        st.caption("Downloaded tracks and videos will appear here.")
        return

    for index, item in enumerate(items[:6]):
        label = f"{item['title']} [{item['format'].upper()}]"
        with st.expander(label, expanded=index == 0):
            meta = item["artist"] or item["source"]
            st.caption(f"{meta} | {human_size(item['size_bytes'])} | {item['saved_at']}")
            file_path = Path(item["file_path"])
            if not file_path.exists():
                st.warning("This temporary file is no longer available.")
                continue

            try:
                file_data = file_path.read_bytes()
            except OSError as exc:
                st.error(f"Could not load the file: {exc}")
                continue

            if item["format"] in {"mp3", "wav"}:
                st.audio(file_data, format=item["mime"])
            elif item["format"] == "mp4":
                st.video(file_data)

            st.download_button(
                "Download this file",
                data=file_data,
                file_name=item["download_name"],
                mime=item["mime"],
                use_container_width=True,
                key=f"single-{index}-{item['download_name']}",
            )


def apply_styles() -> None:
    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 1.5rem;
            padding-bottom: 2rem;
            max-width: 1180px;
        }
        .hero {
            padding: 1.35rem 1.4rem;
            border-radius: 24px;
            background:
                radial-gradient(circle at top right, rgba(244, 163, 97, 0.35), transparent 30%),
                linear-gradient(135deg, #0f172a 0%, #16213f 45%, #1f3a5f 100%);
            color: #f8fafc;
            border: 1px solid rgba(148, 163, 184, 0.28);
            margin-bottom: 1rem;
        }
        .hero h1 {
            margin: 0 0 0.35rem 0;
            font-size: 2rem;
            line-height: 1.05;
        }
        .hero p {
            margin: 0;
            max-width: 56rem;
            color: rgba(241, 245, 249, 0.9);
        }
        .stats {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 0.75rem;
            margin: 1rem 0 0 0;
        }
        .stat-card {
            background: rgba(255, 255, 255, 0.08);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 16px;
            padding: 0.85rem 0.9rem;
        }
        .stat-card span {
            display: block;
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            opacity: 0.72;
        }
        .stat-card strong {
            display: block;
            font-size: 1rem;
            margin-top: 0.15rem;
        }
        .surface {
            padding: 1rem 1.05rem;
            border-radius: 18px;
            background: rgba(248, 250, 252, 0.76);
            border: 1px solid rgba(148, 163, 184, 0.3);
            margin-bottom: 1rem;
        }
        @media (max-width: 900px) {
            .stats {
                grid-template-columns: 1fr;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_hero(source: str, output_format: str, quality: str, playlist_limit: int) -> None:
    quality_text = f"{quality} kbps" if output_format == "mp3" else "best available"
    ffmpeg_state = "Detected" if detect_ffmpeg_location() else "Missing"
    js_runtime_info = detect_js_runtime()
    js_runtime = js_runtime_info[0] if js_runtime_info else "Missing"
    st.markdown(
        f"""
        <div class="hero">
            <h1>Cloud Music Downloader</h1>
            <p>Built for Streamlit Community Cloud: lightweight queue handling, inline playback, and browser-first delivery for single tracks and playlist bundles.</p>
            <div class="stats">
                <div class="stat-card">
                    <span>Source</span>
                    <strong>{source}</strong>
                </div>
                <div class="stat-card">
                    <span>Output</span>
                    <strong>{output_format.upper()} • {quality_text}</strong>
                </div>
                <div class="stat-card">
                    <span>Safety limit</span>
                    <strong>{playlist_limit} items per run • FFmpeg {ffmpeg_state} • JS {js_runtime}</strong>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


st.set_page_config(page_title="Cloud Music Downloader", layout="wide")
cleanup_stale_workspaces()
init_session_state()
apply_styles()

with st.sidebar:
    st.header("Controls")
    source = st.radio("Source", ["YouTube", "Spotify"], horizontal=True)
    output_format = st.selectbox("Format", ["mp3", "mp4", "wav"], index=0)
    quality = "192"
    if output_format == "mp3":
        quality = st.selectbox("MP3 quality", ["128", "192", "320"], index=1)

    playlist_limit = st.slider("Playlist cap per run", 1, 50, 15)
    embed_tags = st.checkbox("Embed MP3 metadata", value=True)

    ffmpeg_location = detect_ffmpeg_location()
    if ffmpeg_location:
        st.caption(f"FFmpeg ready: {ffmpeg_location}")
    else:
        st.warning("FFmpeg not found. MP3/WAV conversion may fail until `packages.txt` is installed on Streamlit Cloud.")

    js_runtime = detect_js_runtime()
    if js_runtime:
        st.caption(f"JS runtime ready for yt-dlp: {js_runtime[0]}")
    else:
        st.warning(
            "No JS runtime found. Modern YouTube downloads often fail without one on hosted Linux."
        )

    st.markdown("---")
    st.caption(
        "Cloud note: files are temporary on the server. Download what you need during the current session."
    )

render_hero(source, output_format, quality, playlist_limit)

left_col, right_col = st.columns([1.35, 1.0], gap="large")

with left_col:
    st.markdown('<div class="surface">', unsafe_allow_html=True)
    st.markdown("### Input")
    url = st.text_input(
        "Paste a YouTube or Spotify link",
        placeholder="https://www.youtube.com/watch?v=... or https://open.spotify.com/playlist/...",
    )
    render_browser_save_note()
    start_download = st.button("Start Download", type="primary", use_container_width=True)
    if start_download:
        if not url.strip():
            st.warning("Enter a valid URL first.")
        else:
            panel = render_download_panel()
            process_download_request(
                url=url.strip(),
                source=source,
                output_format=output_format,
                quality=quality,
                playlist_limit=playlist_limit,
                embed_tags=embed_tags,
                progress_panel=panel,
            )
    st.markdown("</div>", unsafe_allow_html=True)

    last_batch = st.session_state.last_batch
    render_batch_summary(last_batch)

with right_col:
    render_quick_check(st.session_state.session_downloads)
