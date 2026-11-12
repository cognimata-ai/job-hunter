"""`docs/RUNBOOK.md`, the production runbook (spec §19.8; plan M5 Task 31, R1, R6, R7, R22, R24, R30, R33,
Deviations 31 and 32).

Its sections 1 to 13 come first, under `## The production stack`, which sets `JH` once to Task 21's production
command line; the M3 and M4 sections follow with their bare compose commands. Each test reads the file and
checks it against what it documents:

- the headings: the production intro, sections 1 to 13, then the existing sections, in that order;
- every alert code of `CATALOGUE` has one row in section 6, with the catalogue's severity and resolution;
- the deploy checklist (section 1) names every secret of `deploy/secrets.example`, every environment variable
  `deploy/compose.yaml` reads, PRIVACY.md's operator fields and the deployment's other inputs, the bucket's
  noncurrent-version expiry is 30 days and no other (Ruling T32-2), and the file holds nothing e-mail-shaped;
- no rotation is followed by an `up -d`: in section 7 each secret install is followed by a restart, and the
  rows of the two key alerts say `$JH restart worker`;
- every `startup-check` names its role, the role of the service it runs in, and host setup names all five;
- the offline recovery kit (section 8) lists the keys and the repository access a restore needs, the earlier
  data keys included;
- the restore (section 9) runs one `crypto rotate` pass per extra key between the restore and `up -d`;
- every compose command of the production sections is `$JH …`, and the existing sections keep theirs;
- a one-off `$JH run` whose service depends on `migrate` takes `--no-deps`, and api and worker are stopped
  before any command runs `migrate` (§19.5; Ruling T31-3 M11);
- no data volume is removed before section 8's procedure ships the manifests and checks that none is left, and
  section 10 states the manifest format's expand/contract rule (Rulings T31-2, T31-3 I1 and I3); section 8's
  procedure stops api and worker before it ships and checks, and each block that removes a volume does too,
  in that order, before the removal (Rulings T31-5, T31-6);
- V21 is the N1 stack test on the VPS, a skip counting as not passed, and its hand probes of api and docparse
  connect to a literal address (Rulings T31-3 I2, T31-5);
- every `scripts/…` path exists and takes the flags given to it, every `jobhunter …` command is a command of
  the CLI's parser with the options given to it, and every service a `$JH` command names is a service of
  `deploy/compose.yaml`.
"""

import argparse
import re
import shlex
from functools import cache
from pathlib import Path
from typing import Any, Final, get_args

import yaml
from jobhunter import cli
from jobhunter.config import Role
from jobhunter.observability.catalogue import CATALOGUE, RESOLVE_ON_ACK

REPO: Final = Path(__file__).resolve().parents[2]
RUNBOOK: Final = REPO / "docs/RUNBOOK.md"
COMPOSE: Final = REPO / "deploy/compose.yaml"
SECRETS_EXAMPLE: Final = REPO / "deploy/secrets.example"
PRIVACY: Final = REPO / "docs/PRIVACY.md"

PRODUCTION: Final = "## The production stack"
SECTIONS: Final = (
    "## 1. Deploy checklist",
    "## 2. Host setup",
    "## 3. First start",
    "## 4. Start and stop, and a frozen worker",
    "## 5. Health check",
    "## 6. Alerts",
    "## 7. Secret rotation",
    "## 8. Backups and the offline recovery kit",
    "## 9. Restore",
    "## 10. Upgrade and rollback",
    "## 11. Disk pressure and a lost password",
    "## 12. The embedder model",
    "## 13. The deployment step",
)
CHECKLIST, HOST_SETUP, _, _, _, ALERTS, ROTATION, KIT, RESTORE, UPGRADE, _, _, DEPLOYMENT = SECTIONS
# The M3 and M4 sections, after the new ones, in their order.
EXISTING: Final = (
    "## The web UI's operator paths",
    "## A paused score lane",
    "## A quarantined batch and its release",
    "## A submitted batch that cannot be retrieved (`batch.retrieve_failing`)",
    "## The live schema dry-run before a prompt-schema version is activated",
    "## The golden eval run on the installation before an activation",
    "## The E2E suite on a development stack",
)

