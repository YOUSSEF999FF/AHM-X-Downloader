# Upstream references and implementation notes

AHM X Downloader Bot V3 is an independent Render-oriented implementation. Its feature choices and architecture were informed by the public documentation of the following open-source projects:

- `antlis/tg-media-bot` — https://github.com/antlis/tg-media-bot (GitHub identifies the repository as MIT-licensed). Reference ideas: user allowlisting, rate limiting, progress updates, concurrency caps, and cleanup of temporary files.
- `Avazbek22/VideoDownloaderBot` — https://github.com/Avazbek22/VideoDownloaderBot. Reference ideas: metadata-first workflow, quality selection, MP3 option, and strict Telegram file-size checks.
- `yt-dlp` — https://github.com/yt-dlp/yt-dlp and its EJS documentation — https://github.com/yt-dlp/yt-dlp/wiki/ejs.

No third-party source file is represented as copied verbatim in this bundle. Direct source cloning was unavailable in the build environment, so this implementation was authored as a clean, single-Web-Service application based on documented design patterns. Consult each upstream repository's current LICENSE and notices before redistributing any of its files.

The bot does not collect cookies, automate sign-in, bypass DRM/paywalls, or defeat a platform's login or anti-bot challenge. Download only content you own or are authorized to download.
