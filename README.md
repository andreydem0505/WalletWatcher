# Hyperliquid Wallets Monitor

Telegram bot that monitors positions of configured Hyperliquid wallets. Alerts
contain the latest detected fill, current positions, changes since the current
alert started, P&L, and a link to Hyperdash.

![Example of the alert](message.png)

## Setup

The application runs **only in Docker**. Install Docker Engine with Compose
2.24+ on Linux, or Docker Desktop on macOS/Windows. No local Python environment
is required. The image uses Python 3.12, pinned application dependencies, and a
non-root user (UID/GID 10001). Direct host launches of `bot.py` and `supervisor.py`
are rejected before configuration is loaded or network requests are made.

```sh
cp .env.example .env
```

Fill in `.env`:

```dotenv
BOT_TOKEN=123456789:token_from_BotFather
CHAT_IDS=123456789,-1001234567890
ADMIN_ID=123456789
MODE=TEST
```

`CHAT_IDS` contains authorized **chat IDs**, not an independent user allowlist.
Anyone who can write in an authorized group can manage its tracked wallets.
`TEST` sends broadcasts and trade alerts only to `ADMIN_ID`; command replies are
sent to the authorized chat that invoked the command. Use `PROD` to send alerts
to every configured chat. Users must start the bot, or add it to their group,
before it can send them messages.

Wallets can be added with Telegram commands after startup. The optional
`/data/wallets.txt` file inside the container uses one wallet per line:

```text
0x0123456789012345678901234567890123456789;whale
0xabcdefabcdefabcdefabcdefabcdefabcdefabcd;
```

Addresses are normalized to lowercase. Blank lines and comments beginning with
`#` are ignored. Malformed lines are logged and skipped. Wallets, the SQLite
outbox and instance locks are stored in the named volume `walletwatcher_walletwatcher-data`.
The `.env` file stays on the host and is injected at runtime; it is never copied
into the image. Source code is baked into the image, so changes require rebuilding.

### Hyperliquid API compatibility (verified 2026-10-04)

The public mainnet `POST https://api.hyperliquid.xyz/info` endpoint remains current.
Both `clearinghouseState` and `userFills` with `aggregateByTime=true` were checked
against current documentation, the official Python SDK and live responses.
Use the actual master/subaccount/vault address; an agent/API-wallet address can
return empty account data.

| Call or link | Current behaviour |
| --- | --- |
| `clearinghouseState` | Uses explicit `dex`; reads `assetPositions[].position` |
| `userFills` | Returns up to 2000 fills, including original perps, HIP-3 and spot |
| `entryPx` | May be `null`; rendered as `N/A`. Zero-size positions are excluded |
| Hyperdash wallet link | Uses `https://hyperdash.com/address/<wallet>`; `/trader/` redirects there |

`PERP_DEXS=main` monitors the original perpetual market by default, preserving
the project's original scope. To include a HIP-3 market, set e.g.
`PERP_DEXS=main,xyz`. Names can be discovered with the documented `perpDexs` info
request. Each configured dex is queried separately. A partial failed snapshot
is rejected, so API failures cannot falsely close another dex's positions.
Spot is not monitored. Fill selection excludes spot/unconfigured dexes and matches
the assets whose positions changed, including positions that were closed.

The API currently permits 1200 request-weight units per minute per IP:
`clearinghouseState` costs 2; `userFills` costs 20 plus returned-item weight.
The client uses a conservative 1000-unit sliding-minute budget, reserving the
maximum fill-response weight before a request. Other software on the same IP
can still cause 429 responses; the shared cooldown observes `Retry-After`, including
HTTP dates. More wallets/dexes increase polling latency.

Sources: [perpetual account API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals),
[fills API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint),
[request limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits),
[official SDK](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/hyperliquid/info.py).

## Running

After filling in `.env`, start the application:

```sh
docker compose up -d --build
docker compose ps
docker compose logs -f --tail=100
```

Stop gracefully with `docker compose stop`; resume with `docker compose up -d`.
After changing code or `.env`, run `docker compose up -d --build` again to recreate
the container. `docker compose restart` does not reload the environment file.
Missing or invalid credentials produce a clear configuration error (exit 78);
stop the container, correct `.env`, then recreate it.