ALERT_HEADER: Final = ["Alert", "Severity", "Resolves", "Action"]
# The two rows whose action is a key's rotation (R6: a restart, never an `up -d`).
KEY_ROWS: Final = ("source.auth_failed", "llm.auth_failed")
# The deployment's inputs the checklist names besides the secrets and the environment (plan Task 31, item 1).
INPUTS: Final = (
    "ntfy server",
    "one check for the collection schedule",
    "UUID",
    "read-write",
    "alert integration",
    "EU workspace",
    "`jobhunter-prod`",
    "trace retention",
    "EU region",
    "4 vCPU",
    "16 GB",
    "100 GB NVMe",
    "OS image",
    "LUKS",
    "Docker Engine",
    "N1",
    "A record",
    "ACME e-mail",
    "S3",
    "bucket versioning",
    "noncurrent-version expiry of 30 days",  # Ruling T32-2: the offsite lag of deleted data is about 4 months
    "Object Lock",
    "compliance mode",
    "SSH",
    "key-only",
    "no Claude session",
    "GHCR",
    "`release.yml`",
    "digest PR",
    "release tag",
    "offline recovery kit",
    "fp32",
    "int8",
    "V7",
    "SerpApi account",
    "C2",
    "Anthropic workspace",
    "C3",
)
# What the kit holds besides the secrets the restore service mounts (spec §19.8; R1, Deviation 31).
KIT_ITEMS: Final = (
    "the current `data_key`",
    "every earlier data key",
    "key id",
    "`data_key_ids`",
    "`RESTIC_REPOSITORY`",
    "`/etc/jobhunter/jobhunter.env`",
    "offline",
    "3 months and a week",
)
# `jobhunter restore run`'s line and `crypto rotate`'s outcomes (`cli_backup._restore_run`, `crypto_rotate`).
RESTORE_OUTPUT: Final = ('"restored"', '"set_id"', '"data_key_ids"')
ROTATE_OUTCOMES: Final = ("one pass per key id", "exit 3", "`other_keys_remain`", "exit 0")

FENCE: Final = "```"
SPAN: Final = re.compile(r"`([^`]+)`")
EMAIL: Final = re.compile(r"[\w.%+-]+@[\w-]+(\.[\w-]+)+")
OPERATOR_FIELD: Final = re.compile(r"\[set at deployment: ([^\]\n]+)\]")
COMPOSE_VARIABLE: Final = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?::[?-][^}]*)?\}")
INSTALL: Final = "install -o root -g 10010 -m 0440"
PREVIOUS: Final = "/etc/jobhunter/secrets/data_key_previous"
SECRET_INSTALL: Final = re.compile(rf"\b{INSTALL} .+ /etc/jobhunter/secrets/")
UP_DETACHED: Final = re.compile(r"\bup -d\b")
STARTUP_CHECK: Final = re.compile(r"run --rm --no-deps (\S+) jobhunter startup-check --role (\S+)")
SCRIPT: Final = re.compile(r"(?<![\w/.-])(scripts/[\w./-]+)")
COMMAND: Final = re.compile(r"(?<![\w/.-])jobhunter ([a-z][\w-]*(?: [^\s;|&]+)*)")
LONG_OPTION: Final = re.compile(r"(?<!\S)(--[a-z][a-z-]*)")
# A placeholder such as `<dir>` or `<new file>`, replaced before a command is split at its shell separators.
PLACEHOLDER: Final = re.compile(r"<[^<>\n]+>")
SEPARATOR: Final = re.compile(r"&&|\|\||[;|<>]")
SEPARATORS: Final = frozenset({"&&", "||", "|", ";", ">"})
# Compose's options that take a value, before or after its subcommand.
COMPOSE_VALUE_OPTIONS: Final = frozenset(
    {"--profile", "--since", "--tail", "-t", "--timeout", "-u", "-e", "-w"}
)
# The subcommands whose first operand is the service (the rest is its command).
ONE_SERVICE: Final = frozenset({"run", "exec"})
ONE_OFF_RUN: Final = re.compile(r"\$JH (?:--profile \S+ )?run\b")
MIGRATING: Final = re.compile(r"\bjobhunter (?:migrate|manifests replay)\b")
# A run of the migrate service with its own command (`… migrate jobhunter startup-check …` runs no migration).
OWN_MIGRATE: Final = re.compile(r"\$JH (?:--profile \S+ )?run(?: -\S+)* migrate")
APP: Final = frozenset({"api", "worker"})
# Section 8's procedure before a wipe, and what removes the manifests volume (Ruling T31-3 I1).
BEFORE_A_WIPE: Final = "**Before any volume is removed**"
VOLUME_REMOVAL: Final = re.compile(r"docker volume rm .*jobhunter_manifests|\$JH down -v|\bwiped\b")
SHIP: Final = "$JH exec backup jobhunter backup ship"
STOP_APP: Final = "$JH stop api worker"
VOLUME_RM: Final = re.compile(r"\bdocker volume rm\b")
# A top-level item of a numbered list: a block that removes a volume starts at the item it is in.
ITEM: Final = re.compile(r"^\d+\. ")
SKIP_FAILS: Final = "a skipped test counts as not passed"
# V21 on the VPS (Ruling T31-3 I2): the N1 stack test, and hand probes that connect to a literal address.
N1: Final = "tests/stack/test_prod_network.py"
ADDRESS_PROBE: Final = re.compile(r"create_connection\(\('\d{1,3}(?:\.\d{1,3}){3}', \d+\)")


