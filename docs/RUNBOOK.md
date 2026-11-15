# Job Hunter runbook

The operations guide of spec §19.8. This file starts with the production stack (M5, plan Task 31): the deploy
checklist, host setup, the first start, start and stop, the health check, the alert table, secret rotation,
the backups and the offline recovery kit, restore, upgrade and rollback, and the deployment step. The score
lane's entries follow (M3, plan Task 27): a paused score lane and its acknowledgement, a quarantined batch and
its release, a submitted batch that cannot be retrieved, the live schema dry-run before a prompt-schema version
is activated, and the golden eval run on the installation before an activation. M4 adds the web UI's operator
paths (below), which each entry names before its API route, and, for development, the E2E suite (last).

Every command below runs on the host in the compose directory. `docker compose run --rm --no-deps worker
jobhunter …` starts a one-off container of the worker image with the worker's configuration and secrets; it
never starts a second worker loop (§3.3: the worker stays a singleton). The logs carry ids, counts, codes and
amounts, never ad, CV or profile text.

The web UI is the first way to act: the System section of Profile & Settings lists what needs you and offers
each action (below, **The web UI's operator paths**). The read-only queries and the API calls that follow are
for what the UI does not show (an alert's `context`, a log line's ids) and for a host without a browser.

**A read-only query.** As the application role, over the container's local socket (it selects ids, codes,
states, times and counts only, never ad or profile text):

```
docker compose exec postgres psql -U jobhunter_app -d jobhunter -c "<the query>"
```

**An API call from the host.** Every `/api` call needs the logged-in session's cookie; a state-changing one
also an `Origin` header equal to the public origin (`JOBHUNTER_PUBLIC_ORIGIN`, here `$ORIGIN`) and
`X-JH-Request: 1` (§16.5). Log in once with the body `{"username": "…", "password": "…"}` (and `"totp":
"123456"` when TOTP is on) in a file of mode 0600 that you write with an editor, never with the password on a
command line, and delete the file afterwards:

```
curl -sS -c jh.cookies -H "Origin: $ORIGIN" -H 'Content-Type: application/json' \
  --data @login.json "$ORIGIN/api/auth/login"
rm login.json
```

Then pass `-b jh.cookies -H "Origin: $ORIGIN" -H 'X-JH-Request: 1'` to each call below, and remove
`jh.cookies` when done (`POST /api/auth/logout` ends the session).

## The production stack

