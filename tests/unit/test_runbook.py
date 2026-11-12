"""`docs/RUNBOOK.md`, the production runbook (spec §19.8; plan M5 Task 31, R1, R6, R7, R22, R24, R30, R33,
Deviations 31 and 32).

Its sections 1 to 13 come first, under `## The production stack`, which sets `JH` once to Task 21's production
command line; the M3 and M4 sections follow with their bare compose commands. Each test reads the file and
checks it against what it documents:

- the headings: the production intro, sections 1 to 13, then the existing sections, in that order;
- every alert code of `CATALOGUE` has one row in section 6, with the catalogue's severity and resolution;
- the deploy checklist (section 1) names every secret of `deploy/secrets.example`, every environment variable
  `deploy/compose.yaml` reads, PRIVACY.md's operator fields and the deployment's other inputs, and the file
  holds nothing e-mail-shaped;
- no rotation is followed by an `up -d`: in section 7 each secret install is followed by a restart, and the
  rows of the two key alerts say `$JH restart worker`;
- every `startup-check` names its role, the role of the service it runs in, and host setup names all five;
- the offline recovery kit (section 8) lists the keys and the repository access a restore needs, the earlier
  data keys included;
- the restore (section 9) runs one `crypto rotate` pass per extra key between the restore and `up -d`;
- every compose command of the production sections is `$JH …`, and the existing sections keep theirs;
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
CHECKLIST, HOST_SETUP, _, _, _, ALERTS, ROTATION, KIT, RESTORE, _, _, _, _ = SECTIONS
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
    "noncurrent-version expiry",
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


@cache
def _text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _split(markdown: str) -> tuple[list[str], str]:
    """The logical lines of `markdown`'s fenced code blocks (a line ending in `\\` joined to the next, every
    run of whitespace one space), and its text outside them."""
    code: list[str] = []
    prose: list[str] = []
    inside = False
    pending = ""
    for line in markdown.splitlines():
        if line.strip().startswith(FENCE):
            inside = not inside
            continue
        if not inside:
            prose.append(line)
            continue
        if line.rstrip().endswith("\\"):
            pending += line.rstrip()[:-1] + " "
            continue
        code.append(" ".join((pending + line).split()))
        pending = ""
    return code, "\n".join(prose)


def _spans(prose: str) -> list[str]:
    """The inline code spans of `prose`, each on one line (a span may wrap)."""
    return [" ".join(span.split()) for span in SPAN.findall(prose)]


def _snippets(markdown: str) -> list[str]:
    """Every piece of code of `markdown`: the fenced lines, then the inline spans."""
    code, prose = _split(markdown)
    return [*code, *_spans(prose)]


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


def _jh_services(snippet: str) -> list[str]:
    """The services each `$JH …` command of `snippet` names (placeholders such as `<services>` left out)."""
    named: list[str] = []
    for match in re.finditer(r"\$JH\b", snippet):
        tokens = shlex.split(snippet[match.end() :])
        while tokens and tokens[0].startswith("-"):
            tokens = tokens[2:] if tokens[0] in COMPOSE_VALUE_OPTIONS else tokens[1:]
        if not tokens:
            continue
        subcommand, rest = tokens[0], tokens[1:]
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
    return named


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