@cache
def _text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _lines(markdown: str) -> list[tuple[bool, str]]:
    """`markdown`'s lines in order, each with whether it is code: a fenced line is one logical line (a line
    ending in `\\` joined to the next, every run of whitespace one space), the fences themselves left out."""
    lines: list[tuple[bool, str]] = []
    inside = False
    pending = ""
    for line in markdown.splitlines():
        if line.strip().startswith(FENCE):
            inside = not inside
        elif not inside:
            lines.append((False, line))
        elif line.rstrip().endswith("\\"):
            pending += line.rstrip()[:-1] + " "
        else:
            lines.append((True, " ".join((pending + line).split())))
            pending = ""
    return lines


def _split(markdown: str) -> tuple[list[str], str]:
    """The logical lines of `markdown`'s fenced code blocks, and its text outside them."""
    lines = _lines(markdown)
    return [text for code, text in lines if code], "\n".join(text for code, text in lines if not code)


def _spans(prose: str) -> list[str]:
    """The inline code spans of `prose`, each on one line (a span may wrap)."""
    return [" ".join(span.split()) for span in SPAN.findall(prose)]


def _snippets(markdown: str) -> list[str]:
    """Every piece of code of `markdown`, in document order: its fenced lines and its inline spans."""
    lines = _lines(markdown)
    assert not [text for code, text in lines if code and "`" in text], "a fenced line with a backtick"
    return _spans("\n".join(f"`{text}`" if code else text for code, text in lines))


def _headings() -> list[str]:
    return [line for line in _split(_text())[1].splitlines() if line.startswith("## ")]


def _region(start: str, end: str | None) -> str:
    """The lines from the heading `start` (excluded) to the heading `end` (excluded; `None`: the end of the
    file), headings being `## ` lines outside code fences."""
    body: list[str] = []
    inside = False
    found = False
    for line in _text().splitlines():
        if line.strip().startswith(FENCE):
            inside = not inside
        elif not inside and line.startswith("## "):
            if found and line == end:
                break
            if line == start:
                found = True
                continue
        if found:
            body.append(line)
    assert found, f"no heading {start!r}"
    return "\n".join(body)


def _section(heading: str) -> str:
    """The body of a section of sections 1 to 13."""
    following = [*SECTIONS[1:], EXISTING[0]][SECTIONS.index(heading)]
    return _region(heading, following)


def _alert_rows() -> dict[str, list[str]]:
    """Section 6's table, by alert code: each row's cells."""
    rows: dict[str, list[str]] = {}
    lines = [line for line in _section(ALERTS).splitlines() if line.startswith("|")]
    assert lines, "section 6 has no table"
    assert [cell.strip() for cell in lines[0].strip("|").split("|")] == ALERT_HEADER
    for line in lines[2:]:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        assert len(cells) == len(ALERT_HEADER), f"a row of {len(cells)} cells: {line}"
        code = re.fullmatch(r"`([a-z_]+(?:\.[a-z_0-9]+)+)`", cells[0])
        assert code is not None, f"a row without an alert code: {line}"
        assert code[1] not in rows, f"{code[1]} has two rows"
        rows[code[1]] = cells
    return rows