`docker compose down` removes containers and preserves wallet/outbox data.
**`docker compose down -v` deletes that data permanently.** Do not run multiple
containers or installations with the same Telegram token: polling returns 409
conflicts. Locks prevent simultaneous processes sharing this volume; different
volumes cannot coordinate Telegram polling.

Commands:

- `/addwallet 0x...`
- `/removewallet 0x...`
- `/settag 0x... some tag` (at most 100 characters, without semicolons/newlines)
- Any other text shows wallets, per-wallet last successful update, failure
  counts and the number of queued notifications.

Changes to wallets and tags are written immediately using a temporary file,
`fsync` and atomic replacement. The command does not confirm success before saving.

## Telegram delivery and retry behaviour

All sends and edits, including replies, pass through one SQLite-backed outbox.
Changes to the same wallet/chat replace pending text, so a burst of updates
produces the latest state rather than a growing queue of obsolete edits.

| Setting | Default | Purpose |
| --- | --- | --- |
| `POLL_INTERVAL` | 5 seconds | Minimum interval between successful checks of one wallet |
| `EDIT_INTERVAL` | 15 seconds | Minimum interval between updates of the same alert |
| `CHAT_INTERVAL` | 3.1 seconds | Minimum interval between send/edit requests in one chat |
| `GLOBAL_INTERVAL` | 0.1 seconds | Minimum interval between outbound message requests |
| `WATCHDOG_TIMEOUT` | 180 seconds | Maximum time without a worker/process heartbeat |

