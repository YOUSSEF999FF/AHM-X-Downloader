# Third-party references and implementation notes

AHM X Downloader V4 is an independently authored, modular Render Web Service project. The architecture and feature ideas were informed by public documentation and repository descriptions:

- `TameyAnderson/sdp-downloader` — https://github.com/TameyAnderson/sdp-downloader — reference for separating downloader engines, verifying outputs before delivery, Mini App progress, runtime settings, and local SQLite history.
- `Avazbek22/VideoDownloaderBot` — https://github.com/Avazbek22/VideoDownloaderBot — reference for metadata-first flow, quality selection, and conservative file-size checks.
- `yt-dlp` — https://github.com/yt-dlp/yt-dlp — the upstream downloader library; consult its current license and documentation.
- `yt-dlp-ejs` documentation — https://github.com/yt-dlp/yt-dlp/wiki/ejs — JavaScript challenge support requirements.
- `cobalt` — https://github.com/imputnet/cobalt — optional API protocol reference. This bundle includes an API client only; it does not bundle Cobalt source code. Configure it only against an instance you operate or are explicitly authorized to use. Do not rely on a public third-party instance without permission.

No upstream repository source file is copied verbatim into this project. Direct repository cloning was unavailable in the build environment, so implementation code was authored independently from public feature descriptions and API docs. Re-check upstream licenses before copying any additional source code.

The bot does not collect user cookies, automate account sign-in, bypass DRM/paywalls, or defeat platform authentication / anti-bot challenges. Use only content you own or have permission to download.