@cache
def _compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return loaded


def _secret_names() -> list[str]:
    lines = SECRETS_EXAMPLE.read_text(encoding="utf-8").splitlines()
    return [line.split("#", 1)[0].split()[0] for line in lines if line.split("#", 1)[0].strip()]


def _compose_variables() -> set[str]:
    """The environment variables `deploy/compose.yaml` reads (the upper-case ones: the image variables of the
    two lock files are lower-case), its comments left out."""
    names: set[str] = set()
    for line in COMPOSE.read_text(encoding="utf-8").splitlines():
        names.update(COMPOSE_VARIABLE.findall(line.split("#", 1)[0]))
    return names


def _prod_command() -> str:
    """Task 21's production command line, from `deploy/compose.yaml`'s header, without its `up -d --wait`."""
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("#   docker compose -p jobhunter "))
    parts: list[str] = []
    for line in lines[start:]:
        part = line.removeprefix("#").strip()
        parts.append(part.removesuffix("\\"))
        if not part.endswith("\\"):
            break
    command = " ".join(" ".join(parts).split())
    assert command.endswith(" up -d --wait"), command
    return command.removesuffix(" up -d --wait")


def _named(name: str, text: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text) is not None


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    return {
        name: sub
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
        for name, sub in action.choices.items()
    }


def _check_command(parser: argparse.ArgumentParser, words: list[str]) -> None:
    """`jobhunter <words>` is a complete command of `parser`, and each long option given is one of its."""
    current = parser
    path: list[str] = []
    rest = list(words)
    while (subcommands := _subcommands(current)) and rest:
        word = rest.pop(0)
        path.append(word)
        assert word in subcommands, f"`jobhunter {' '.join(path)}` is no command of the CLI"
        current = subcommands[word]
    assert not _subcommands(current), f"`jobhunter {' '.join(path)}` names no complete command"
    for option in LONG_OPTION.findall(" ".join(rest)):
        assert option in current._option_string_actions, f"`jobhunter {' '.join(path)}` has no {option}"


def _compose_calls(snippet: str) -> list[tuple[str, list[str]]]:
    """Each `$JH …` command of `snippet`: its subcommand and the services it names (placeholders such as
    `<services>` left out)."""
    calls: list[tuple[str, list[str]]] = []
    for match in re.finditer(r"\$JH\b", snippet):
        tokens = shlex.split(snippet[match.end() :])
        while tokens and tokens[0].startswith("-"):
            tokens = tokens[2:] if tokens[0] in COMPOSE_VALUE_OPTIONS else tokens[1:]
        if not tokens:
            continue
        subcommand, rest = tokens[0], tokens[1:]
        named: list[str] = []
        index = 0
        while index < len(rest) and rest[index] not in SEPARATORS and rest[index] != "$JH":
            token = rest[index]
            if token.startswith("-"):
                index += 2 if token in COMPOSE_VALUE_OPTIONS else 1
                continue
            if not token.startswith("<"):
                named.append(token)
            if subcommand in ONE_SERVICE:
                break
            index += 1
        calls.append((subcommand, named))
    return calls


def _jh_services(snippet: str) -> list[str]:
    """The services the `$JH …` commands of `snippet` name."""
    return [service for _, services in _compose_calls(snippet) for service in services]


def _depends_on(service: str) -> set[str]:
    """Every service `service` depends on in `deploy/compose.yaml`, directly or not."""
    services = _compose()["services"]
    found: set[str] = set()
    pending = [service]
    while pending:
        for dependency in services[pending.pop()].get("depends_on", {}):
            if dependency not in found:
                found.add(dependency)
                pending.append(dependency)
    return found


def test_the_new_sections_come_first_in_order() -> None:
    assert _headings() == [PRODUCTION, *SECTIONS, *EXISTING]


