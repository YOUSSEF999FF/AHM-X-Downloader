# AHM X Downloader Bot V3 🔴⚫

Telegram video/audio downloader bot for Render **Web Service**. This version intentionally focuses on the Telegram bot; the Mini App is deferred until the core bot is stable.

## Features

- Send a public video URL; the bot tries `yt-dlp` metadata extraction.
- Displays title, uploader, duration, views (when available), and thumbnail (when available).
- Inline buttons for available video resolutions and MP3 audio extraction.
- Estimated sizes where metadata makes a reasonable estimate possible.
- Progress/status edits and a cancel button during downloads.
- Conservative Telegram upload cap (default 47 MiB (conservative margin under the cloud Bot API limit)), checked during and after downloading.
- Private-chat-only downloads; optional `ALLOWED_USER_IDS` allowlist and admin commands.
- Temporary files are deleted after each job.
- FastAPI `/health` endpoint keeps it deployable as a Render Web Service and reports `yt-dlp`, `yt-dlp-ejs`, Node and FFmpeg versions.
- Uses Telegram webhook; no polling loop is required.

## Important platform limitation

`yt-dlp` supports many sites, but support changes and not every URL will work. YouTube may reject a Render IP with `Sign in to confirm you're not a bot`; the bot will show a clear failure. The code does not bypass sign-in/anti-bot challenges or use account cookies. Updating `yt-dlp` and installing its JS runtime helps with ordinary extraction compatibility but cannot force a platform to serve a video.

## Deploy to Render (mobile-friendly)

1. Download this ZIP and extract it.
2. Create a GitHub repository and upload the **contents** of this folder to the repository root. `Dockerfile` and `render.yaml` should be at the root.
3. In Render, choose **New → Blueprint** and connect that repository. Render creates a **Web Service**, not a Background Worker.
4. Add `TELEGRAM_BOT_TOKEN` from `@BotFather` as a secret/environment variable. The Blueprint generates `WEBHOOK_SECRET` automatically.
5. Fill `ADMIN_IDS` with your numeric Telegram ID. You can get it by opening the bot and sending `/id`. Optional: set `ALLOWED_USER_IDS` to a comma-separated allowlist. If left empty, any user can use it in private chat.
6. Deploy, then open `https://YOUR-SERVICE.onrender.com/health`. Verify it returns `status: ok`, `yt_dlp_ejs` is not `missing`, Node is `v22.x`, and FFmpeg is available.
7. Open the Telegram bot and send `/start`, then test a short public video from a site that permits access.

For an existing Render service, push these files to the same repository and trigger **Manual Deploy → Clear build cache & deploy** so Docker installs the dependencies again.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from BotFather. Keep secret. |
| `WEBHOOK_SECRET` | Render Blueprint generates it | Authenticates Telegram webhook requests. |
| `WEBHOOK_URL` | Local only if `RENDER_EXTERNAL_URL` is absent | Public HTTPS service URL. Render usually supplies `RENDER_EXTERNAL_URL`. |
| `ADMIN_IDS` | Optional | Comma-separated numeric Telegram admin IDs. |
| `ALLOWED_USER_IDS` | Optional | Comma-separated IDs allowed to use the bot; empty means no allowlist. |
| `MAX_UPLOAD_MB` | No | Upload limit, clamped to 5–47 MiB. |
| `MAX_VIDEO_HEIGHT` | No | Highest quality button, default 1080. |
| `MAX_ACTIVE_DOWNLOADS` | No | Concurrent downloads, default 1. |
| `REQUEST_COOLDOWN_SECONDS` | No | Per-user cooldown between URL submissions. |
| `LOG_LEVEL` | No | Logging verbosity. |
| `YTDLP_NODE_PATH` | No | Node.js path for yt-dlp JavaScript compatibility. |

## Commands

- `/start` — welcome and basics
- `/help` — usage instructions
- `/cancel` — ask to cancel the current download
- `/id` — show your Telegram user ID
- `/status` — admin-only operational status
- `/maintenance on` and `/maintenance off` — admin-only maintenance mode

## Local Docker run

1. Copy `.env.example` to `.env` and set the token, HTTPS `WEBHOOK_URL`, and `WEBHOOK_SECRET`.
2. Run:

```bash
docker build -t ahm-x-downloader-bot .
docker run --env-file .env -p 10000:10000 -e PORT=10000 ahm-x-downloader-bot
```

Telegram must be able to reach the HTTPS URL in `WEBHOOK_URL`; plain localhost will not receive Telegram webhooks unless you provide a secure public tunnel.

## Testing notes

The package was statically checked for Python syntax, YAML validity, Docker/config presence, and expected core helper behavior. It has not been connected to your real Telegram bot token and a real Render deployment from this environment, so the first live deployment still needs a short smoke test.

## Open-source reference notes

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for the reference repositories and licensing note. This bundle is a clean implementation inspired by documented features, not a claimed verbatim fork of any repository.
