# Discord Bot

A Discord bot that monitors DNS and HTTP endpoints and posts to a channel when a result changes.

## Setup

```sh
uv sync
cp .env.sample .env   # then edit the values
uv run main.py
```

See [.env.sample](.env.sample) for all settings. The bot needs permission to send messages in the alert channel.

## Settings

| Variable | Description |
|---|---|
| `API_KEY` | Discord bot token. |
| `DNS_ALERT_CHANNEL_ID` | Channel ID for alerts. Required for DNS and HTTP monitoring. |
| `DNS_SERVER` | IP address of the DNS server to query. |
| `DNS_CHECK_EXTERNAL_HOST` | External domain to resolve. The legacy `DNS_CHECK_HOST` is accepted as a fallback. |
| `DNS_CHECK_INTERNAL_HOST` | Internal domain to resolve. |
| `HTTP_CHECK_URLS` | Comma-separated URLs to check. Leave empty to disable HTTP monitoring. |

## DNS monitoring

Every minute, the bot sends an A-record query for the external and internal domains to the same `DNS_SERVER`. A query that gets no answer within 10 seconds, or that returns a DNS error, counts as a failure.

## HTTP monitoring

Every minute, the bot sends a GET request to each URL in `HTTP_CHECK_URLS`. A status code of 200-204 is healthy. Any other status (redirects are not followed), a timeout (10 seconds), or a connection error is a failure.

## Notifications

Results are kept in memory only, so they are reset when the bot restarts. The first check after startup is recorded as a baseline and is not reported. After that, a message is posted only when a result changes, such as the returned A records or a switch between success and failure. Repeated failures do not produce repeated messages.