def test_every_catalogue_code_has_a_row() -> None:
    rows = _alert_rows()
    assert set(rows) == set(CATALOGUE), {
        "without a row": sorted(set(CATALOGUE) - set(rows)),
        "not in the catalogue": sorted(set(rows) - set(CATALOGUE)),
    }
    for code, entry in CATALOGUE.items():
        _, severity, resolves, action = rows[code]
        escalation = "" if entry.escalates_to is None else f" / {entry.escalates_to}"
        if entry.auto_resolves:
            resolution = "auto"
        elif code in RESOLVE_ON_ACK:
            resolution = "manual, by its acknowledgement"
        else:
            resolution = "manual"
        assert (severity, resolves) == (f"{entry.severity}{escalation}", resolution), code
        assert action, f"{code} has no action"


def test_the_deploy_checklist_names_every_deployment_input() -> None:
    checklist = _section(CHECKLIST)
    secrets = _secret_names()
    assert len(secrets) == 20, secrets
    assert [name for name in secrets if not _named(name, checklist)] == []
    variables = _compose_variables()
    assert "NTFY_TOPIC_URL" in variables and "HEALTHCHECKS_API_URL" in variables
    assert sorted(name for name in variables if not _named(name, checklist)) == []
    fields = OPERATOR_FIELD.findall(PRIVACY.read_text(encoding="utf-8"))
    assert len(fields) == 5, fields
    assert [field for field in fields if field not in checklist] == []
    assert [phrase for phrase in INPUTS if phrase not in checklist] == []
    bucket = [line for line in checklist.splitlines() if "noncurrent-version expiry" in line]
    assert len(bucket) == 1, bucket
    assert re.findall(r"\b(\d+) days\b", bucket[0]) == ["30"], "the deleted versions' expiry is 30 days only"
    assert EMAIL.search(_text()) is None, "no e-mail address: the ACME e-mail is a placeholder"


def test_no_up_d_follows_a_rotation() -> None:
    code, prose = _split(_section(ROTATION))
    commands = [*code, *(span for span in _spans(prose) if span.startswith("$JH"))]
    assert [command for command in commands if UP_DETACHED.search(command)] == []
    installs = [index for index, line in enumerate(code) if SECRET_INSTALL.search(line)]
    assert installs, "section 7 installs no secret"
    for index in installs:
        following = next((line for line in code[index + 1 :] if "$JH" in line), "")
        assert following.startswith("$JH restart "), f"after `{code[index]}`: `{following}`"
    rows = _alert_rows()
    for alert in KEY_ROWS:
        action = rows[alert][3]
        assert "`$JH restart worker`" in action, alert
        assert not UP_DETACHED.search(action), alert


def test_startup_check_always_names_its_role() -> None:
    roles = set(get_args(Role))
    snippets = _snippets(_text())
    assert _text().count("startup-check") == sum(snippet.count("startup-check") for snippet in snippets), (
        "every `startup-check` is a command"
    )
    for snippet in snippets:
        checks = STARTUP_CHECK.findall(snippet)
        assert len(checks) == snippet.count("startup-check"), f"a startup-check without its role: {snippet}"
        for service, role in checks:
            assert role in roles and service == role, snippet
    host_setup = {
        role for snippet in _snippets(_section(HOST_SETUP)) for _, role in STARTUP_CHECK.findall(snippet)
    }
    assert host_setup == roles


def test_the_recovery_kit_lists_its_items() -> None:
    kit = _section(KIT)
    restore_secrets = _compose()["services"]["restore"]["secrets"]
    # The database passwords are made anew on a fresh host (its postgres init creates the roles); the data key
    # is the api's and the worker's, and only it opens the restored ciphertexts.
    needed = ["data_key", *(name for name in restore_secrets if not name.startswith("db_"))]
    assert [name for name in needed if not _named(name, kit)] == []
    assert [item for item in KIT_ITEMS if item not in kit] == []


def test_the_restore_rotates_once_per_extra_key_before_up() -> None:
    restore = _section(RESTORE)
    code, prose = _split(restore)

    def first(predicate: str) -> int:
        return next(index for index, line in enumerate(code) if re.search(predicate, line))

    postgres = first(r"^\$JH up -d .*\bpostgres$")
    restored = first(r"^\$JH --profile restore run --rm restore$")
    previous = first(rf"^{INSTALL} (?!/dev/null).+ {PREVIOUS}$")
    rotate = first(r"^\$JH run --rm --no-deps worker jobhunter crypto rotate$")
    emptied = first(rf"^{INSTALL} /dev/null {PREVIOUS}$")
    ups = [index for index, line in enumerate(code) if UP_DETACHED.search(line) and index != postgres]
    assert ups, "the stack never comes up"
    assert postgres < restored < previous < rotate < emptied < min(ups)
    assert [phrase for phrase in (*RESTORE_OUTPUT, *ROTATE_OUTCOMES) if phrase not in restore] == []
    assert "`data_key_previous`" in prose


