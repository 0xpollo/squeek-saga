# Squeek Saga

**One story, shared by every Discord server, moved forward by a real hamster.**

Squeek Saga is a community game for [Squeekchain](https://docs.squeekchain.com). Squeektoshi, a real hamster,
runs in a wheel; his work is grouped into 600-second rounds and settled on Ethereum Sepolia. Every hour,
Squeek Saga reads what he really did **on chain** and writes the next chapter of a saga that all servers share:

- **He slept** → a Dreamtide chapter: dreams, memories, hidden doors.
- **He trotted** → an ordinary waking day: travel, small discoveries.
- **He sprinted** → the Wheelwind roars: bold moves and big set-pieces.

Between chapters, members vote with Discord buttons on what the hero does next. Each player has a persistent
character that the storyteller names and remembers. A weekly boss is worn down **only** by the hamster's real
settled work, while players prepare daily to earn glory. A public website keeps the full chronicle, with every
chapter linked to the settlement transaction that triggered it.

Squeektoshi is never pushed to run. Sleep moves the story too, and a boss that is not worn down simply wanders off.

> **Testnet only.** Squeek Saga reads contracts deployed on the Sepolia testnet. Nothing here has monetary value.
>
> **Unofficial community project.** Not affiliated with, endorsed by, or operated by Squeekchain.

<!-- ============================================================================
     SQUEEKCHAIN GENESIS VERIFICATION (README line)
     Replace the placeholder on the next line with the exact line Squeekchain gives you.
     ============================================================================ -->
Genesis verification: `PASTE-THE-VERIFICATION-LINE-FROM-SQUEEKCHAIN-HERE`

---

## How to play

Add the bot from the website's **Add to Discord** button, then:

| Command | What it does |
|---|---|
| `/setup channel:#saga` | Admin: choose the channel where each new chapter is posted |
| `/join role:…` | Create your character (Seedblade, Lanternwright, Mosswhisper, Tunnelrunner, Crumbmage, Storykeeper) |
| `/story` | The latest chapter and its vote buttons |
| `/me` | Your character: glory, vote history and what the saga remembers about you |
| `/prep action:…` | Once a day: get ready for the weekly boss (covers the next 24 hours) |
| `/raid` | This week's boss, damage so far, and how many rounds are still unknown |
| `/leaderboard scope:…` | Glory ranking for this server, or servers ranked globally |
| `/wheel` | What the chain has certified, and when the next chapter can land |
| `/about` | What this is, with the disclaimers |

Voting needs no command: press a button under a chapter. One vote per player per chapter; you can change it until
the next chapter. A tie (or no votes at all) is broken by the hour's settled work, modulo the number of tied options.

## How it uses Squeekchain

The rule of the house: **what decides the story comes from the chain; the HTTP API is only for display.**

### Contract: SqueekCore (Ethereum Sepolia, chain ID 11155111)

The proxy address, expected implementation, runtime hash, storage-layout digest, ProxyAdmin, admin Timelock and
launch configuration are read at start-up from the published deployment record,
<https://docs.squeekchain.com/assets/deployments/sepolia.json>, and the ABI from
<https://docs.squeekchain.com/assets/abi/SqueekCore.json>. No address or ABI is hard-coded in this repository.
If neither the live record nor a copy previously fetched from that URL is readable, the app refuses to start.

Every tick (60 s), everything is read at **one pinned Ethereum block** (`tip − 12`), whose hash is recorded and
re-checked after the reads:

| Read | Used for |
|---|---|
| `eth_chainId` | Must be `11155111` |
| EIP-1967 implementation and admin slots (`eth_getStorageAt`) | Must match the record (every tick) |
| `eth_getCode` of the implementation → keccak256 | Must equal `implementationRuntimeHash` (hourly) |
| `implementationCodeAddress()`, `implementationStorageLayoutDigest()` | Must match the record (hourly) |
| ProxyAdmin `owner()` | Must be the admin Timelock (hourly) |
| `configuration()` | Genesis timestamp, round length, work cap, stream ID must match the record (hourly) |
| `state()` | `settledThroughRound` (the certified frontier), `cumulativeWork`, `cumulativeBlockUnits`, difficulty, `paused` |
| `roundPage(afterRound, throughRound, 16)` | **The source of per-round work.** Paged from our frontier to the chain's, following the returned `certifiedThroughRound` |

Per-round work is the difference between consecutive `cumulativeWork` values in `roundPage`. Each active round is
then re-priced with the docs' *price first, then steer* integer rule, using the **previous** position's difficulty,
and must reproduce the returned `cumulativeBlockUnits` and next difficulty exactly; the replay must land on `state()`.
Any mismatch pauses the saga rather than patching the numbers.

### Events: `BatchSettled` and `RoundRecorded`

Fetched with `eth_getLogs`, filtered by the verified proxy address, for recent blocks only:

- `BatchSettled(digest, reporter, settledThroughRound, nonce, previousSettledThroughRound, activeRoundCount, cumulativeWork, cumulativeBlockUnits)`
  gives each chapter its **settlement transaction**: the batch that certified the hour's last round. Every
  `BatchSettled` advances the frontier, including zero-work batches, and consecutive settlements must chain
  (`previousSettledThroughRound` equals the frontier we hold).
- `RoundRecorded(roundId, work, cumulativeWork, cumulativeBlockUnits, workPerThousandBlocks, dataHash)` is
  cross-checked field by field against what `roundPage` returned for the same round.

### From rounds to chapters

- A **saga hour** is six consecutive rounds, aligned to the genesis timestamp.
- A chapter is written only when all six rounds are **at or below `settledThroughRound`**. Rounds above the
  frontier are *unknown, not zero*: the saga waits and says so. A certified round with no record did zero work.
- Branch: `0` work → slept; `≥ 1400` work units in the hour → sprinted; otherwise trotted. The threshold is the
  median of the hamster's real active hours at launch and is configurable (`SPRINT_WORK_PER_HOUR`).
- **Weekly raid:** a week is 1008 rounds from genesis. Boss hit points are 90% of the work settled over the 1008
  certified rounds before the raid began (a default if fewer are known). Damage is the settled work of the week's
  certified rounds. Uncertified rounds are reported as unknown. Players cannot deal damage; `/prep` only decides
  who earns glory when settled work lands.
- **Reorgs:** each sync stores `(block number, block hash, frontier)`. If a stored hash is no longer canonical, the
  ledger is unwound to the newest read that still is, the rounds are read again, any chapter told from a vanished
  settlement is withdrawn and retold, and raid damage and glory are recomputed from scratch.
- **Integers:** Python integers are arbitrary precision, so `uint64`/`uint96` values are never floats. `uint96`
  totals are stored as decimal text; round ids and work go through an explicit range check before entering a
  signed 64-bit SQLite column. The site's JSON returns large numbers as decimal strings.

### Public API (display only): `https://api.squeekchain.com/v1`

| Endpoint | Used for |
|---|---|
| `GET /v1/status` | The "live, not settled" line on the website and in `/wheel`: device state, rpm, work in the open round |

Bodyless `GET`, no key, at most one request per 5 seconds (well under the 30/s limit), `ETag` sent back in
`If-None-Match`, `Retry-After` honoured on 429/503. A response with `"status": "unavailable"`, a missing
measurement, an error or an unparseable body is shown as **unknown**, never as zero or empty. Nothing from the API
feeds chapters, votes, raid damage or glory.

### What it never does

No private key is requested, stored or derived. The bot signs nothing and sends no transaction. It asks players for
no wallet. It does not read or act on SqueekMining, SqueekStaking, SqueekToken or SqueekNFT.

## Storyteller

One [Claude API](https://platform.claude.com/docs) call per chapter, never more than one chapter per saga hour
(at most 24 calls a day; fewer whenever settlement is behind). Each call has:

- a fixed system prompt (the world, how each branch reads, and the standing rules);
- a rolling summary of the whole saga, plus the previous chapter;
- the hour's branch, the winning vote, the boss state, and up to four players to name, with their memories;
- a JSON schema for the output (title, body, a three-option choice, cameos, updated summary).

Guardrails: all-ages content; the Wheelkeeper is never woken, hurried or blamed; no real names (only sanitised
Discord display names); no money talk. Player names are stripped to plain characters before they reach the prompt
and are marked as data. The output is validated (lengths, exactly three distinct options, no links, mentions or
disallowed words, cameos only for listed players). If the call fails, is declined, or the draft fails a check,
a built-in stock chapter is used. There is no retry loop. Without an API key the saga runs on stock chapters.

## Run it locally

Requires Python 3.12.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env        # optional for a first look: every value has a default
.venv/bin/pytest            # 149 tests, no network needed
set -a; . ./.env; set +a
.venv/bin/python -m squeek_saga
```

Open <http://localhost:8000>. Without `DISCORD_TOKEN` the worker and website still run; without
`ANTHROPIC_API_KEY` chapters use the stock text. The first chapter appears once SqueekCore has settled the next
full hour of rounds (the saga starts at the current frontier and does not backfill).

To create the Discord application: <https://discord.com/developers/applications> → New Application → **Bot** →
copy the token into `DISCORD_TOKEN`; copy the Application ID into `DISCORD_CLIENT_ID`. No privileged intents are
needed. Invite permissions: View Channel, Send Messages, Embed Links, plus the `applications.commands` scope
(the website's button builds this link).

### Tests

`pytest` covers: branch choice from rounds, vote tally and tie-breaks, raid damage and glory, the unknown-data
cases (uncertified rounds, RPC outage, API `unavailable`, docs outage), ledger verification, reorg rollback,
storyteller guardrails and fallback, the website, and the Discord vote buttons.

## Deploy (Docker + Caddy)

```bash
git clone https://github.com/<you>/squeek-saga.git && cd squeek-saga
cp .env.example .env && nano .env       # SITE_DOMAIN, SITE_URL, DISCORD_*, ANTHROPIC_API_KEY, RPC_URL
docker compose --profile proxy up -d --build
docker compose logs -f saga
```

Point the subdomain's DNS `A` record at the VPS first; Caddy then obtains the certificate on its own. If a Caddy
already runs on the host, start only the app (`docker compose up -d --build`) and add the block from
`Caddyfile.host-example` to the host's Caddyfile. State lives in the `saga-data` volume (SQLite).

## Squeekchain Genesis verification

Three places hold the values Squeekchain provides:

1. **Home page `<head>` tag.** Set `GENESIS_META_NAME` and `GENESIS_META_CONTENT` in `.env` and restart. The tag is
   rendered on the home page only. If the tag you are given has another shape, paste it at the marked line in
   `squeek_saga/templates/base.html`.
2. **README line.** Replace the `Genesis verification:` line near the top of this file.
3. **DNS record.**

   <!-- GENESIS DNS RECORD: fill in the record Squeekchain asks for, on the site's (sub)domain. -->
   | Type | Name | Value |
   |---|---|---|
   | `TXT` | `TO-FILL` | `TO-FILL` |

## Project layout

```
squeek_saga/
  deployment.py   published deployment record and ABI (docs.squeekchain.com only)
  chain.py        read-only SqueekCore access: verification, state, roundPage, logs, reorg detection
  ledger.py       local projection: price-then-steer replay, settlement continuity, rollback
  rounds.py       pure round arithmetic: saga hours, branch choice, unknown handling
  votes.py        vote tally
  raid.py         weekly boss: hit points, damage, glory
  story.py        Claude call, fixed prompt, guardrails, stock chapters
  engine.py       the worker that ties it together
  bot.py          Discord slash commands and vote buttons
  web.py          FastAPI chronicle site
  squeek_api.py   display-only client for the public API
tests/
```

## Disclaimer

Sepolia testnet only. Unofficial community project built for Squeekchain Genesis (Code Lane). Not affiliated with
Squeekchain. "Squeekchain", "Squeektoshi" and related names belong to their owners. The saga's world, characters
and text are original to this project. No animal is asked to do anything: the bot only reads public records.