These variables are optional. Telegram may still return 429 because its flood
limits vary. The bot persists and observes `parameters.retry_after` plus a
one-second margin before attempting delivery again. Flood waits also pause
polling; they do not count as hung workers. See the [Telegram limits](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this)
and [retry_after documentation](https://core.telegram.org/bots/api#responseparameters).

- Identical text is not edited. Telegram's `message is not modified` response
  is treated as successful delivery.
- A deleted or uneditable alert is replaced with a new message after the chat
  cooldown.
- Timeouts, connection failures and server errors retry with increasing delays,
  capped at five minutes. Failed delivery remains pending even if positions stop changing.
- A blocked or missing chat is retried after one hour and does not stop other chats.
- Invalid tokens and other persistent API errors are logged and retried slowly.
- Pending text, retry deadlines, message IDs and Telegram polling offsets survive
  process restarts in `state.sqlite3`. Changing the configured bot identity clears
  the previous bot's delivery state. Removed wallets/chats are pruned on startup.
- HTML output escapes tags and tickers; long position lists are shortened at
  complete section boundaries to stay within Telegram's message limit.

Do not remove `state.sqlite3` or its WAL files while the bot is running. Back up
the wallet file and SQLite database when migrating an installation.

## Recovery and deployment

Hyperliquid requests have connect/read timeouts of 5/15 seconds. HTTP status,
JSON structure and numerical values are checked. A failed wallet receives its
own retry delay and does not stop the rest of the monitoring cycle.

Monitoring, polling and delivery use separate threads. Unexpected thread exits
are restarted after five seconds. A stalled thread causes the process to exit;
`supervisor.py` replaces it. The supervisor also watches a process heartbeat,
terminates an unresponsive child, and kills it if graceful termination exceeds
ten seconds. Restart delays grow from 2 to 60 seconds; after five minutes of
stable operation the delay resets. A child exits if its supervisor disappears.
Logs go to container stdout/stderr, with Telegram tokens redacted from messages
and tracebacks. Compose rotates Docker logs at 5 MiB with three files.

Docker's `restart: unless-stopped` recovers the container if its supervisor exits
and starts it when the Docker daemon returns, unless it was explicitly stopped.
Enable Docker daemon startup on Linux, or Docker Desktop startup at login on
macOS/Windows. The host must remain awake and Docker must remain running.
The previous direct-Python systemd/launchd templates have been removed. If one
was installed previously, stop and disable it before starting the container.

The container uses an init process, a read-only root filesystem, a writable
persistent `/data` volume and a temporary `/tmp` filesystem. No inbound ports
are needed. A local Docker healthcheck checks heartbeat freshness without
calling external APIs. It allows the watchdog timeout plus 90 seconds for
process replacement/backoff. `unhealthy` is diagnostic: Docker restart policies
react to process exits, not health status; the internal watchdog performs bot
process replacement.

References: [Compose service options](https://docs.docker.com/reference/compose-file/services/),
[Docker restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/).

### Migrating existing data

Stop the old bot first. To transfer its `wallets.txt` and SQLite database, build
the new image and copy the files into the volume using a temporary container:

```sh
docker compose build
docker compose run --rm --no-deps -T --entrypoint python \
  -v "$PWD:/import:ro" walletwatcher - <<'PY'
import shutil
from pathlib import Path
from runtime import InstanceLock

source, target = Path('/import'), Path('/data')
with InstanceLock(target / 'supervisor.lock'), InstanceLock(target / 'bot.lock'):
    if (target / 'wallets.txt').exists() or any(target.glob('state.sqlite3*')):
        raise SystemExit('Destination already contains data')
    files = [source / 'wallets.txt', *source.glob('state.sqlite3*')]
    for path in files:
        if path.is_file():
            shutil.copy2(path, target / path.name)
PY
docker compose up -d
```

Run this migration only into a new, empty volume with both bots stopped. Copy
SQLite WAL files together with the database. Existing host files are not
loaded automatically and remain untouched by this command.

## Tests

```sh
docker build --target test -t walletwatcher:tests .
docker run --rm --network none --read-only --tmpfs /tmp walletwatcher:tests
```

The test runner blocks DNS resolution and outbound socket connections, so an
unexpected network request fails even outside Docker. All API responses and
Telegram deliveries are simulated; persistence tests use temporary SQLite
databases and wallet files. Production dependencies are used without import stubs.

To include line and branch coverage (minimum combined coverage: 90%):

```sh
docker run --rm --network none --read-only --tmpfs /tmp walletwatcher:tests \
  python -B tests/run_tests.py --coverage
```

Coverage data is written to `/tmp`, so the read-only container remains supported.
The coverage package is installed only in the test image. Application code and
runtime dependencies are unchanged by the test tooling.

For development without a running Docker daemon, the same offline suite can be
run in a temporary Python 3.12+ environment. This does not launch the application;
normal bot/supervisor launches still require Docker.

```sh
python3.12 -m venv /tmp/walletwatcher-tests
/tmp/walletwatcher-tests/bin/python -m pip install -r requirements-test.txt
/tmp/walletwatcher-tests/bin/python -B tests/run_tests.py --coverage
```

Behavioral tests cover first-snapshot baselines, send/edit grouping, idle polls,
refreshing all positions after a position change, maximum-leverage crowns,
leverage arrows, notional volumes, copyable wallet lists, Moscow timestamps,
command authorization and atomic saves, retry backoff and shutdown/recovery.
`tests/test_known_limitations.py` separately records the current limitations:
round trips can be missed, only the latest fill is represented, and leverage/tag
changes can update an alert without a new trade. Passing these characterization
tests does not imply every trade is captured; a deliberate behavior fix should
update the corresponding test and the monitoring limits together.

An opt-in live compatibility check uses only public mainnet reads, with no
Telegram/exchange credentials. For example, using a public HLP child vault:

```sh
docker run --rm --read-only --tmpfs /tmp walletwatcher:tests \
  python -B tests/live_hyperliquid.py \
  --wallet 0x010461c14e146ac35fe42271bdc1134ee31c703a --dexs main,xyz
```

The suite injects Telegram flood waits, missing/unchanged messages, network and
disk failures, concurrent updates, and invalid data. It checks persistence and
rate limiting, exercises the installed TeleBot transport without network calls,
and starts real test child processes to verify crash/hang recovery. No live
Telegram token or Hyperliquid account is needed.

## Monitoring limits

The bot detects changes in position size, direction, leverage or entry price;
market-price/P&L changes alone do not trigger alerts. It displays the most recent
fill when a change is detected. It does not replay every trade or guarantee
detection of trades whose net position change is zero between checks.

Position baselines and grouping history are held in memory. After a restart the
first successful check establishes a fresh baseline; previously queued alerts
are still delivered. The displayed delta is measured from the beginning of the
current alert, not from a continuously rolling one-hour snapshot. During long
outages the outbox retains only the latest state for each wallet/chat, with a
10,000-pending-job cap. SQLite failures and queue exhaustion are logged; monitor
enqueues are retried, while a command reply at capacity may require a retry.
Ambiguous network failures can duplicate a send because Telegram does not offer
an idempotency key for `sendMessage`.

Restarting cannot repair invalid credentials, disk failure, or an unavailable
external API. Such conditions remain visible in logs/status and need correction.