def test_every_compose_command_uses_jh() -> None:
    production = _region(PRODUCTION, EXISTING[0])
    snippets = _snippets(production)
    composes = [snippet for snippet in snippets if "docker compose" in snippet]
    assert composes == [f'JH="{_prod_command()}"'], composes
    assert "cd /opt/jobhunter" in snippets
    assert sum(snippet.startswith("$JH ") for snippet in snippets) > 0
    existing = _region(EXISTING[0], None)
    assert "$JH" not in existing, "the existing sections are unchanged"
    assert "docker compose run --rm --no-deps worker jobhunter schema-dry-run --live --version <id>" in (
        _snippets(existing)
    )


def test_every_named_script_and_command_exists() -> None:
    parser = cli._build_parser()
    services = set(_compose()["services"])
    scripts = commands = 0
    for snippet in _snippets(_text()):
        plain = PLACEHOLDER.sub("_", snippet)
        for match in SCRIPT.finditer(plain):
            path = REPO / match[1]
            assert path.is_file(), f"no {match[1]}"
            tail = SEPARATOR.split(plain[match.end() :], maxsplit=1)[0]
            text = path.read_text(encoding="utf-8")
            assert [option for option in LONG_OPTION.findall(tail) if option not in text] == [], snippet
            scripts += 1
        for match in COMMAND.finditer(plain):
            words = SEPARATOR.split(match[1], maxsplit=1)[0].split()
            _check_command(parser, words)
            commands += 1
        assert [name for name in _jh_services(snippet) if name not in services] == [], snippet
    assert scripts and commands


def test_a_one_off_run_never_starts_migrate_beside_the_stack() -> None:
    """Without `--no-deps`, Compose starts a run's dependencies again: a one-off run of a service that depends
    on `migrate` (api, worker) takes `--no-deps`, so `migrate` never runs beside a live api and worker."""
    runs = 0
    for heading in SECTIONS:
        for snippet in _snippets(_section(heading)):
            services = _jh_services(snippet) if ONE_OFF_RUN.match(snippet) else []
            if not services:
                continue
            (service,) = services
            runs += 1
            if "migrate" in _depends_on(service):
                options = snippet.split(f" {service} ", 1)[0]
                assert "--no-deps" in options.split(), f"{heading}: `{snippet}`"
    assert runs


def test_api_and_worker_stop_before_migrate() -> None:
    """Within each section, in document order, a command that runs `migrate` or `manifests replay` follows a
    stop of api and worker, with no command between that starts either again."""
    migrations = 0
    for heading in SECTIONS:
        stopped = False
        for snippet in _snippets(_section(heading)):
            for subcommand, named in _compose_calls(snippet):
                services = set(named)
                if subcommand in ("stop", "down") and (not services or APP <= services):
                    stopped = True
                elif subcommand in ("up", "start", "restart") and (not services or services & APP):
                    stopped = False
                if OWN_MIGRATE.fullmatch(snippet) or MIGRATING.search(snippet):
                    migrations += 1
                    assert stopped, f"{heading}: `{snippet}` while api and worker may run"
    assert migrations


def _wipe_blocks(markdown: str) -> list[list[tuple[bool, str]]]:
    """Each block of `markdown` that removes a volume: the lines (`_lines`) from the top-level list item the
    fenced `docker volume rm` is in (else the start) to that line."""
    lines = _lines(markdown)
    blocks: list[list[tuple[bool, str]]] = []
    for index, (code, text) in enumerate(lines):
        if code and VOLUME_RM.search(text):
            items = [i for i in range(index) if not lines[i][0] and ITEM.match(lines[i][1])]
            blocks.append(lines[(items[-1] if items else 0) : index + 1])
    return blocks


