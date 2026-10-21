# Changelog

## V4 — modular bot + Mini App hardening

- Separate downloader handlers for YouTube, Facebook, Instagram, and generic supported URLs.
- Restored crimson/black Telegram Mini App, quality selection, thumbnail/metadata preview, MP3, progress, cancellation, and download history pagination.
- Owner/admin panel with global concurrency and daily quotas, per-user block/quota controls, maintenance mode, platform toggles, upload-size and max-height settings.
- Owner-only admin management; bootstrap environment admins are seeded only when the admin database is empty.
- Detailed diagnostic records with error IDs and traceback viewer; maintenance interaction notifications are rate-limited per user.
- Dynamic concurrency updates now apply to tasks already waiting in the queue.
- User-management listing is paginated, daily limits reset at Cairo midnight, and interrupted jobs are marked after service restart.
- Avoid Markdown parsing of external video titles/error text in `/history` and `/errors`.
- Fixed missing `shutil` import used during cleanup.
- Render Blueprint uses a paid persistent disk so history/settings can survive redeploys; review costs before deploying.
- YouTube `LOGIN_REQUIRED` / anti-bot challenges remain hosting- and platform-dependent. Optional Cobalt integration requires an owned or authorized instance; no cookies or login automation are included.
