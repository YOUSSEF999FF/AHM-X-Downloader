# Keep-alive / health-ping workflow (GitHub Actions)

This optional workflow is saved at `.github/workflows/render-keepalive.yml` and checks your Render service's `/health` URL every 10 minutes.

## Setup

1. Push the `.github/workflows/render-keepalive.yml` file to the default branch of your GitHub repository.
2. In GitHub, open **Settings → Secrets and variables → Actions → Variables → New repository variable**.
3. Set the name to `RENDER_HEALTH_URL` and the value to `https://YOUR-SERVICE.onrender.com/health` (replace it with your service's real URL).
4. Open the **Actions** tab and enable workflows if GitHub asks, then run **Render Web Service Health Ping → Run workflow** once to test it.

## Important Render behavior

- The existing `render.yaml` in this project requests the paid `starter` web-service plan. When deployed as that paid plan, Render does not spin down the service for inactivity; the scheduled workflow is optional monitoring and not required to keep it awake.
- Render Free web services normally spin down after 15 minutes without inbound traffic. This workflow sends a public health request every 10 minutes and may help wake/check a Free service, but scheduled GitHub jobs can be delayed or disabled and are **not a guarantee of 24/7 uptime**. Render's Free monthly instance-hour limit still applies; it is not a replacement for an always-on paid plan.
- A health ping cannot repair a startup crash. If Render still shows `Application startup failed`, check the new `FATAL STARTUP ERROR during phase=...` line and the traceback directly below it. Share those lines with secrets removed.
- Never place your Telegram bot token, webhook secret, or any other credential in this workflow or the repository.