def _wipe_events(lines: list[tuple[bool, str]]) -> list[str]:
    """The order of `lines`' steps before a wipe: `stop` (api and worker), `ship`, `check` (the unshipped-
    manifest query, or a reference to section 8's procedure) and `removal` (a fenced `docker volume rm`)."""
    events: list[str] = []
    for code, text in lines:
        if code:
            pieces = [text]
        else:
            found = [(match.start(), " ".join(match[1].split())) for match in SPAN.finditer(text)]
            found += [(match.start(), BEFORE_A_WIPE) for match in re.finditer(re.escape(BEFORE_A_WIPE), text)]
            pieces = [piece for _, piece in sorted(found)]
        for piece in pieces:
            if piece == BEFORE_A_WIPE or "manifest_shipment_items" in piece:
                events.append("check")
            elif piece == SHIP:
                events.append("ship")
            elif piece.startswith("$JH stop ") and APP <= set(_jh_services(piece)):
                events.append("stop")
            elif code and VOLUME_RM.search(piece):
                events.append("removal")
    return events


def test_each_volume_removal_stops_ships_and_checks_first() -> None:
    """Section 8's procedure stops api and worker, then ships, then checks; each block that removes a volume
    does the same before the removal, a ship before its check included, so no path relies on an earlier stop
    or ship (Rulings T31-5, T31-6)."""
    kit = _section(KIT)
    assert BEFORE_A_WIPE in kit, "section 8 has no procedure before a wipe"
    procedure = _wipe_events(_lines(kit[kit.index(BEFORE_A_WIPE) + len(BEFORE_A_WIPE) :]))
    in_order = sorted({"stop", "ship", "check"} & set(procedure), key=procedure.index)
    assert in_order == ["stop", "ship", "check"], f"section 8's procedure: {procedure}"
    blocks = [block for heading in SECTIONS for block in _wipe_blocks(_section(heading))]
    assert blocks, "no block removes a volume"
    for block in blocks:
        events = _wipe_events(block)
        before = events[: events.index("removal")]
        assert "check" in before, f"{events}: no check before the removal"
        assert "ship" in before[: before.index("check")], f"{events}: no ship before the check (Ruling T31-6)"
        first = min(before.index(step) for step in ("ship", "check") if step in before)
        assert "stop" in before[:first], f"{events}: api and worker are not stopped before the ship and check"


def test_no_volume_is_removed_before_the_manifests_are_shipped() -> None:
    kit = _section(KIT)
    assert BEFORE_A_WIPE in kit, "section 8 has no procedure before a wipe"
    procedure = kit[kit.index(BEFORE_A_WIPE) :]
    snippets = _snippets(procedure)
    ship = snippets.index(SHIP)
    check = next(index for index, snippet in enumerate(snippets) if "manifest_shipment_items" in snippet)
    assert ship < check, "the check of the latest shipment follows the ship"
    assert [p for p in ("backup_job_busy", "run it again", "remove no volume") if p not in procedure] == []
    removals = 0
    for heading in SECTIONS:
        text = _section(heading)
        for match in VOLUME_REMOVAL.finditer(text):
            removals += 1
            assert BEFORE_A_WIPE in text[: match.start()], f"{heading}: `{match[0]}` before {BEFORE_A_WIPE}"
    assert removals
    upgrade = _section(UPGRADE)
    assert [p for p in ("expand/contract", "remove no volume", "roll forward") if p not in upgrade] == []


def test_v21_runs_n1_on_the_vps_and_probes_connecting_out_by_address() -> None:
    assert (REPO / N1).is_file()
    snippets = _snippets(_section(DEPLOYMENT))
    n1 = [s for s in snippets if s.startswith("JOBHUNTER_N1_OUTSIDE=1 ") and "pytest -m stack" in s]
    assert [s for s in n1 if s.endswith(f" {N1}")], "V21 runs the N1 stack test with the outside reach"
    assert SKIP_FAILS in " ".join(_section(DEPLOYMENT).split()), "a skipped N1 test is no pass"
    probes = [s for s in snippets if re.match(r"\$JH exec -T (?:api|docparse) ", s)]
    assert {probe.split()[3] for probe in probes} == {"api", "docparse"}
    assert [probe for probe in probes if not ADDRESS_PROBE.search(probe)] == [], "connect to an address"
