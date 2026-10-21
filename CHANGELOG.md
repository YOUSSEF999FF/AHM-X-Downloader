# Changelog

## 2.0.2 — GitHub-informed compatibility update

- Added `yt-dlp-ejs` as an explicit dependency for YouTube's normal JavaScript challenge solving.
- Configured yt-dlp to use an explicit Node.js binary path; Docker/Render uses Node.js 22+.
- Expanded `/health` with `yt-dlp`, `yt-dlp-ejs`, and Node runtime versions/path so a deployment can be diagnosed without guessing.
- Documented the observed YouTube sign-in/bot-check limitation and linked the upstream yt-dlp EJS and comparison repositories.
- No cookie upload, personal-session storage, or sign-in/bot-check bypass has been added.

This compatibility change may resolve extraction failures caused by a missing JavaScript challenge runtime. It cannot guarantee that YouTube will permit requests from a public hosting provider.