Sections 1 to 13 deploy, run, back up, restore, rotate and upgrade the production stack of `deploy/compose.yaml`
on the VPS (M5). Each command in them runs in a root shell on the VPS (`sudo -i`), in the checkout of the
release tag at `/opt/jobhunter`, and each compose command is `$JH …`, Task 21's production command line. It is
set once in each root shell, once section 2 has cloned the checkout (a shell variable ends with its shell, after
a reboot for one; root's shell profile can hold the two lines):

```
cd /opt/jobhunter
JH="docker compose -p jobhunter --env-file deploy/images.lock --env-file deploy/release.lock \
  --env-file /etc/jobhunter/jobhunter.env -f deploy/compose.yaml"
```

Compose without the two lock files reads no image, and `${app_image:?}` stops it. A one-off `$JH run` while the
stack runs takes `--no-deps`: without it Compose starts the service's dependencies again, `migrate` among them,
which runs only while api and worker are stopped (section 10). "Section n" below is one of these sections, "§"
a section of the design specification. Placeholders are in angle brackets (`<domain>`, `<dir>`). No secret's
value is ever typed on a command line, printed or pasted, with one exception: `user totp enable` prints the
otpauth URI, which holds the TOTP secret, for the authenticator app to read (section 11); clear the terminal and
its scrollback afterwards. The sections after section 13, from **The web UI's operator paths** on, are M3's and
M4's, written with the bare compose commands of the introduction above. On the VPS each of those runs as `$JH`
with the same arguments: in `/opt/jobhunter` a bare docker compose finds no compose file, the files being in
`deploy/`.

## 1. Deploy checklist

Every input of the deployment and where it goes. Gather them before the deployment step (section 13), and
record in the deployment record which are set, never their values. A value lives in one of three places: a
secret file in `<dir>`, a directory of mode 0700 on the VPS that host setup installs from and then empties
(section 2); the non-secret environment file `/etc/jobhunter/jobhunter.env` (below); or an account outside the
VPS. No secret goes into this repository, an issue or a chat.

| Input | Where it goes |
|---|---|
| The ntfy server, a private topic on it, and a token when the server requires one | `NTFY_TOPIC_URL` (`https://<ntfy server>/<topic>`); the secret `ntfy_token` (without a token, none: host setup installs an empty file) |
| healthchecks: one check for the collection schedule, its UUID ping URL and a read-write API key | the secrets `healthchecks_ping_url` (the https ping URL, ending in the check's UUID) and `healthchecks_api_key` (a read-write Management API key: the worker sets the check's period, §17.7); `HEALTHCHECKS_API_URL` for a self-hosted server only (empty: healthchecks.io's v3 API) |
| healthchecks' own alert integration (a mailbox, an app or a phone that healthchecks itself notifies) | the healthchecks account: the only path that alerts you when the VPS is dead. It is tested once (section 13, item 10) |
| LangSmith: an EU workspace, a service key, the project `jobhunter-prod`, its trace retention and quota | the secret `langsmith_key`; the project and the EU endpoint are fixed in `deploy/compose.yaml`; the trace retention and the quota are set in the workspace |
| The Anthropic API key of an Anthropic workspace this installation alone uses (C3) | the secret `anthropic_key` |
| The JSearch (RapidAPI), SerpApi and Adzuna keys; the SerpApi account is used by this app only (C2) | the secrets `rapidapi_key`, `serpapi_key`, `adzuna_app_id` and `adzuna_app_key` |
| The VPS: provider, EU region, 4 vCPU (AVX-512 VNNI preferred), 16 GB, ≥ 100 GB NVMe, OS image, LUKS where offered | the provider's order; the provider also goes into the operator file (below); no swap, or encrypted swap only (section 2) |
| Docker Engine: one pinned version, no older than the one N1's DNS assertion passed on | the host's package manager, holding that version; both versions go into the deployment record |
| Domain and DNS: an A record only, no AAAA (the networks run without IPv6, Deviation 36) | `JOBHUNTER_DOMAIN` (`<domain>`) and `JOBHUNTER_PUBLIC_ORIGIN` (`https://<domain>`); the A record names the VPS before the first start (ACME) |
| The ACME e-mail: an operator mailbox | `JOBHUNTER_ACME_EMAIL` (`<operator mailbox>`) |
| The offsite S3 provider (another than the VPS's), a bucket and its keys | `RESTIC_REPOSITORY` (`s3:<endpoint>/<bucket>/<path>`), `AWS_DEFAULT_REGION`, the secrets `s3_access_key_id` and `s3_secret_access_key`. The bucket has bucket versioning on, with a noncurrent-version expiry of 30 days, long enough for the backup alerts or the monthly restore test to reveal a destroyed repository while its deleted versions still exist (each day longer keeps purged data offsite longer), and no Object Lock in compliance mode, which would keep deleted data past every purge (R33) |
| SSH: key-only | the VPS's sshd, password login off; no Claude session gets access to the VPS, its keys or a shell on it |
| GHCR: the release's images | run `release.yml`, merge its digest PR and check out the release tag (section 10); the VPS logs in to `ghcr.io` with a read-only token (section 2) |
| The place of the offline recovery kit | off the VPS and offline, named in the deployment record (section 8) |
| The embedder's export: fp32 (the one locked) or int8 | V7 on the VPS's CPU decides (section 13, item 9); int8 means a new `embedder/model.lock` and a new release (section 12) |
| The privacy statement's five operator fields: operator name, contact address, controller name, VPS provider and S3 backup provider | the operator file `/etc/jobhunter/operator.json` on the VPS (section 2, item 3), never the repository: a JSON object of exactly the keys `operator`, `contact`, `controller`, `vps_provider` and `s3_provider`, each a string with a visible character (not blank), of at most 200 characters, without control characters, and no longer the template's `<…>` placeholder. Compose mounts it read-only into the api only; the statement shows the values in place of the markers `docs/PRIVACY.md` keeps. The api does not start without a valid file (startup check `operator_file`, which names the problem, never a value), and neither do the api's one-off commands. The offline recovery kit keeps a copy (section 8). A changed file applies after a restart: check it first with `$JH run --rm --no-deps api jobhunter startup-check --role api`, then `$JH restart api` (section 4) |
| The exclusivity confirmations: the SerpApi account is used by this app only (C2), the Anthropic workspace by this installation only (C3) | the deployment record; a foreign use shows as `quota.serpapi_foreign_usage`, or as spend the app did not count |

**The environment file** `/etc/jobhunter/jobhunter.env`, root's, mode 0640, written with an editor. It holds no
secret, and the offline recovery kit keeps a copy (section 8):

```
JOBHUNTER_ENV=prod
JOBHUNTER_DOMAIN=<domain>
JOBHUNTER_ACME_EMAIL=<operator mailbox>
JOBHUNTER_PUBLIC_ORIGIN=https://<domain>
NTFY_TOPIC_URL=https://<ntfy server>/<topic>
HEALTHCHECKS_API_URL=
POSTGRES_DB=jobhunter
POSTGRES_USER=jobhunter_bootstrap
JOBHUNTER_DB_HOST=postgres
JOBHUNTER_DB_PORT=5432
JOBHUNTER_DB_NAME=jobhunter
JOBHUNTER_EMBEDDER_URL=http://embedder:8080
JOBHUNTER_HTTP_USER_AGENT=
JOBHUNTER_LOG_LEVEL=INFO
RESTIC_REPOSITORY=s3:<endpoint>/<bucket>/<path>
AWS_DEFAULT_REGION=<region>
JOBHUNTER_BACKUP_CRON=
JOBHUNTER_MANIFEST_SHIP_CRON=
JOBHUNTER_BACKUP_PRUNE_CRON=
JOBHUNTER_RESTORE_TEST_CRON=
JOBHUNTER_BACKUP_KEEP_DAILY=
JOBHUNTER_BACKUP_KEEP_WEEKLY=
JOBHUNTER_BACKUP_KEEP_MONTHLY=
```

An empty value takes the default: healthchecks.io's API, the app's own User-Agent, and the backup schedule and
counts of section 8. `JOBHUNTER_SECRETS_DIR` stays unset, so the secrets are read from
`/etc/jobhunter/secrets`. The LangSmith settings are fixed in `deploy/compose.yaml` (tracing on, the EU
endpoint, inputs and outputs hidden), and the release's image digests are `deploy/release.lock`'s.

**The secret files** in `<dir>`, one file per name of `deploy/secrets.example`, which also names the services
that read each:

| Secret | Its value |
|---|---|
| `session_secret`, `data_key`, `restic_password` | made on the VPS, 32 random bytes in base64 (section 2); `data_key` and `restic_password` go into the offline recovery kit first (section 8) |
| `pg_bootstrap`, `db_app`, `db_migrator`, `db_backup`, `db_restore` | made on the VPS, 24 random bytes in hex (section 2) |
| `data_key_previous` | none outside a data-key rotation: host setup installs an empty file (section 7) |
| `anthropic_key`, `rapidapi_key`, `serpapi_key`, `adzuna_app_id`, `adzuna_app_key`, `langsmith_key` | the providers' keys (above) |
| `ntfy_token` | the ntfy token, or none |
| `healthchecks_ping_url`, `healthchecks_api_key` | the check's ping URL and the API key (above) |
| `s3_access_key_id`, `s3_secret_access_key` | the bucket's keys (above); they go into the offline recovery kit too |

## 2. Host setup

Once before the first start: on the VPS of the deployment step, and on the fresh host of a restore (section 9).

1. **The swap.** `swapon --show` lists nothing, or encrypted swap only: the `parse_jobs` tmpfs must never page
   a decrypted CV to a plain disk (§19.8 step 5). `scripts/host-setup.sh` stops on any unencrypted swap device,
   zram included. On a host whose image ships zram swap, turn it off before host setup: `swapoff` its device
   (`swapoff /dev/zram0`) and disable the provider's zram service (`systemctl disable --now <zram service>`) so
   that it does not come back at boot; `swapon --show` after a reboot lists nothing.
2. **The checkout and the registry.** Install Docker Engine at its pinned version (section 1), and the
   package `nftables` (`apt-get install nftables`): its `nft` loads the egress rule at every boot, and host
   setup refuses to start without it. Clone the repository to `/opt/jobhunter`, check out the release tag
   (section 10), and log in to GHCR with the read-only token, read from a file that is deleted afterwards
   (Docker keeps the login in root's `~/.docker/config.json`):

   ```
   git clone <repository URL> /opt/jobhunter
   git -C /opt/jobhunter checkout <release tag>
   docker login ghcr.io --username <GitHub user> --password-stdin < <token file>
   rm <token file>
   ```

   Then set `JH` (the introduction).

3. **The environment file and the operator file.** `install -d -m 0755 /etc/jobhunter`, then write
   `/etc/jobhunter/jobhunter.env` (section 1) with an editor and `chmod 0640 /etc/jobhunter/jobhunter.env`.
   Then write the operator file `/etc/jobhunter/operator.json` with an editor, the privacy statement's five
   values (section 1) in place of the placeholders:

   ```
   {
     "operator": "<operator name>",
     "contact": "<contact address>",
     "controller": "<controller name>",
     "vps_provider": "<VPS provider>",
     "s3_provider": "<S3 backup provider>"
   }
   ```

   and `chmod 0644 /etc/jobhunter/operator.json`: root's, readable by the api's user. It holds no secret, but
   personal details: it never goes into the repository. Host setup's startup checks (item 5) need it: the
   api's refuses a missing or invalid file, and compose does not create the api's container without it. Then
   `$JH pull` fetches the release's images.
4. **The secret files.** In a new directory `<dir>`, root's, mode 0700, make the generated values, then write
   each provider key into its own file with an editor (never `echo`, never a command line). On the VPS of a
   first deployment:

   ```
   install -d -m 0700 <dir>
   umask 077
   for name in session_secret data_key restic_password; do openssl rand -base64 32 > "<dir>/$name"; done
   for name in pg_bootstrap db_app db_migrator db_backup db_restore; do openssl rand -hex 24 > "<dir>/$name"; done
   ```

   Then, on a first deployment only, copy `data_key` and `restic_password` into the offline recovery kit, the
   data key labelled with its key id (section 8). On the fresh host of a restore (section 9), `data_key` and
   `restic_password` are not made anew: copy the kit's `data_key`, `restic_password`, `s3_access_key_id` and
   `s3_secret_access_key` into `<dir>`, and make the other generated values only:

   ```
   install -d -m 0700 <dir>
   umask 077
   openssl rand -base64 32 > "<dir>/session_secret"
   for name in pg_bootstrap db_app db_migrator db_backup db_restore; do openssl rand -hex 24 > "<dir>/$name"; done
   ```
5. **Host setup**, by `scripts/host-setup.sh` (R22): a dry run first, which prints each command and runs none,
   then the real run:

   ```
   sudo scripts/host-setup.sh --secrets-from <dir> --dry-run
   sudo scripts/host-setup.sh --secrets-from <dir> --delete-sources
   ```

   Its preflight changes nothing and stops at the first failure: it runs as root from `/opt/jobhunter`;
   `deploy/release.lock` and `/etc/jobhunter/jobhunter.env` exist; the operator file
   `/etc/jobhunter/operator.json` exists (item 3; a dry run only names it when it is missing); `nft` is
   installed (the package `nftables`, item 2), and so is `swapon` (the package `mount`); `<dir>` holds every
   name of `deploy/secrets.example` but the optional `data_key_previous` and `ntfy_token`; gid 10010 is free
   or the group `jobhunter-secrets` without members, and no host user has it as its primary group; the swap
   check passes. Then it runs the steps below, each command printed first: the group, the directory, the
   files, the egress rule's unit, the startup assertions of api, worker, migrate, backup and restore, and,
   with `--delete-sources`, the deletion of the source files once every step has passed. Remove the empty
   `<dir>` afterwards (`rmdir <dir>`).
6. **The manual equivalent**, §19.8's steps 1 to 6, for a host where the script cannot run:
   1. `groupadd --gid 10010 jobhunter-secrets`; no user is added to it.
   2. `install -d -o root -g 10010 -m 0750 /etc/jobhunter/secrets`.
   3. For each name of `deploy/secrets.example`: `install -o root -g 10010 -m 0440 <dir>/<name>
      /etc/jobhunter/secrets/<name>`, then delete `<dir>/<name>`; for a `data_key_previous` or `ntfy_token`
      without a value, `install -o root -g 10010 -m 0440 /dev/null /etc/jobhunter/secrets/<name>`. Never
      `chmod 0400`: the app services run as uid 10001 and read their secrets through group 10010.
   4. Each role's startup assertions (§16.8), in a one-off container of its own, with no service started; each
      exits 0, or 78 naming the check that failed (never a value):

      ```
      $JH run --rm --no-deps api jobhunter startup-check --role api
      $JH run --rm --no-deps worker jobhunter startup-check --role worker
      $JH run --rm --no-deps migrate jobhunter startup-check --role migrate
      $JH run --rm --no-deps backup jobhunter startup-check --role backup
      $JH run --rm --no-deps restore jobhunter startup-check --role restore
      ```

   5. No swap, or encrypted swap only (item 1).
   6. The egress rule (§16.9): its unit loads `deploy/firewall/docker-user.nft` at boot, before Docker:

      ```
      install -o root -g root -m 0644 deploy/firewall/jobhunter-egress.service /etc/systemd/system/jobhunter-egress.service
      systemctl daemon-reload
      systemctl enable --now jobhunter-egress.service
      ```

7. **After every boot, and after any firewall change**, `nft list table inet jobhunter_egress` lists the egress
   rule's table (section 5).

## 3. First start

Once, after host setup, in this order (section 13, items 5 and 6):

1. The restic repository in the S3 bucket, explicitly and once (no job creates it):

   ```
   $JH run --rm backup jobhunter backup init
   ```

   It prints `{"created": true}`, or `{"created": false}` for a repository that exists, which it leaves as it
   is. A database that was not restored from a repository gets a new, empty one: never point
   `RESTIC_REPOSITORY` at the bucket path of an earlier lifetime of the deployment, whose `manifests` snapshots
   a later restore would replay into the new database (its deletions by id). On a first deployment, and on a
   deployment started again from nothing, `backup init` must print `{"created": true}`; on the fresh host of a
   restore (section 9) it is the existing repository the restore reads.
2. Caddy against Let's Encrypt's staging CA first, so that a mistake does not spend the production rate limit:
   add the line `acme_ca https://acme-staging-v02.api.letsencrypt.org/directory` to the global options block of
   `deploy/Caddyfile` (its first `{ … }`), then start the stack and read Caddy's log:

   ```
   $JH up -d --wait
   $JH logs caddy
   ```

   `up -d` starts postgres, whose first start creates the roles from the secret files (§19.3), then `migrate`,
   before api and worker start, then the rest. The log shows the certificate obtained from the staging CA.
   Then production:

   ```
   git checkout deploy/Caddyfile
   $JH restart caddy
   ```

3. The user: `$JH run --rm --no-deps api jobhunter user create --username <name>`. It asks for the password
   twice, without echo; `--password-stdin` is refused in prod (`cli_user.py`).
4. `/readyz` from outside, from another machine: `curl -sS https://<domain>/readyz` answers `{}` (200). A 503
   lists what fails: `db`, `migrations`, `worker_heartbeat` or `embedder`.
5. V15, the push channel and the dead-man check:

   ```
   $JH run --rm --no-deps worker jobhunter ops notify-test
   $JH run --rm --no-deps worker jobhunter ops deadman-sync --show
   ```

   The first prints `{"sent": true}`, and the test notification arrives on the phone (else `{"sent": false,
   "error": "<code>"}`). The second prints the check's period as healthchecks reads it back, `{"timeout_s",
   "grace_s", "paused", "source"}`. Neither prints a URL, a uuid or a key.
6. V20, V21 and V7: section 13, items 7 to 9.

## 4. Start and stop, and a frozen worker

- **Start:** `$JH up -d --wait`. **Stop:** `$JH stop`, graceful: the worker drains within its 120 s, and a run
  in flight resumes as a new attempt at the next start; the backup service lets a running job end, which can
  take up to 10 minutes (section 8). Never `--scale worker=…`: the worker is a singleton
  (§3.3, `replicas: 1`).
- **A frozen worker.** With one replica, a paused or swap-storming worker is never replaced: Docker restarts a
  container that exits, not one that hangs or turns unhealthy. Restart it:

  ```
  $JH restart worker
  ```

  The new worker takes the singleton lock and a new epoch, and the takeover does the rest: a run in flight
  resumes as a new attempt. The dead-man switch is what pages for a frozen worker (M0 Carry-forward 3): no run
  pings healthchecks, whose own alert reaches you even when the VPS is dead. In the app, `worker.heartbeat_stale`
  and `/readyz`'s `worker_heartbeat` show it.
- **A changed environment file.** Compose applies a change of `/etc/jobhunter/jobhunter.env` only through `up`,
  which recreates each service whose environment changed, and which also starts `migrate` again. So stop api
  and worker first, then edit the file and start the stack:

  ```
  $JH stop api worker
  $JH up -d --wait
  ```

  The offline recovery kit's copy of the file changes too (section 8).
- **A changed operator file.** The api reads `/etc/jobhunter/operator.json` once, when it starts. After an edit,
  run the api's startup check on the file in a one-off container first: a file it refuses would stop the
  restarted api (exit 78), again at each of Docker's restarts. It exits 0, or 78 naming the problem, never a
  value; then restart the api:

  ```
  $JH run --rm --no-deps api jobhunter startup-check --role api
  $JH restart api
  ```

  The offline recovery kit's copy of the file changes too (section 8).

## 5. Health check

1. From outside: `curl -sS https://<domain>/readyz` answers `{}` (200); a 503 names what fails (`db`,
   `migrations`, `worker_heartbeat`, `embedder`).
2. In the UI: Profile & Settings → System → Health shows the run and backup ages, the queues, the quota windows,
   the spend, the disk and table sizes, the embedder and the LangSmith state (`GET /api/system/health`).
3. On the VPS: `$JH ps -a` (each service running, those with a healthcheck `healthy`, `migrate` exited 0;
   without `-a`, `ps` lists the running containers only) and
   `$JH logs worker --since 1h` (the logs hold ids, codes and counts, never personal data).
4. After every boot, and after any change to the host's firewall (a rule file installed, an `nft flush
   ruleset`, a firewall front end's reload or restart): `nft list table inet jobhunter_egress` lists the egress
   rule's table. That is the check. `systemctl status jobhunter-egress` is no evidence: the unit stays `active
   (exited)` after a flush has deleted the table. The unit is ordered before Docker and fails open, so a rule
   file that does not load, or a flush, stops nothing, and only this check shows it: fix the file, or load the
   rule again after a flush, with `systemctl restart jobhunter-egress`, then check again.

## 6. Alerts

Every alert of the catalogue (§17.6, `observability/catalogue.py`) and what to do. `info` alerts show in the
app only; `warn` ones go to ntfy too, at priority 3, and `critical` ones at priority 5, again every 6 h (warn)
or every hour (critical) while they are unacknowledged; `notify.delivery_failed` never goes through ntfy. An
`auto` alert resolves itself once its condition clears. A `manual` one is resolved by you, once its cause is
dealt with: System → Alerts → **[Resolve]** (`POST /api/alerts/{id}/resolve`). **[Acknowledge]** records that
you saw an alert and resolves none but `llm.reservation_bound_violated`. An alert's `context` (ids and codes,
never content) is in System → Alerts, and in a read-only query (the introduction).

| Alert | Severity | Resolves | Action |
|---|---|---|---|
| `run.failed` | critical | auto | System → Runs → the run's **[Report]** gives its status reason and causes, `$JH logs worker --since 1h` its error class. Fix the cause (a key: `source.auth_failed`; the network; the disk: section 11); the next succeeded run resolves it. |
| `run.failed_invariant` | critical | manual | Read the run report's invariant section; report a bug with the run's id; the next run proceeds. Then resolve it. |
| `run.partial` | warn | auto | One alert per partial cause, which the run report names: fix that source, quota or key. A run without the cause resolves it. |
| `run.resume_exhausted` | critical | manual | The run was resumed more than 3 times and failed: a worker that keeps crashing or restarting. `$JH ps` and `$JH logs worker` show why (memory, a crash); fix it, then resolve it. The next scheduled run starts afresh. |
| `run.orphaned_queue` | warn | auto | A queued run lost its queue job (a worker restart) and was failed. Nothing to do unless it repeats (then as `worker.heartbeat_stale`); the next success resolves it. |
| `schedule.stale_data` | critical | auto | No successful run for twice the schedule's interval. Check the worker (`$JH ps`, section 4), System → Runs and Needs attention → Broken sources; the dead-man switch alarms for the same cause. A successful run resolves it. |
| `worker.heartbeat_stale` | critical | auto | The worker's heartbeat is older than 60 s: `$JH ps worker` and `$JH logs worker --since 1h`. A frozen worker takes `$JH restart worker` (section 4). A fresh heartbeat resolves it. |
| `embedder.unavailable` | warn | auto | An embedding failed in a run: `$JH ps embedder` and `$JH logs embedder` (memory, a failed start). When the log shows `embedder.checksum_mismatch`, or the container keeps exiting with code 78, follow `embedder.checksum_mismatch` (next row). Otherwise it restarts by itself; the alert resolves once the embedder is healthy and the backlog is embedded. |
| `embedder.checksum_mismatch` | critical | auto | The embedder's model files differ from `embedder/model.lock`. This alert is best effort: the real embedder exits 78 at a mismatch and restarts in a loop, so a mismatch shows mostly as `embedder.unavailable`, the readiness check `embedder` and the dead-man alarm, with `embedder.checksum_mismatch` in `$JH logs embedder`. The image is pinned by digest, so its local copy is damaged or the release is wrong: `$JH rm -sf embedder`, `docker image rm <embedder_image of deploy/release.lock>`, `$JH pull embedder`, `$JH up -d --no-deps --wait embedder`. If it stays, report it and roll back (section 10). A checked start resolves it. |
| `source.breaker_open` | warn | auto | A source failed repeatedly and its breaker paused it; Needs attention → Broken sources shows its last error. It tries again by itself and resolves on a success; act on an error that lasts (a key: `source.auth_failed`). |
| `source.auth_failed` | critical | auto | Rotate or renew the source's key in its secret file (section 7: `rapidapi_key`, `serpapi_key`, `adzuna_app_id`, `adzuna_app_key`), then `$JH restart worker`: the worker reads its secrets at its start (R6). A successful call resolves it. |
| `source.contract_broken` | critical | auto | The source's responses no longer fit the envelope its adapter expects: the provider changed its API. Report it with the source and the run; Needs attention → Rejected raw items keeps the responses, to **[Replay]** once a fixed release is deployed (section 10). A valid response resolves it. |
| `source.not_found` | warn | auto | An ATS board answers 404: the company moved or closed its board. Correct or remove the company in Settings → Watchlist; a success or the removal resolves it. |
| `ats.mass_disappearance` | warn | auto | More than half of a board's postings vanished at once (§8.11). Needs attention → Mass disappearances → **[Confirm closure]** when they are gone; otherwise it resolves when the absent share falls to 50 % or below. |
| `quota.unaccounted_usage` | warn | manual | The provider reports less remaining than the app counted. Check whether the key is used elsewhere; rotate it if needed (section 7). Then resolve it. |
| `quota.serpapi_foreign_usage` | warn | auto | The SerpApi account must be used by this app only (§6.5, C2). Stop the other user or rotate the key (section 7); the conservative mode ends by itself after 24 h without detection. |
| `quota.exhausted` | warn | auto | JSearch reports its quota exhausted; the window's renewal resolves it. A plan that runs out every month needs a larger plan, and its limit in Settings → Queries & sources → Quota limits. |
| `quota.limit_increased` | info | manual | A provider reports a higher limit than configured: if the plan changed, set it in Settings → Queries & sources → Quota limits. Then resolve it. |
| `quota.fallback_active` | info | auto | SerpApi served cells in a run (the fallback). Information; a run without the fallback resolves it. |
| `quota.ambiguous_attempt` | warn | manual | A paid attempt's outcome is unknown, so it counts as spent. Compare with the provider's usage when it repeats; then resolve it. |
| `quota.limit_breach` | critical | manual | The counted units exceed the provider's limit. Compare the provider's dashboard with System → Health's quota windows and the limit in Settings → Queries & sources → Quota limits, and look for another user of the key (C2); then resolve it. |
| `coverage.too_broad` | info | auto | A search cell stays incomplete for 3 runs or more: its query finds more than a run fetches. Narrow it in Settings → Queries & sources; it resolves once the cell is covered. |
| `coverage.gap_abandoned` | warn | manual | A coverage gap older than the oldest ad age kept was given up: ads in it may be missing. Information; resolve it. |
| `llm.cap_80pct` | warn | auto | The month's counted LLM spend reached 80 % of the cap. Raise it if needed (Settings → Budget & quota → Monthly LLM cap); a new month or a higher cap resolves it. |
| `llm.cap_reached` | critical | auto | Scoring is paused: **A paused score lane** (`cap_reached`). It lifts when headroom returns. |
| `llm.budget_counter_drift` | critical | auto | The reconciler has corrected the counters from the ledger; report a bug with the alert's context. |
| `llm.reservation_bound_violated` | critical | manual, by its acknowledgement | Scoring is paused. Check `llm/pricing.py` and the overhead constant against the call's usage; fix, deploy, then acknowledge (**A paused score lane**). |
| `llm.auth_failed` | critical | auto | Scoring is paused. Rotate or renew `anthropic_key` (section 7), then `$JH restart worker` (R6), then acknowledge the alert (**A paused score lane**); a successful call resolves it. |
| `llm.model_mismatch` | critical | manual | Anthropic served another model than the one requested. Check the active model policy against Anthropic's model notices and report it; then resolve it. |
| `llm.cache_miss_streak` | warn | auto | Prompt caching keeps missing (§10.8), which raises the cost. Report it if it lasts; a cache read resolves it. |
| `llm.refusal_rate` | warn | auto | More than 5 % of the calls of 24 h were refused; Needs attention → Refused and failed scores lists them. Report it; it resolves below the threshold. |
| `scoring.stuck_target_repaired` | warn | auto | The reconciler reclaimed stuck targets this hour (a cycle that died). Nothing to do unless it recurs every hour (then as `worker.heartbeat_stale`). |
| `scoring.publication_invalid` | critical | manual | A score's publication failed the strict model: a bug. Report it with the alert's context; once a fixed release is deployed, **[Retry]** the target in Needs attention → Refused and failed scores; then resolve it. |
| `scoring.critic_rate_high` | warn | auto | The critic runs more often than the evaluation policy projects, which raises the cost. Review the policy; it resolves below the threshold. |
| `score_cycle.orphaned` | warn | auto | A score cycle was failed as orphaned (a worker restart during it). Nothing to do unless it repeats; the next completed cycle resolves it. |
| `score_cycle.failed` | critical | auto | A score cycle failed with an unclassified error: a bug. `$JH logs worker --since 1h` gives its error class; report it. The next completed cycle resolves it. |
| `operation.failed` | warn | manual | A durable operation failed for good; its kind and error are in the alert's context. Fix the cause and repeat the action in the UI; then resolve it. |
| `scoring.canary_drift` | warn | auto | A canary scored more than 8 points from its first score (§11.13): the model's behaviour changed. Run a golden eval (**The golden eval run on the installation before an activation**); the next canary within bounds resolves it. |
| `scoring.feedback_disagreement` | warn | auto | Your labels disagree with the scores beyond the threshold (§11.13). Review the rubric and run a golden eval; it resolves below the threshold. |
| `scoring.distribution_shift` | info | auto | The scores' distribution moved (§11.13). Information. |
| `admission.exploration_hits` | warn | auto | Ads admitted for exploration score well although their similarity is low (§9.6.5): the admission is too tight. Review the profile and the queries; it resolves below the threshold. |
| `admission.high_sim_expired` | warn | auto | Ads of high similarity expired before they were scored (§9.6.5): scoring lags. Check the cap and the score lane; it resolves after 24 h without recurrence. |
| `admission.backlog_drain` | info | auto | The pending targets would take long to drain at the current pace (§9.6.5). Information. |
| `batch.submit_ambiguous_unresolved` | critical | auto | Inspect the Anthropic console. Release only when certain that no batch exists; the reserved spend stays counted as ambiguous either way (§10.10; **A quarantined batch and its release**). |
| `batch.results_unreadable` | critical | manual | A batch's result stream could not be read; its remaining items took the missing-item path (§10.10). Report it with the alert's context; then resolve it. |
| `batch.item_unprocessable` | critical | manual | One batch item failed in 12 passes and was given up as missing (§10.10). Report it with the alert's context; then resolve it. |
| `batch.retrieve_failing` | critical | auto | **A submitted batch that cannot be retrieved**. |
| `scope.apply_failed` | critical | auto | The old scope is still active. Read the operation's error, fix the cause, then Needs attention → Failed scope apply → **[Retry apply]**. |
| `batch.custom_id_anomaly` | critical | manual | A batch shares only part of a submission's ids and is never adopted: report it as a bug before any release (**A quarantined batch and its release**); then resolve it. |
| `batch.items_missing` | critical | manual | A batch ended without a result for some items. Report it with the alert's context; then resolve it. |
| `dedup.identity_conflict` | info | auto | Needs attention → Identity conflicts → **[Keep apart]** or **[Merge]**. |
| `dedup.field_conflict` | info | auto | Postings of one job disagree on a structured field. Information; it resolves when they agree. |
| `auth.lockout` | critical | auto | The global login lockout: failed logins from many addresses. It ends by itself; `$JH run --rm --no-deps api jobhunter sessions unlock` lifts it at once. |
| `backup.failed` | critical | auto | `$JH logs backup --since 24h` and the set's error (System → Health) name the step; check the bucket, its keys and `RESTIC_REPOSITORY`, then `$JH exec backup jobhunter backup set` (section 8). The next succeeded set resolves it. |
| `backup.stale` | warn / critical | auto | No succeeded set for 36 h (warn) or 72 h (critical): check that the backup container runs (`$JH ps backup`), then as `backup.failed`. |
| `backup.restore_test_failed` | critical | auto | The monthly restore test failed: `$JH logs backup` names its step. Fix it, then `$JH exec backup jobhunter backup restore-test`; its success resolves it. A run that failed `cleanup:<error class>` passed every step but could not drop its scratch database, a full copy of the data (section 8): the next restore test drops it first, and so does `$JH restart backup`. |
| `system.disk_pressure` | warn / critical | auto | The data volumes' disk is 80 % full (critical from 90 %): section 11. |
| `system.table_growth` | info | auto | `procrastinate_jobs` holds more than a million rows, or a table more than 10 GB: section 11. |
| `deadman.update_failed` | warn | auto | The healthchecks Management API missed the period's update. Check `healthchecks_api_key` (read-write) and `HEALTHCHECKS_API_URL` with `$JH run --rm --no-deps worker jobhunter ops deadman-sync --show`; a renewed key takes `$JH restart worker` (section 7). The sync tries again with a backoff; a success resolves it. |
| `notify.delivery_failed` | warn | auto | ntfy failed 3 deliveries; this alert shows in the app only. `$JH run --rm --no-deps worker jobhunter ops notify-test` prints the error code; check the server, `NTFY_TOPIC_URL` and `ntfy_token` (section 7). A delivery resolves it. |
| `eval.gate_failed` | warn | manual | A candidate failed the golden-set gate (**The golden eval run on the installation before an activation**). Never override it without reading which gate failed; then resolve it. |
| `privacy.purge_failed` | critical | auto | Fenced work stays blocked until the purge completes. Read the operation's error, fix the cause, then System → Needs attention → Failed purges → **[Retry]** (`POST /api/system/purges/{id}/retry`). |
| `privacy.manifest_unshipped` | critical | auto | Check the backup container (`$JH ps backup`, `$JH logs backup`) and the S3 keys, then ship by hand: `$JH exec backup jobhunter backup ship`. The deletion is reported done only once shipped. It also opens after a prune removed a false receipt (`manifest_receipt_removed` in `$JH logs backup`, section 8): the manifests that receipt shipped are not offsite, the next shipment uploads the whole stream, and the alert resolves once they are marked shipped again. |
| `llm.unexpected_stop_reason` | warn | manual | A call ended with a stop reason the gateway does not expect, and failed. Report it if it repeats; then resolve it. |
| `profile.extract_generation_mismatch` | warn | manual | A profile extraction met its draft in another generation; the draft stays `extracting_profile` and holds the one open-draft slot. Fail it by hand (below the table), then resolve the alert. |
| `dedup.ambiguous_grouping` | info | auto | A job could join more than one duplicate group, and joined the best. Information. |
| `quota.serpapi_renewal_stale` | warn | auto | SerpApi's monthly window is still unconfirmed 48 h after its start: check the SerpApi plan and its renewal. A read that confirms the window resolves it. |
| `reconciler.failed` | warn | auto | A reconciler raised: `$JH logs worker` names it (`reconciler_failed`, with the error class). Report it if it repeats; its next success resolves it. |
| `retention.failed` | warn | auto | A retention rule failed: `$JH logs worker` names it (`retention_rule_failed`). The next nightly pass tries again, or `jobhunter retention run` at once (section 11); report it if it repeats. |
| `api.unhandled_exception` | warn | manual | A request ended in a 500: a bug. `$JH logs api` gives its error class; report it; then resolve it. |
| `auth.totp_key_mismatch` | critical | manual | No mounted data key opens the TOTP secret: a wrong `data_key`, or a key a restore did not rotate (section 9). Mount the right key (section 7); if it is lost, `$JH run --rm --no-deps api jobhunter user totp disable` and turn TOTP on again. Then resolve it. |
| `docparse.leftover` | warn | auto | docparse could not remove a leftover on the parse tmpfs and takes no job. Recreate the worker and docparse containers, which share it: `$JH stop worker docparse`, `$JH rm -f worker docparse`, `$JH up -d --no-deps --wait worker docparse`. A clean start resolves it. |

**Failing a draft by hand** (`profile.extract_generation_mismatch`). The draft the alert's context names
(`draft_id`) stays `extracting_profile` with nothing to drive it on, and it holds the one open-draft slot, so
no upload or paste starts. First check that no operation can still move it, by a read-only query that must
return no row (a row: wait until that operation has ended):

```
$JH exec postgres psql -U jobhunter_app -d jobhunter -c "SELECT id, kind, status FROM operations WHERE kind IN ('extract_profile_text', 'extract_profile') AND payload->>'draft_id' = '<draft_id>' AND status IN ('pending', 'running')"
```

Then fail it along the transition matrix's edge `extracting_profile → failed`, as the application role (the
table's trigger checks the edge):

```
$JH exec postgres psql -U jobhunter_app -d jobhunter -c "UPDATE profile_drafts SET status = 'failed', status_reason = 'attempts_exhausted', failure_code = 'attempts_exhausted', status_changed_at = now(), updated_at = now() WHERE id = <draft_id> AND status = 'extracting_profile'"
```

`UPDATE 1` means it failed. The CV page then shows it failed, to retry, to discard, or to replace by a new
upload or paste, which discards it.

## 7. Secret rotation

**A secret file** is replaced with the command that installed it, then the services that hold it are
restarted:

```
install -o root -g 10010 -m 0440 <new file> /etc/jobhunter/secrets/<name>
$JH restart <services>
```

Then delete `<new file>`. Never `up -d` alone: Compose compares a service's configuration, not a secret file's
content, so it would keep the running containers with the old value (R6); a restart mounts the file again.

| Secret | Then |
|---|---|
| `session_secret` | `$JH restart api`; it logs the user out (every session ends) |
| `anthropic_key`, `rapidapi_key`, `serpapi_key`, `adzuna_app_id`, `adzuna_app_key`, `langsmith_key`, `ntfy_token`, `healthchecks_ping_url`, `healthchecks_api_key` | `$JH restart worker` |
| `s3_access_key_id`, `s3_secret_access_key` | `$JH restart backup` (the restore container is one-off; during a backup job the restart waits up to 10 minutes, section 8); the offline recovery kit takes the new keys |
| `data_key` | the data key's procedure (below) |
| `db_app`, `db_migrator`, `db_backup`, `db_restore`, `pg_bootstrap` | `scripts/rotate-db-role.sh` (below) |
| `restic_password` | `scripts/rotate-restic-password.sh` (below) |

Two are never rotated by a new file alone. `restic_password` opens the repository: a new file alone locks the
backups out of it, so its script changes the repository's keys before it installs the file. `pg_bootstrap` is
the bootstrap role's password, read by postgres's first start and by `scripts/rotate-db-role.sh`'s one-off
container, which connects with it: its rotation sets the role's password while the old file is installed, then
installs the new one.

**The data key** (§16.8; R1, Deviation 31). One rotation at a time: two `crypto rotate` runs at once are safe
but pointless.

1. Make the new key, and put it into the offline recovery kit with its key id before anything else (section
   8); the old key stays there too:

   ```
   umask 077
   openssl rand -base64 32 > <new key file>
   base64 -d <new key file> | sha256sum | cut -c1-16
   ```

2. Mount the old key as `data_key_previous` and the new one as `data_key`, restart the two services that hold
   them, and rotate:

   ```
   install -o root -g 10010 -m 0440 /etc/jobhunter/secrets/data_key /etc/jobhunter/secrets/data_key_previous
   install -o root -g 10010 -m 0440 <new key file> /etc/jobhunter/secrets/data_key
   $JH restart api worker
   $JH run --rm --no-deps worker jobhunter crypto rotate
   ```

   Delete `<new key file>`. The rotation re-encrypts every ciphertext under the previous key with the current
   one (the CV files, the CV texts, the TOTP secret, the exports), resumably: a run stopped anywhere is run
   again. Its verification pass then counts what remains by key id, and it prints `{"rewritten": {…},
   "remaining_by_key_id": {…}}` (key ids and counts only). Its exit code says what comes next:

   | Exit | Output | Next |
   |---|---|---|
   | 0 | the report | Only the current key remains: step 3. |
   | 1 | `"error": "previous_key_remains"` | A ciphertext is still under the mounted previous key, sealed by a service that was not restarted: `$JH restart api worker`, then run it again. |
   | 2 | `"error": "no_previous_key"` | `data_key_previous` is empty, and a ciphertext under another key than the current one exists; nothing was rewritten. Mount the old key (step 2). |
   | 3 | `"error": "other_keys_remain"` | Keys it was not given remain (`remaining_by_key_id`): the next pass installs one of them, copied from the kit, as `data_key_previous` (the copy deleted once installed), restarts api and worker, and runs it again. |

   Exit 1 is also `{"error": "database"}`, or `{"error": "unexpected", …}` naming the kind, the table and the
   row of a ciphertext that cannot be rotated; every rerun stops at that row until it is dealt with. Its
   `error_class` `ExportUnauthenticated` names an export already under the current key whose row's hash
   differs from its archive's and whose archive does not open: the rotation does not repair that hash. That
   archive is damaged or another's, and not the rotation's to fix. Keep `data_key_previous` mounted and wait:
   an export expires 7 days after it was made, the `exports` retention rule then ends it (`expired`, its
   archive removed) at the next nightly pass or at `jobhunter retention run` (section 11), and the rotation,
   which reads `ready` exports only, passes it when run again.
3. Once it exits 0, empty `data_key_previous` and restart again:

   ```
   install -o root -g 10010 -m 0440 /dev/null /etc/jobhunter/secrets/data_key_previous
   $JH restart api worker
   ```

   The old key stays in the offline recovery kit for 3 months and a week after this pass (section 8).

**A database role** (R7). `scripts/rotate-db-role.sh` installs the new password as the role's secret file,
sets the role's password to it in PostgreSQL through a one-off postgres container (the password goes from the
file to psql inside the container, never onto a command line), and restarts the services that hold it: api
and worker for `db_app`, backup for `db_backup` and `db_restore`, none for `db_migrator` (migrate and restore
are one-off). It defaults to the production command line and `/etc/jobhunter/secrets`, so it takes neither
`--compose` nor `--secrets-dir` here:

```
umask 077
openssl rand -hex 24 > <new file>
sudo scripts/rotate-db-role.sh --dry-run db_app <new file>
sudo scripts/rotate-db-role.sh db_app <new file>
```

Then delete `<new file>`. For `db_app`, new connections of api and worker fail for a few seconds between the
role's new password and their restart, which follows from the order install, alter, restart; connections
already open keep working, and the restart ends the window.

**The bootstrap role** (`pg_bootstrap`). The same script, with `pg_bootstrap`, goes the other way round: its
one-off connects with the installed password and sets the bootstrap role's to the new one, which it reads from
its stdin (the script feeds it `<new file>`), never from a command line; then it installs `<new file>`. Nothing
restarts: postgres reads the file at its first start only.

```
umask 077
openssl rand -hex 24 > <new file>
sudo scripts/rotate-db-role.sh --dry-run pg_bootstrap <new file>
sudo scripts/rotate-db-role.sh pg_bootstrap <new file>
```

Then delete `<new file>`. If it stops before the install, the role keeps its old password: run it again. If
the install itself fails, the script says so: install `<new file>` as `pg_bootstrap` with the `install` command
above before any other database role's rotation, whose one-off connects with that file.

**The restic password** (`restic_password`; §19.5). restic encrypts the repository with a master key that each
of its keys wraps, one per password, so the rotation adds a key for the new password and removes the old
password's key: every backup set stays as it is and opens with the new password only. Put the new password
into the offline recovery kit first, beside the old one (section 8).

```
umask 077
openssl rand -base64 32 > <new file>
sudo scripts/rotate-restic-password.sh --dry-run <new file>
sudo scripts/rotate-restic-password.sh <new file>
```

In this order (Ruling FU-10): a one-off backup container, which reads the new password from its stdin, adds a
key for it (`restic key add`) and checks that the new password opens the repository through that key, and
names the old password's key (`the old key is <id>`); the script installs `<new file>` as `restic_password`
and restarts backup (during a backup job the restart waits up to 10 minutes, section 8); then a second one-off,
which mounts the new password, removes the old key (`restic key remove`) and checks that it is gone. So the
backup service's restic calls always use a password whose key is there, whenever it runs. Every restic call
waits for a running job's repository lock, as the jobs' calls do. Its last line is
`rotate-restic-password: done: …`. Then delete `<new file>`, and remove the old password from the kit: it
opens nothing any more.

If it stops before the install (`… is unchanged and nothing was restarted`), run it again: it adds no second
key. If the install fails (`… still holds the old one: run it again`), run it again too. Once the new file is
installed, never remove the old key before backup has restarted: until then the running backup container
may still mount the old password. So if the restart fails, it says so, and a run of the script again after
an install whose run then stopped (`… holds the new password already …`, naming the repository's other keys)
says the same: restart backup first (`$JH restart backup`, harmless when the earlier run restarted it), then
remove the old key by hand, below. If the removal fails, it exits 1 naming the old key's id (`… the old key
<id> may still be in the repository …`): the new password is installed and backup restarted, and the line
before it says how restic exited (a failed `restic key remove`, or a failed check after it, when the key may
be gone already). Once the cause is fixed, list the keys, as the new password:

```
$JH run --rm --no-deps -T backup sh -c 'AWS_ACCESS_KEY_ID=$(cat /run/secrets/s3_access_key_id) AWS_SECRET_ACCESS_KEY=$(cat /run/secrets/s3_secret_access_key) restic key list --no-cache --retry-lock 30m --password-file /run/secrets/restic_password'
```

It lists each key by the first 8 hex digits of its id, the one the installed password opens marked `*`. If
the old key is listed, remove it by its id (the full one the script named, or those 8 digits):

```
$JH run --rm --no-deps -T backup sh -c 'AWS_ACCESS_KEY_ID=$(cat /run/secrets/s3_access_key_id) AWS_SECRET_ACCESS_KEY=$(cat /run/secrets/s3_secret_access_key) restic key remove --no-cache --retry-lock 30m --password-file /run/secrets/restic_password <old key id>'
```

It prints `removed key <old key id>`. The bucket's versioning keeps a removed key as a noncurrent version for its
30 days (section 1): until then, someone holding both the old password and the bucket's keys could still open
the repository, so a rotation because the password may have leaked rotates the S3 keys too (the table above).

## 8. Backups and the offline recovery kit

**The backups** (§19.5) run in the backup container on their schedule, in UTC: a backup set nightly at 01:15
(`JOBHUNTER_BACKUP_CRON`); the deletion manifests shipped every 5 minutes (`JOBHUNTER_MANIFEST_SHIP_CRON`);
the prune on Sundays at 02:30 (`JOBHUNTER_BACKUP_PRUNE_CRON`), which keeps 7 daily, 4 weekly and 3 monthly
sets, and forgets the `manifests` snapshots older than the latest one a shipment confirmed (that one holds
every manifest shipped up to it, and newer ones are kept, so no manifest is lost, §19.5); the restore test on
the first Sunday of the month at 03:30 (`JOBHUNTER_RESTORE_TEST_CRON`). One backup job runs at a time:
another prints `{"error": "backup_job_busy"}` and changes nothing. A shipment holds a lock of its own instead
(§19.5's shipment lock): one by hand while the scheduler's runs waits for it, then ships what is left, so it
never prints `backup_job_busy`. The prune holds the shipment lock too while it chooses and forgets `manifests`
snapshots, so a ship started then waits for it as well. A restore reads every `manifests` snapshot, never only
the newest (section 9). System → Health shows the latest set, shipment and restore test, with their ages. By
hand, each printing one JSON line:

```
$JH exec backup jobhunter backup set
$JH exec backup jobhunter backup ship
$JH exec backup jobhunter backup restore-test
```

**A lost lock.** A ship and a prune each hold a lock in PostgreSQL for the length of their job, on a session
of their own. When that session ends while the job runs (PostgreSQL restarted, the connection cut), `backup
ship` prints `{"error": "ship_lock_lost"}` and a prune's run fails `prune:ShipLockLost`: the job wrote
nothing (no receipt; no snapshot forgotten, no receipt removed), and the next run repeats it, the scheduler's
within 5 minutes for a ship and on Sunday for a prune. Nothing else is needed; if it recurs, find out why
PostgreSQL restarts or drops the backup container's connections (`$JH logs postgres`).

A restore test started by hand restores the latest set into a scratch database and `/data/staging/restore_test`
and removes both when it ends. If that process is killed hard (an OOM, a `kill -9`), nothing removes that full
copy of the database until another restore test or a restart of the backup service: so run
`$JH exec backup jobhunter backup restore-test` again, or `$JH restart backup`. Every restore test, the monthly
one included, first removes what an earlier one left. A restore test that could not drop its scratch database,
or remove its scratch directory, never ends `succeeded`: when every step passed, its run fails
`cleanup:<error class>` and `backup.restore_test_failed` pages (section 6); when a step failed, the run keeps
that step's code and its report names the cleanup's error beside it (`cleanup_error`, the error's class).
`$JH logs backup` names it too (`restore_test_scratch_not_dropped`, `restore_test_staging_not_removed`).
Either way the copy stays until the next restore test or `$JH restart backup`, so run one of them.

**The prune's report.** A prune records what it did in its run's report, read with:

```
$JH exec postgres psql -U jobhunter_app -d jobhunter -c "SELECT id, status, report FROM backup_runs WHERE kind = 'prune' ORDER BY id DESC LIMIT 1"
```

`manifests_forgotten` counts the `manifests` snapshots it forgot. When it forgot none on purpose,
`manifests_skipped` says why: `no_receipt` (no shipment has written a receipt yet) or `receipt_not_listed` (it
removed a false receipt, below); it is `null` otherwise. `no_receipt` stays until the first deletion has
shipped: a shipment of an empty stream writes no receipt, so a new installation reports it every week until
then, which is harmless. `receipt_not_listed` clears at the next shipment, and the next prune forgets again.

**A false receipt.** A shipment's receipt (a `manifest_shipments` row) names the snapshot that confirmed its
manifests offsite. If the prune finds the latest receipt's snapshot missing from the repository (deleted by
hand, or lost), that receipt confirms nothing: the prune removes it, and every receipt below it whose snapshot
is missing too, logs `manifest_receipt_removed` with each one's shipment id, snapshot id and item count,
forgets no `manifests` snapshot that week, and lists the removed receipts in its run's report
(`receipts_removed`). The deletions those receipts confirmed are no longer shown confirmed offsite, and
`privacy.manifest_unshipped` opens for them. Nothing else is needed: the next shipment (within 5 minutes)
uploads the whole stream, and the alert resolves. If it does not, the shipment fails: treat it as that alert
says (section 6). Find out how the snapshot went missing before the next prune.

**After a restore of an older set.** A backup set taken before a prune removed a false receipt still holds
that receipt, and the receipts below it, which name snapshots earlier prunes forgot on purpose. After a restore
of that set (section 9), every shipment finds the stream equal to that receipt's items and ships nothing, until
the next weekly prune finds the receipt's snapshot missing. That prune then removes the stale receipts
(usually all of them), so expect, up to a week after the restore: `receipts_removed` listing them in its
report, a `manifest_receipt_removed` line for each in `$JH logs backup`, and one critical
`privacy.manifest_unshipped` alert, paged, for every deletion they confirmed, each shown not confirmed
offsite. Nothing is lost: a `manifests` snapshot in the repository holds every manifest throughout. The next
shipment, within about 5 minutes, uploads the whole stream, and the alerts resolve by themselves.

**A restart of the backup service waits for its job.** `$JH restart backup`, `$JH stop` and an `up` that
recreates the service let a running job finish: the service has a stop grace of 10 minutes (§19.5). Between
jobs it stops at once. During a job it can take up to those 10 minutes: expect that during the nightly set
(from 01:15), the Sunday prune (from 02:30) and the monthly restore test (the first Sunday, from 03:30), or a
job started by hand. A job still running when the 10 minutes end is killed and recorded `interrupted`; the next
start removes what it left (a restore test's scratch database and staging).

**Before any volume is removed** (a rollback by restore, section 10; a timed restore onto the same VPS, section
13). The manifests volume holds the only copy of every deletion manifest not shipped yet, and a restore replays
the offsite stream only: a deletion never shipped would not be replayed, and the data it deleted would come
back. So, before a `docker volume rm` or a `down` that removes volumes, whatever was stopped before:

1. Stop api and worker, so that no purge starts, or writes a manifest, after the check of step 3 (a purge is
   requested through the api and runs in the worker). They stay stopped until the volumes are removed:

   ```
   $JH stop api worker
   ```

2. Ship the manifests:

   ```
   $JH exec backup jobhunter backup ship
   ```

   Only exit 0 lets the procedure go on: `{"shipped": true, …}`, or `{"shipped": false, …}` when the stream was
   shipped already. On `{"error": "locked"}` (restic's repository lock, which a prune can hold longer than a
   ship waits) or `{"error": "ship_lock_lost"}` (a lost lock, above), run it again; a ship never prints
   `backup_job_busy`, since it waits for a ship already running. On any other error, stop and remove no
   volume: fix the cause as the `privacy.manifest_unshipped` row says (section 6), then ship again.
3. Check that the latest shipment holds every manifest file, as the application role. api and worker are
   stopped, so a manifest's `status` can lag behind its shipment; this query reads the shipment itself:

   ```
   $JH exec postgres psql -U jobhunter_app -d jobhunter -c "SELECT m.id, m.status, m.file_sha256 IS NOT NULL AS has_file FROM deletion_manifests m WHERE NOT EXISTS (SELECT 1 FROM manifest_shipment_items i WHERE i.shipment_id = (SELECT max(id) FROM manifest_shipments) AND i.manifest_uuid = m.manifest_uuid AND i.file_sha256 = m.file_sha256)"
   ```

   No row: nothing is left unshipped. A row with `has_file` true: ship again (step 2), and remove no volume
   until the query returns no row. A row with `has_file` false is a purge that had not started deleting (its
   deletions start only once its file exists, §19.5): the volumes take it with them, so note its id and request
   that deletion again afterwards. Before step 1, while the api still runs, System → Health's unshipped
   manifests and the alert `privacy.manifest_unshipped` give an early view of the same.

**The offline recovery kit** (R1). Only the kit restores a lost VPS: the backup sets are restic's, encrypted
with `restic_password`, and the CV files, the CV texts and the TOTP secret in them are sealed with the data
key. The kit holds:

- the current `data_key`, labelled with its key id;
- every earlier data key a retained backup set may still need, each labelled with its key id;
- `restic_password`, `s3_access_key_id` and `s3_secret_access_key`;
- `RESTIC_REPOSITORY`, with `AWS_DEFAULT_REGION`;
- a copy of `/etc/jobhunter/jobhunter.env`;
- a copy of `/etc/jobhunter/operator.json`, the privacy statement's operator values (section 1).

It is kept offline, at the place the deploy checklist names (section 1): never on the VPS alone, in the bucket
or in this repository. It changes before the stack does: a new data key goes into it before it is installed
(section 7), and so does a new `restic_password` before its rotation, which then replaces the kit's old copy
(the old password opens nothing once the rotation is done, and every set opens with the new one, section 7);
new S3 keys, a changed environment file or a changed operator file go in when they change. A key id is the
first 8 bytes of the key's sha256, in hex, as `crypto rotate` and `restore run` print it:
`base64 -d <key file> | sha256sum | cut -c1-16`.

A key retired by a rotation stays in the kit until no retained set names it in its `data_key_ids`, the key ids
each set's `backup_set.json` records (Deviation 31). restic keeps sets for about 3 months (7 daily, 4 weekly,
3 monthly), so a retired key stays for 3 months and a week after the rotation's verification pass (its exit 0,
section 7), then leaves the kit.

## 9. Restore

On a fresh host (a lost VPS, or the timed restore of section 13), from the offline recovery kit only. api and
worker stay stopped until the last step.

1. Host setup (section 2), with the kit's current `data_key`, `restic_password`, `s3_access_key_id` and
   `s3_secret_access_key` in `<dir>`, and its `/etc/jobhunter/jobhunter.env` and `/etc/jobhunter/operator.json`
   (section 2, item 3, with the modes there); the other secrets are new (the database passwords made afresh,
   the providers' keys from the providers). The DNS A record names the new host. No `backup init`: the
   repository exists.
2. The restore:

   ```
   $JH up -d --wait postgres
   $JH --profile restore run --rm restore
   ```

   postgres starts on an empty data directory, and its first start creates the roles (§19.3). The restore
   runs §19.5's order: the manifest stream, the latest backup set (`$JH --profile restore run --rm
   restore jobhunter restore run --set <id>` takes another), `pg_restore`, the `restored` verification, the CV
   files, `migrate` with its replay, the exports and the `replayed` verification. The manifest stream is the
   union of every `manifests` snapshot, never only the newest: a snapshot restic left incomplete hides no
   manifest an earlier one holds. A snapshot that holds no file at all (a shipment whose files restic could
   not read) is skipped with the log line `manifest_snapshot_empty` and its id: it holds no manifest, so
   nothing is to be done about it. A file name two snapshots hold with different bytes stops the restore at
   its first step (`{"error": "manifest_stream_conflict", "step": "stream"}`; the log line
   `manifest_stream_conflict` names the file and both snapshots): a manifest file is written once, so this is
   a damaged repository, and the restore never picks one. Stop there and keep the repository as it is: the
   two snapshots the log line names hold the two versions. It refuses a database or a CV
   store that is not empty (`database_not_empty`, `cv_store_not_empty`). Its two verification outputs are the
   log lines `restore_verified` with `phase` `restored`, then `replayed`, each with `ok` true and no
   `failures`. Then it prints `{"restored": true, "set_id": <id>, "data_key_ids": [<key id>, …]}`: the key ids
   of the set's ciphertexts. A failure prints `{"error": "<code>", "step": "<step>"}` and exits 1. Exports are
   not in a backup set: each comes back `expired`, or `purged` when a later deletion named it, and the user
   requests a new one.
3. Before `up -d`, one pass per key id of the set other than the kit's current key: each installs that key from
   the kit as `data_key_previous` and rotates:

   ```
   install -o root -g 10010 -m 0440 <kit key file> /etc/jobhunter/secrets/data_key_previous
   rm <kit key file>
   $JH run --rm --no-deps worker jobhunter crypto rotate
   ```

   `<kit key file>` is the key copied from the kit onto the VPS; it is deleted once installed, and step 4
   empties the installed one.

   On exit 3, `other_keys_remain`, its `remaining_by_key_id` names the key ids still present: the next pass
   installs one of them. On exit 0 only the current key remains. The current key's id is `base64 -d
   /etc/jobhunter/secrets/data_key | sha256sum | cut -c1-16`; a set whose `data_key_ids` hold only it needs no
   pass. The other exit codes are section 7's.
4. Empty `data_key_previous`, then start the stack:

   ```
   install -o root -g 10010 -m 0440 /dev/null /etc/jobhunter/secrets/data_key_previous
   $JH up -d --wait
   ```

5. The health check (section 5). The next nightly set is the new host's first. If the set restored is older
   than a false receipt a prune removed, the next weekly prune pages once for every deletion, which resolves
   by itself within about 5 minutes and loses nothing: section 8, **After a restore of an older set**.

**`restore verify` by hand.** `restore run` runs both verification phases itself. `jobhunter restore verify
--set <id> --phase restored` alone is for a restore staged by hand, before `migrate`: both `backup_set.json`
and `jobhunter.dump` of the set must be in `/data/staging/restore` (the `backup_staging` volume). The backup
service's start removes `/data/staging/restore`, a full copy of the database that no purge reaches, so stage a
set for this only while `backup` stays up, or stage it again after a restart of `backup` (a restore test
leaves it as it is). `--phase replayed` may be run again at any time:

```
$JH --profile restore run --rm restore jobhunter restore verify --set <id> --phase replayed
```

The RTO is 4 h (§19.5), measured by the deployment step's timed restore (section 13, item 12).

## 10. Upgrade and rollback

**Releases** (R24; `.github/workflows/release.yml`). A release is dispatched by hand on the commit to deploy
(Actions → Release → Run workflow). It pushes the app, backup, docparse and embedder images to GHCR, tagged
with the commit, and opens the digest PR, which writes their digests into `deploy/release.lock`; a deploy runs
that PR's head commit, by its release tag (below).

- The workflow needs the repository setting that lets GitHub Actions create pull requests (Settings → Actions →
  General → Workflow permissions); it is off by default.
- GHCR packages start private: the VPS logs in to `ghcr.io` with a read-only token (`read:packages`; section 2).
- The digest PR is opened with the workflow's `GITHUB_TOKEN`, so it starts no CI: run CI on it by hand, by
  dispatching the workflows that offer it or by pushing a commit to its branch.
- An attempt that fails after its push leaves its branch `release/<sha>-<run id>-<attempt>` and its tag: delete
  both (`git push origin --delete <branch> <tag>`), as for a digest PR closed without merging.
- The runner's disk for the real embedder build is checked at its first run.
- The workflow tags the digest PR's head commit `release-<YYYYMMDD>-<12 hex digits>`, the run's date in UTC and
  the start of that commit's hash, and pushes the tag with the PR's branch; the PR's description names it. It
  tags the head commit, not a merge commit: the head's tree is the tree the images were built from plus
  `deploy/release.lock`, while a merge commit can carry other commits that landed meanwhile. Every attempt
  makes a commit and a tag of its own. Merge the PR, then deploy its tag: the release tag is what the VPS
  checks out.

**An upgrade** (§19.6):

0. Check out the release tag (R24), the `release-<YYYYMMDD>-<12 hex digits>` the merged digest PR names:
   `git fetch --tags`, then `git checkout <release tag>`, in `/opt/jobhunter`.
1. `$JH pull`.
2. A backup set and a shipment just before the change, which a rollback by restore starts from:
   `$JH exec backup jobhunter backup set` (note the `set_id` it prints), then
   `$JH exec backup jobhunter backup ship`. On the set's `{"error": "backup_job_busy"}` (another backup job
   runs), or either's `{"error": "locked"}` (restic's repository lock), run that command again; on another
   error, fix it (the `backup.failed` row, section 6). Do not start the upgrade without a
   `set_id` and a ship that exited 0.
3. `$JH stop api worker`: `migrate` and `jobhunter manifests replay` run only with api and worker stopped
   (§19.5); the worker drains within its 120 s.
4. `$JH run --rm migrate`, which exits 0.
5. `$JH up -d --wait`; a run that was in flight resumes as a new attempt.
6. The health check (section 5).

**Host files a checkout changes.** A checkout gives the files the containers bind-mount, and the egress rule,
new content that the running containers and the loaded rule do not see, and `up -d` recreates a container only
when its compose configuration changed. After the checkout of step 0, or of a rollback, list what changed:

```
git diff --stat <previous release tag> <release tag> -- deploy/Caddyfile deploy/caddy deploy/postgres/postgresql.conf deploy/firewall
```

- `deploy/postgres/postgresql.conf`: `$JH restart postgres` in step 3, once api and worker are stopped.
- `deploy/Caddyfile` or `deploy/caddy/`: `$JH restart caddy` after step 5.
- `deploy/firewall/docker-user.nft`: `systemctl restart jobhunter-egress`.
- `deploy/firewall/jobhunter-egress.service`: install it again (section 2, step 6 of the manual equivalent),
  then `systemctl daemon-reload` and `systemctl restart jobhunter-egress`.
- After either, `nft list table inet jobhunter_egress` lists the table (section 5's check after any firewall
  change).

A change of `/etc/jobhunter/jobhunter.env` is section 4's.

Every `migrate` re-checks each completed manifest's postconditions, a few count queries per manifest, so its
time grows with the number of manifests kept: negligible at this installation's scale, and the first place to
look when a `migrate` slows down after years of deletions.

**A rollback** redeploys the previous release tag when its `migrate` reports the schema `ahead_compatible`, and
restores the last backup set when it reports `migrate_schema_ahead_incompatible` (Deviation 32):

1. `git checkout <previous release tag>`, `$JH pull`, `$JH stop api worker`, `$JH run --rm migrate`.
2. Exit 0, with the log line `migrate_schema_ahead_compatible`: the previous release runs on the newer schema,
   and `/readyz` passes. `$JH up -d --wait`, then the host files that changed (above) and the health check.
3. Exit 1, with the log line `migrate_schema_ahead_incompatible`: the newer schema is none the previous release
   can run on (a destructive revision, or a floor it does not know). Roll back by restore, with the previous
   release still checked out, and only when it reads every manifest the newer release wrote (**Rollbacks and
   the manifest format**, below). If it cannot, remove no volume: stay on the newer release
   (`git checkout <release tag>`, then `$JH up -d --wait`) and roll forward with a fix. Otherwise:
   1. Stop api and worker, and ship the manifests: the first two steps of section 8's procedure before a wipe,
      so that this path relies on no earlier stop:

      ```
      $JH stop api worker
      $JH exec backup jobhunter backup ship
      ```

      The ship must exit 0; on `{"error": "locked"}`, run it again (a ship never prints `backup_job_busy`).
   2. Then the check, step 3 of section 8's **Before any volume is removed**: it must return no row. On any
      failure of these two steps, stop here and remove no volume.
   3. Remove the data volumes, and restore the set of the upgrade's step 2:

      ```
      $JH down
      docker volume rm jobhunter_pgdata jobhunter_cv_store jobhunter_manifests jobhunter_exports jobhunter_backup_staging
      $JH up -d --wait postgres
      $JH --profile restore run --rm restore jobhunter restore run --set <id>
      ```

      Then section 9's steps 3 to 5. What was written after that set is lost, except the deletions: their
      manifests are offsite, in every `manifests` snapshot the restore reads (section 9), and the previous
      release's restore replays them, as it reads them (below).

**Rollbacks and the manifest format** (Ruling T31-2). A release whose migration narrows a transition edge of an
existing table declares its revision destructive (`db/compat.py`): its rollback is a restore, never a redeploy
of the previous tag. A change of the manifest format ships expand/contract instead: release N teaches the
parser the new format while it still writes the old one, and release N+1 writes the new format. A rollback from
N+1 lands on N, whose restore reads both, so the restore of a rollback replays every manifest of the stream.
Should the previous release not read the newer manifests (a format change that skipped release N), its restore
would stop at the first of them (`ManifestInvalid`, in its `migrate` step) after every volume was removed: so
remove no volume, stay on the newer release, and roll forward with a fix.

**The drill.** `tests/stack/test_prod_drills.py` rehearses an upgrade and its rollback on the compose test
stack, never on the VPS. `scripts/build-prev-image.sh`, which takes no arguments and needs a clone with the
full history, builds the release before migration 0012 for it as `jobhunter-app:prev`; it removes no image.

## 11. Disk pressure and a lost password

**Disk pressure** (`system.disk_pressure`: the data volumes' disk 80 % full, critical from 90 %;
`system.table_growth`: `procrastinate_jobs` above a million rows, or a table above 10 GB):

1. Where the space goes: `df -h /var/lib/docker` and `docker system df`; the table sizes in System → Health
   (`table_bytes` of `GET /api/system/health`, the figures of the gauge `jobhunter_table_bytes`).
2. The retention pass (`retention_daily`, nightly at 03:17 UTC) deletes what §20.1 lets go. When the disk
   fills before its next pass, run it now, as the worker, while the worker runs:

   ```
   $JH run --rm --no-deps worker jobhunter retention run
   ```

   It runs the same pass once, fenced by the worker's current epoch as the worker's own pass is, each rule under
   its usual locks, so it is safe beside the running worker and beside its nightly pass. It prints
   `{"counts": {…}}`, what each rule deleted (rows, threads, files, the jobs it tombstoned). A count of `-1` is
   a rule that failed or was skipped, and the command exits 1: its output's `retention_rule_failed` line and
   the `retention.failed` alert name the rule (section 6). `{"error": "fenced_out"}`: the worker started
   again meanwhile; run it again. `{"error": "no_worker_epoch"}`: no worker has started on this database yet.
   It runs only the pass: the daily reconcilers stay the worker's, after its nightly pass. The space it frees
   is reused by the table's next rows; step 3 makes it reusable sooner.
3. Vacuum the table that grew, as its owner:

   ```
   $JH exec postgres psql -U jobhunter_migrator -d jobhunter -c "VACUUM (ANALYZE) <table>"
   ```

   A plain `VACUUM` makes the space reusable. `VACUUM FULL` returns it to the disk but locks the table while it
   runs: stop api and worker first (`$JH stop api worker`), and start them after (`$JH up -d --wait`).
4. Old images: the previous release's images stay for a rollback (section 10); remove only older ones
   (`docker image ls`, `docker image rm <image>`).
5. The containers' logs are capped: Docker's `local` driver keeps 10 files of 20 MB per container.

**A lost password**: `$JH run --rm --no-deps api jobhunter user set-password` asks for the new one twice and
revokes every session. A lost authenticator: `$JH run --rm --no-deps api jobhunter user totp disable`, then
`$JH run --rm --no-deps api jobhunter user totp enable` for a new one; it prints the otpauth URI, which holds
the TOTP secret, and its QR code, so clear the terminal and its scrollback afterwards. A login lockout:
`$JH run --rm --no-deps api jobhunter sessions unlock`. Each runs in the api's container and passes the api's
startup assertions first, so `user …` and `sessions …` need a valid operator file, as the api does: compose
does not create the container without `/etc/jobhunter/operator.json`, and an invalid one exits 78 naming the
problem (section 4, a changed operator file).

## 12. The embedder model

The VPS pulls the release's embedder image (`embedder_image` of `deploy/release.lock`) and never builds it, so
it needs no route to `huggingface.co`: the model is in the image, and the embedder checks its files against
`embedder/model.lock` at every start (`embedder.checksum_mismatch`, section 6). `release.yml` builds the image
on GitHub's runner, whose build downloads the model. A host that must build it stages the files of
`embedder/download_model.sh` on a host that has the route (M1 Carry-forward 6): `embedder/download_model.sh
<model dir>` there downloads each file of `embedder/model.lock` and checks it against its sha256. The build as
it stands takes no staged files (its model stage runs the same script, and the build context leaves
`.model-cache/` out), so it too runs on a host with the route.

**The export.** The lock pins the fp32 export (V7; M1 ruling T21-a). V7 on the VPS's CPU measures fp32 and int8
(section 13, item 9), and the user decides (M1 Deviation 47). int8 means a new `embedder/model.lock` and a new
release, whose embedder has a new model id: the revisions are embedded again in the background, and vectors of
the two models are never compared (§19.6).

## 13. The deployment step

Carry-forward 1's procedure, with the user (M5-U1), in this order. Record each item as passed, blocked (by
what) or not attempted (why), in `docs/spikes/V21.md`, `docs/spikes/V15.md` and the deployment record.

1. [ ] **The VPS.** Provision it (section 1): EU, 4 vCPU with AVX-512 VNNI preferred, 16 GB, ≥ 100 GB NVMe,
   LUKS where offered, no swap or encrypted swap only, the pinned Docker Engine version; SSH key-only.
2. [ ] **The inputs.** The deploy checklist's inputs (section 1), the operator file's five values, and the
   exclusivity confirmations (C2, C3).
3. [ ] **The release.** Run `release.yml`, merge the digest PR, check out the release tag on the VPS (section
   10).
4. [ ] **Host setup.** `sudo scripts/host-setup.sh --secrets-from <dir> --delete-sources` (section 2); the
   egress unit loads at boot.
5. [ ] **DNS and Caddy.** The A record; Caddy against Let's Encrypt staging first (`acme_ca`), then production
   (section 3, step 2).
6. [ ] **The first start.** `$JH run --rm backup jobhunter backup init` against the real S3 bucket;
   `$JH up -d --wait`; `user create`; `/readyz` from outside (section 3).
7. [ ] **V21: N1 on the VPS image.** The record is the N1 stack test, run on the VPS as `docs/spikes/V21.md`
   says, with `JOBHUNTER_N1_OUTSIDE=1`: api and docparse can neither resolve nor connect out, the worker can,
   its reach to the outside included. The test brings up the compose test stack, which uses the production
   stack's subnets, and it loads and deletes the egress rule's table itself (it skips while the production
   table is loaded). So the production stack and the rule stop for it, and start again after. From a scratch
   checkout of the release tag, with uv (as for item 9):

   ```
   $JH down
   systemctl stop jobhunter-egress
   cd <scratch checkout>
   JOBHUNTER_N1_OUTSIDE=1 uv run pytest -m stack -q -rs -p no:cacheprovider tests/stack/test_prod_network.py
   cd /opt/jobhunter
   systemctl start jobhunter-egress
   $JH up -d --wait
   ```

   N1 passes only when every test of the module ran and passed, `test_worker_reaches_outside` included: a
   skipped test counts as not passed, and so does a module that skips (it skips while the production table is
   still loaded, or while another stack holds the subnets; `-rs` prints why). The module builds
   the test stack's images (the embedder's `fake` target, so no model), makes its dummy secrets in the scratch
   checkout and removes its own projects at the end; delete the scratch checkout afterwards. Then reboot once,
   and check on the production stack that the rule loaded at boot and that the boundary holds. These hand
   probes are a quick check, not the record. api and docparse connect to a literal address, so what fails is
   the connection itself, not a name lookup:

   ```
   systemctl status jobhunter-egress
   nft list table inet jobhunter_egress
   $JH exec -T api python -I -c "import socket; socket.create_connection(('192.0.2.1', 443), 3)"
   $JH exec -T docparse python -I -c "import socket; socket.create_connection(('192.0.2.1', 443), 3)"
   $JH exec -T worker python -I -c "import socket; socket.create_connection(('example.org', 443), 3); print('connected')"
   ```

   The unit is `active (exited)` and the table is listed. api and docparse fail at once with `OSError: [Errno
   101] Network is unreachable`: they have no route out (192.0.2.1 is a documentation address). The worker
   prints `connected`.
8. [ ] **V20: N2 from two real client addresses**, with and without a spoofed header, as
   `docs/spikes/V20.md` says. Sign in from two networks (for example a phone on mobile data and a fixed line).
   Then, from one of them, send one login with a spoofed `X-Forwarded-For` (203.0.113.9, a documentation
   address) and a deliberately wrong password, so that no secret is typed; it answers 401. Read the attempts:

   ```
   curl -sS -H "Origin: https://<domain>" -H 'Content-Type: application/json' -H 'X-Forwarded-For: 203.0.113.9' --data '{"username": "<name>", "password": "<a wrong password>"}' https://<domain>/api/auth/login
   $JH exec postgres psql -U jobhunter_app -d jobhunter -c "SELECT at, host(ip), success FROM login_attempts ORDER BY id DESC LIMIT 4"
   ```

   Each attempt records its client's own address, never Caddy's, and the spoofed address appears nowhere:
   Caddy replaces the header.

9. [ ] **V7 on the VPS's CPU.** fp32 and int8 throughput, RSS and parity, with `docs/spikes/V7.md`'s commands,
   on the VPS host from a scratch checkout with uv, not in a container: `embedder/download_model.sh <model
   dir>`, the int8 file beside it with a scratch lock of its sha256, then `uv run --group embedder python
   scripts/spikes/v7_embedder.py <model dir>` and the same with `--onnx onnx/model_int8.onnx --fp32 <model
   dir>`. This one measurement needs the host's own route to `huggingface.co` and the package index. fp16 is
   not measured: spec revision 3.7 (Task 32) drops the fp16 fallback, which V7 measured slower than fp32. The
   user decides the export (M1 Deviation 47); a new embedder image if int8 (section 12).
10. [ ] **V15.** `ops notify-test` arrives on the phone; `ops deadman-sync --show` shows the period (section 3,
    step 5); healthchecks' own alert route is tested once (its integration's test notification).
11. [ ] **The first backups.** A first nightly backup set and a manifest shipment offsite (System → Health); a
    manual `$JH exec backup jobhunter backup restore-test`.
12. [ ] **The timed restore.** From the offline recovery kit only, by section 9, onto a second fresh VPS, or,
    after section 8's **Before any volume is removed** (api and worker stopped, then the manifests shipped and
    checked), onto the same one wiped; against the RTO of 4 h. Record the time.
13. [ ] **A release deployed and rolled back** (section 10).
14. [ ] **The soak.** A first free-sources collection, then 24 to 72 h of watching: the backup age, the dead-man
    pings, the alert delivery, the disk and the memory (`docker stats --no-stream`).

## The web UI's operator paths

Every path starts at **Profile & Settings** (`/settings`, the second item of the main navigation) →
**System** (`/settings#system`). System holds, in this order, Display & notifications, **Needs attention**
(`#needs-attention`), **Runs** (`#runs`), **Score cycles** (`#score-cycles`), **Alerts** (`#alerts`), **Golden
set** (`#golden-set`) and **Health** (`#health`). Each action button is disabled while its request runs; once it
succeeds, Needs attention and the header strip are read again, and a refusal shows at the item without them
being read again. The labels below are the English UI's.

**Alerts and [Acknowledge].** System → Alerts has three tabs: **Open** (unresolved and unacknowledged),
**Acknowledged** and **Resolved**. Each alert shows its severity, its text, its first and last time and how
often it was raised; an open one has **[Acknowledge]** (`POST /api/alerts/{id}/ack`), which records that you saw
it and, for the two pausing alerts, lifts the pause (**A paused score lane**, below); it never resolves the
alert. The header strip's "alerts ⚠ n (m critical)" counts the unacknowledged alerts and links to System,
and the banner "Unresolved critical alerts: n" shows while a critical alert is unacknowledged.

**Needs attention.** One block per section, each with its full count ("and n more" past the 50 shown, "Nothing
here" when empty); a job an item names opens in the Jobs page's drawer. A header banner about such a state
links here with **Needs attention**.

| Block | Action | What it does (API route) |
|---|---|---|
| Rejected raw items | **[Replay]**, on a replayable response; another says why it cannot be replayed | Parses the response's rejected items again with the current parser and resolves those it now reads; the toast says how many are queued (`POST /api/system/raw-items/replay {raw_response_id}`). |
| Identity conflicts | **[Keep apart]** or **[Merge]**, each behind a dialog naming the jobs ("Keep these jobs apart?", "Merge these jobs?") | Resolves the conflict (`POST /api/identity-conflicts/{id}/resolve {decision}`). |
| Refused and failed scores; the refused or failed members of Unscorable duplicate groups | **[Retry]** | Puts the target back to `pending` with its attempts reset ("Scoring again"; `POST /api/targets/{id}/retry`). |
| Quarantined batches; Batches that cannot be retrieved | **[Release & re-score]**, then **[Release & re-score]** in the dialog "Release this batch?", which states the amount that stays counted as ambiguous spend | Releases the submission and re-scores its ads (`POST /api/batches/{id}/release {"confirm": true}`). A lost batch offers it only once it is releasable; before, it reads "Can be released from <time>" (its expiry plus one hour). |
| Mass disappearances | **[Confirm closure]**, behind "Close the missing postings?" | Closes the postings missing from that board now (`POST /api/ats-snapshots/{id}/acknowledge`). |
| Paused scoring | **[Acknowledge]** | Acknowledges the pausing alert (as in Alerts). |
| Failed scope apply | **[Retry apply]** | Applies the saved search scope again (`POST /api/settings/scope/retry-apply`); the header banner "Search scope not applied" offers the same **[Retry apply]**. |
| Broken sources | none | Information: the source, its health and its last error; `degraded` is a warning. |

**The run report.** System → Runs lists the runs, newest first; **[Report]** on a run's row opens its report
below the row (status and reason, the partial causes, then Sources, Identity, Revisions, Quota and Execution;
`GET /api/runs/{id}`), and **[Report]** again closes it. While a run is active, the header's **Run in progress
(view)** opens the same report in a drawer, read again every 10 s. Score cycles lists the score cycles, newest
first (no report).

**The golden set.** System → Golden set lists the golden cases with their label and source ("Your label",
"Wrong-score report", "Synthetic (eval seed)"); **Active** and **Canary** are checkboxes (`PATCH
/api/golden-cases/{id}`; at most two canaries, re-scored daily), and **[Delete]**, behind "Delete this golden
case?", removes a case with its evaluation results (`DELETE /api/golden-cases/{id}`). Cases come from labels:
the Jobs page's label mode and the drawer's feedback.

**The eval status.** Profile & Settings → Scoring & models → **Evaluation** shows "Last eval: …" (the newest
golden run's gate, or its status while it runs or when it did not finish, with ρ) and the **Eval gate** of the
active configuration, and, while a draft changes a gated class, the gate its save would check. The page only
reads them: golden runs start from the command line (**The golden eval run on the installation before an
activation**, below).

## A paused score lane

**What you see.** No score cycle starts. The worker logs `score_cycle_not_started` with a `reason`, and a batch
preparation ends its operation with `progress = {"skipped": "paused"}`. `GET /api/system/health` shows the
reason as the score lane's `pause_reason` (with the open alerts counted by severity), and
`POST /api/system/score-cycle` (an `Idempotency-Key` header of 16 to 128 characters from `A-Z a-z 0-9 _ -`)
answers with the same `reason` at once. Scoring pauses at admission only: a cycle that is already running
finishes.

The reasons, in the order the lane checks them (`scoring.cycles.pause_reason`), and what lifts each:

| Reason | Cause | What to do |
|---|---|---|
| `reservation_bound_violated` | An open, unacknowledged `llm.reservation_bound_violated` (critical): a settled cost exceeded its reservation (§10.7). | Compare the call's usage in the alert's context with `llm/pricing.py` and the prompt overhead constant. Fix and deploy if they are wrong. Then acknowledge the alert. |
| `auth_failed` | An open, unacknowledged `llm.auth_failed` (critical): Anthropic answered 401 or 403. | Rotate or renew `anthropic_key` (§19.2), then `docker compose restart worker`: the worker reads its file secrets at start, and `up -d` would keep the running container, with the old key, when its compose configuration did not change. Then acknowledge the alert; a successful call also resolves it. |
| `cap_reached` | The month's scoring headroom is below the first reservation of an attempt (§6.9). `llm.cap_reached` (critical) is raised once per month and names any amount a quarantined batch holds. | Nothing to acknowledge: the pause lifts by itself when headroom returns (the next UTC month, or a higher cap: Profile & Settings → Budget & quota → Monthly LLM cap, then Save, which is `POST /api/settings`). Release a quarantined batch only for the reasons below, never to win headroom. |
| `global_fence` | A global purge fence is active (§20.2). | It lifts when the purge completes. The `privacy.purge_failed` entry comes with M5's runbook (spec §19.8 L6300), with the purge procedures. |
| `schema_unverified` | The active prompt-schema version and model policy have no passed live schema dry-run (§10.11). | Run the live dry-run (below). |

**Acknowledging.** The acknowledgement lifts the two alert pauses while the alert itself stays open, so the
cause stays visible (spec §17.6 L5608–5609; M3 plan Deviation 19). Acknowledge only after the cause is fixed:
the next cycle admits targets at once, and a still wrong bound or a still refused key fails its calls again.
In the UI: Profile & Settings → System → Needs attention → Paused scoring → **[Acknowledge]**, or System →
Alerts → Open → the alert's **[Acknowledge]**. Without the UI:
1. Find the alert's id with a read-only query:

   ```
   SELECT id, code, severity, first_at, last_at, occurrences, acknowledged_at FROM alerts
    WHERE code IN ('llm.auth_failed', 'llm.reservation_bound_violated') AND resolved_at IS NULL;
   ```

2. Acknowledge it: `curl -sS -b jh.cookies -H "Origin: $ORIGIN" -H 'X-JH-Request: 1' -X POST
   "$ORIGIN/api/alerts/<id>/ack"` (200 with `acknowledged_at`; 404 when no open alert has that id).
3. `GET /api/system/health` then reports no `pause_reason` (or the next one in the order above).

## A quarantined batch and its release

**What you see.** A batch submission whose create had no definite answer is `submit_ambiguous`: its targets
stay `in_batch` and its reserved spend stays counted (spec R9, §10.10). While it is quarantined, no new batch
is submitted; bulk re-scores go on through online admission. `batch_reconcile_ambiguous` (every 15 min) lists
the workspace's batches and adopts the one whose `custom_id` set equals the submission's, once it has ended.
At 25 h after the submit-start it raises `batch.submit_ambiguous_unresolved` (critical); it never moves on a
timeout. `GET /api/system/spend` shows the held amount as `quarantined_batch_usd`.

**What to do.**
1. Find the submission. The worker logged `batch_submit_ambiguous` with its `submission_id` when it became
   quarantined (and `batch_execution_lost`, with `submission_ids`, when a lost execution's recovery made it
   so). Its submit-start time and request count come from a read-only query:

   ```
   SELECT id, state, submit_started_at, request_count, reserved_usd, last_reconcile_at FROM batch_submissions
    WHERE state = 'submit_ambiguous';
   ```

2. Look for the batch in the Anthropic console (Message Batches) created within a few minutes of that
   `submit_started_at`, with that request count. A batch there will be adopted by the next pass once it has
   ended: wait for it.
3. Release only when you are certain that no such batch exists (chair decision C3: an empty listing is no
   proof):

   ```
   curl -sS -b jh.cookies -H "Origin: $ORIGIN" -H 'X-JH-Request: 1' -H 'Content-Type: application/json' \
     -H "Idempotency-Key: release-<id>-$(date +%s)" --data '{"confirm": true}' \
     "$ORIGIN/api/batches/<id>/release"
   ```

   It answers 202 with `{"submission_id", "state": "released", "reserved_usd"}`: the amount that stays counted.
   A submission that cannot be released is 409 `conflict_state` with `detail` `not_releasable`. In the UI:
   Profile & Settings → System → Needs attention → Quarantined batches → the batch's **[Release & re-score]**,
   then **[Release & re-score]** in the dialog "Release this batch?", which states the amount that stays
   counted.
4. After the release the submission is `released`: its targets are `pending` again and are re-scored, its
   reserved amount stays counted as `ambiguous`, and the alert is resolved. The reconciler keeps listing for
   29 days: if the batch turns up after all, its items settle from the provider's per-item results and publish
   nothing.

A `batch.custom_id_anomaly` (critical) means a batch shares only part of the submission's ids: it is never
adopted. Report it as a bug before any release.

## A submitted batch that cannot be retrieved (`batch.retrieve_failing`)

**What it means.** The provider accepted the batch, so its submission is `submitted` with the provider's
`provider_batch_id` and an `expires_at` 24 h after the batch's creation, by which time the provider has ended
every batch. `batch_poll` (every 5 min) retrieves the batch, and once it has ended moves the submission to
`ended` and processes its results. Each failed retrieval logs `batch_retrieve_failed` (warning) with the
`submission_id`, the `error_class` and the HTTP `status`. A retrieval that still fails past `expires_at + 1 h`
raises the critical alert `batch.retrieve_failing` and logs `batch_retrieve_failing`. The alert's `dedup_key`
is `batch.retrieve_failing:<submission id>`, and its `context` holds the `submission_id` and the
`provider_batch_id`; every later failing pass raises it again. While the submission stays `submitted`, its
targets stay `in_batch`, its reserved spend stays counted against the month, and no new batch is submitted for
the tuple (a batch preparation ends `in_flight`). The tuple's other pending targets are still scored by the
online cycles.

**What to do.**
1. Find the submission and the alert with read-only queries:

   ```
   SELECT id, state, provider_batch_id, submitted_at, expires_at, request_count, reserved_usd
     FROM batch_submissions WHERE state = 'submitted';
   SELECT id, dedup_key, context, first_at, last_at, occurrences, acknowledged_at FROM alerts
    WHERE code = 'batch.retrieve_failing' AND resolved_at IS NULL;
   ```

2. Read the worker's `batch_retrieve_failed` lines of that `submission_id` (`docker compose logs worker`).
   Their `status` says why the retrieval fails:
   - 401 or 403: the key is refused. Rotate or renew `anthropic_key` and restart the worker, as for
     `auth_failed` above.
   - no status (a connection failure), 429 or a 5xx: the network or the provider is failing. Wait: the poll
     retries every 5 minutes.
   - 404: the provider does not know the batch. Look for its `provider_batch_id` in the Anthropic console
     (Message Batches).
3. Once a retrieval succeeds and the batch has ended, the next poll moves the submission to `ended`, resolves
   the alert and processes the results as usual. Nothing else is needed.
4. A batch that stays lost (a 404, and not in the console): past `expires_at + 1 h` with the alert open,
   Profile & Settings → System → Needs attention → Batches that cannot be retrieved → **[Release &
   re-score]** and its dialog (or `POST /api/batches/{id}/release {"confirm": true}`)
   releases it; its reservation stays counted as ambiguous and the poll keeps looking for the batch for 29
   days. The request is the one of step 3 of the quarantine above, with that submission's id; it answers 202
   with the `reserved_usd` that stays counted, and 409 `conflict_state` with `detail` `not_releasable` before
   the hour has passed, without the open alert, or once the poll has moved the submission on. After the
   release the submission is `released` (its `provider_batch_id` kept), its targets are `pending` again and
   re-scored, the alert is resolved, and a new batch may be submitted. If a later retrieval finds the batch
   ended, the poll moves the submission `released → ended → reconciled`: its items settle from the provider's
   per-item results and publish nothing. A retrieval that keeps failing raises nothing more. Do not edit the
   row by hand. Acknowledging the alert (`POST /api/alerts/<id>/ack`) records that you saw it; it moves
   nothing.

## The live schema dry-run before a prompt-schema version is activated

A prompt-schema version can be activated (`POST /api/settings/prompt-schema-versions/{id}/activate`) only once
a live schema dry-run has passed for that version and every model the active model policy uses (§10.11); the
activation, a save that changes the model policy, and the score lane's `schema_unverified` pause all read the
same record. A process on the fake LLM skips the check.

1. Make sure `anthropic_key` is set (§19.2). The run sends one minimal request per structured-output schema and
   model (effort `low`, `max_tokens` 2,000); the actual cost is below $0.10 and the worst-case reservations are
   about $0.25, drawn from the non-scoring reserve.
2. Run it for the version to activate (its id from `GET /api/settings/prompt-schema-versions`):

   ```
   docker compose run --rm --no-deps worker jobhunter schema-dry-run --live --version <id>
   ```

   For a model-policy change that is not saved yet, add `--candidate-payload <settings.json>` (the document you
   are about to save); `--model-policy <id>` names an existing policy instead.
3. The output is a table of schema, model and result, and a summary line. Exit 0 means passed; 1 means a case
   failed (read the failed cases: a refused keyword or a changed transformed schema is a bug to report before
   any activation); 2 means the run could not start (the key is missing, `JOBHUNTER_FAKE_LLM` is set, or the
   candidate is invalid). The run is stored as an `eval_runs` row of kind `schema_dry_run`.
4. Activate the version. A refusal with `schema_dry_run_required` names the (schema, model) pairs still without
   a passed live case.

The same dry-run can be started from GitHub with the manual workflow `schema-dry-run-manual.yml`.

## The golden eval run on the installation before an activation

Activating a rubric or prompt-schema version, or saving a model-policy or evaluation-policy change, must pass
the eval gate of §11.13. The gate reads the newest completed golden run of the candidate (its rubric,
prompt-schema version, model policy and evaluation policy) on this installation's golden set. With fewer than
30 active cases it is skipped ("uncalibrated"); with 30 or more and no run, it fails. A failed gate can only be
overridden by acknowledging it at the activation (`eval_override_ack`), which is stored on the settings
version. A run's own gate counts only the user's cases it evaluated that are still active when it ends: a case
added, activated or deactivated while it runs never brings a run of fewer cases to 30 (it ends uncalibrated,
`passed` empty, which the gate reads as failed once 30 cases are active). Label first, then start the run.

1. If the candidate's prompt-schema version is new, run the live dry-run above first: the golden run sends the
   candidate's real requests.
2. Start the run and wait for its end. For the version to activate, name the candidate's ids
   (`<rubric>,<prompt_schema>,<model_policy>,<evaluation_policy>,<execution_config>`), or pass the settings
   document you are about to save:

   ```
   docker compose run --rm --no-deps worker jobhunter eval run --kind golden --candidate <ids> --live
   docker compose run --rm --no-deps worker jobhunter eval run --kind golden \
     --candidate-payload <settings.json> --live
   ```

   `--candidate active` evaluates the active configuration (for a baseline). The command records the run and
   its `eval_run` operation and waits while the running worker scores it on the production route: every
   active case once, then ten of them twice, through the non-scoring reserve (about $2.70 a golden run at the
   §21 figures). `--route forced_critic` adds a diagnostic run that reviews every case; it is
   reported, never gates.
3. A run the worker could not finish (a restart, a lost operation) ends `failed` with `metrics.failed =
   "interrupted"`; it is never resumed. Start a new one. A run requested `--live` on a worker in fake mode, or
   the reverse, fails at once with `"transport_mismatch"` and sends nothing.
4. Read the result: Profile & Settings → Scoring & models → Evaluation shows "Last eval: …" with the gate
   (passed, failed or not calibrated) and ρ, and the Eval gate; the run's row holds `passed`, and in its
   `metrics` the gate's state and, when it failed, `failed_gates`. Then activate. A failed gate raises
   `eval.gate_failed` (warn); do not override it without reading which gate failed.

The daily canary run (`canary_daily`, 06:41 UTC) re-scores the canary cases with the active configuration and
raises `scoring.canary_drift` (warn) for a canary more than 8 points from its first score; it needs no action
before an activation.

## The E2E suite on a development stack

The Playwright suite (spec §18.7) runs on a fresh dev stack of the four compose files `e2e.yml` uses:

```
DC="docker compose --env-file deploy/images.lock -f deploy/compose.dev.yaml -f deploy/compose.fixtures.yaml \
  -f deploy/compose.fakellm.yaml -f deploy/compose.e2e.yaml"
sudo scripts/dev-secrets.sh && $DC up -d --build --wait
npm --prefix frontend run e2e
```

A local reset of this stack that also drops the offsite stand-in's restic repository is
`$DC --profile shipper down -v`: a plain `$DC down -v` keeps the `shipper` profile's volumes `restic_repo` and
`backup_staging`.

A failed local run keeps the failed test's trace, screenshot and page snapshot in `frontend/test-results/`, and
the HTML report with the trace in `frontend/playwright-report/` (both git-ignored). The trace holds the dev
stack's session cookie and the smoke user's password, which every dev stack shares (`scripts/smoke_m0.py`):
never attach these directories or a trace of them to an issue, and never share them. CI records no trace: after
a failure it uploads the report and the test output with screenshots and page snapshots only. `npm --prefix
frontend run e2e:sentinel` scans the traces and reports that a run of its own sentinel spec retains, signed in
with invented credentials, not the artefacts a CI run uploads, and fails when one of them holds a credential.
