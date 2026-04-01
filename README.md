# Cloud Music Downloader

Streamlit app for downloading YouTube or Spotify-linked audio/video with:

- live per-track and overall progress
- inline audio/video preview
- single-file browser downloads
- ZIP export for playlist audio batches

## Deploy to Streamlit Community Cloud

1. Push this folder to a GitHub repository.
2. In Streamlit Community Cloud, create a new app from that repo.
3. Set the main file path to `app.py`.
4. Deploy.

`requirements.txt` installs Python packages and `packages.txt` installs Linux packages used by Streamlit Community Cloud. The app needs `ffmpeg` for MP3/WAV conversion, and `nodejs` helps modern `yt-dlp` handle YouTube on hosted Linux.

## Important cloud behavior

- The app cannot open the user's local file explorer.
- Files are generated on the Streamlit server and then delivered through browser download buttons.
- For playlist audio downloads, use the ZIP button to save the whole batch.
- Server-side files are temporary, so download files during the active session.

## Local run

```powershell
streamlit run app.py
```
