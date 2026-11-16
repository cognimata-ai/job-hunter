### Verdict

**APPROVE.** Re-reviewed `8812840..8255622` as `fable`. Both Important findings of round 1 are fixed, and the fixes hold against the failures they target:
- **I1 / my M2:** both rotation scripts now stage the new secret in the secrets directory and rename it over the live file. The live secret is the old file or the new one on every failure path I could reach, including a signal in the middle of the install.
- **I2:** neither helper can build a service whose resolved image is a prebuilt one. An unknown name and a missing image both fail by name before anything is built or started.
- **Minors:** all five are fixed.

I found no Critical or Important defect in the fix range. I raise four Minors, all on wording or on paths that need a second, independent failure. None blocks.

### Findings mapped

| Round-1 finding | Status | Evidence |
|---|---|---|
| Codex I1 / Fable M2: the install was not atomic | fixed | below |
| Codex I2: a prebuilt image rebuilt through a shared tag | fixed | below |
| Codex M1: "the new password only" | fixed | `scripts/rotate-restic-password.sh:139`, `:287-288`; `docs/RUNBOOK.md:585-586`; `tests/stack/test_prod_drills.py:732-735` |
| Fable M1: FU-7's refresh in no tracked file | fixed | `docs/RUNBOOK.md:1039-1046` |
| Fable M3: the drill docstring named two root commands | fixed | `tests/stack/test_prod_drills.py:44-50`, `:366-367` |
| Fable M4: the digest PR's "head commit" | fixed | `.github/workflows/release.yml:170`; `docs/RUNBOOK.md:865`, `:870-873`, `:877-882`; `tests/unit/test_workflows_m5.py:563-565` |
| Fable M5: the alert row named only the scratch database | fixed | `docs/RUNBOOK.md:427` |

**I1 / Fable M2, question by question.**

- **Where the temporary file is, and its owner, group and mode.**
  - `mktemp "$secrets_dir/.<name>.XXXXXX"` creates it in the secrets directory, so the rename stays in one directory (`rotate-restic-password.sh:184`, `:190`; `rotate-db-role.sh:131`, `:137`).
  - `install -o root -g 10010 -m 0440 <new-file> <staged>` sets owner, group and mode on the staged file, and only then `mv -f -T <staged> <secret>` runs (`rotate-restic-password.sh:191`; `rotate-db-role.sh:138`). The `&&` means a failed install never reaches the rename.
- **Removal on failure.**
  - A failed install or rename removes the staged file and clears `staged` (`rotate-restic-password.sh:195-197`; `rotate-db-role.sh:142-144`).
  - An EXIT trap covers a stop between the two steps, with INT and TERM turned into exits (`rotate-restic-password.sh:63-66`; `rotate-db-role.sh:57-60`).
  - My probe, with an `install` that writes part of the file and then sleeps:

    | Signal to the script | Exit | Live secret | Staged file left |
    |---|---|---|---|
    | TERM | 143 | old, byte-identical | no |
    | HUP | 129 | old, byte-identical | no |
    | KILL | 137 | old, byte-identical | yes (see N3) |

    My INT case was not valid: a background job of a non-interactive shell ignores SIGINT, so the signal never reached the script.
- **Can a value leak?**
  - No. `install` and `mv` take paths only, and `step` prints paths only (`rotate-restic-password.sh:173-178`).
  - Nothing is exported, and the value still reaches the one-offs by stdin or by the mounted file.
  - A leftover staged file is possible only after SIGKILL or a power loss. It sits in the secrets directory with the secret's own owner, group and mode, or empty at `mktemp`'s 0600.
- **Is the failure message truthful?**
  - restic: yes. It is reached only after step 2 named the old key, so "added and verified" is true (`rotate-restic-password.sh:262-271`).
  - `pg_bootstrap`: yes (`rotate-db-role.sh:215-219`). A run again finds the role with the new password, sets nothing and installs (`:90-93`).
  - `db_*`: yes on a first run (`rotate-db-role.sh:222-225`). On a run again after a later step failed, one clause can be wrong: see N2.
- **RUNBOOK recovery.** `docs/RUNBOOK.md:606-620` covers an installed `restic_password` found missing or damaged: install the retained new file by hand, restart backup, remove the old key by hand, or take the current password from the recovery kit when no rotation is in progress. The kit holds the old password beside the new one until the rotation ends (`:587`, `:602-603`), so that path works. One precondition is only implied: see N1.
- **Do the new tests fail without the fix?** Yes. I put the round-1 versions of the two scripts and the two helpers under the new tests in a scratch copy: 31 failed, 29 passed. The failures include the two cut-off cases for restic and the three-way cases for `pg_bootstrap` and `db_app`. The report says 24 for group A; I counted 25 in the two script files. The difference does not change the answer.

**I2, question by question.**

- **Can a service whose image is prebuilt still be built?** No, in either helper.
  - `to_build` drops every built service whose image string equals a prebuilt service's (`tests/support/prebuilt.py:55-60`).
  - `start` builds only `to_build(…)` and then runs `up --no-build` (`prebuilt.py:117-120`).
  - `prodstack.up` does the same, and its `up` already had `--no-build` (`tests/support/prodstack.py:156-161`).
  - The model is read with `--profile '*'`, so a profiled service (`restore`, `shipper`) is seen too (`prebuilt.py:94`).
  - My probe on the test stack's image mapping (`deploy/compose.test.env:10-13`):

    | Prebuilt | Built |
    |---|---|
    | `embedder` | `migrate api worker backup restore docparse` |
    | `api` | `backup restore docparse embedder` |
    | `restore` | `migrate api worker docparse embedder` |
    | `worker,backup` | `docparse embedder` |

  - No compose file sets `build.tags`, so no second tag of a build can name a prebuilt image (`deploy/compose.dev.yaml`, `deploy/compose.yaml`, `deploy/compose.test.yaml`).
  - The match is on the image string of one model, so two spellings of one image (`x` and `x:latest`) would not match. No compose file here has that.
- **Unknown name and missing image.**
  - An unknown name fails naming it and listing the services (`prebuilt.py:45-51`).
  - A missing image fails naming the service and the image (`prebuilt.py:104-106`, `:63-76`).
  - Both happen before the build, and in `prodstack.up` also when `build=False` (`prodstack.py:156`).
  - PT and PB share their compose files (`prodstack.py:62-65`), so a name valid for one is valid for the other. The S3 stand-in does not go through `up` (`prodstack.py:171-173`).

### New findings

**Critical:** none.

**Important:** none.

**Minor:**

**N1. The RUNBOOK's by-hand restic recovery does not state its precondition, and names one symptom of three.** `docs/RUNBOOK.md:608-620`.
- Failure path:
  - The text says "the script installs only after the new password's key is added and verified, so install the retained `<new file>` by hand". That holds only when an earlier run got as far as printing `the old key is <id>`.
  - If the earlier run stopped in its first one-off before `restic key add` (`rotate-restic-password.sh:246-249`) and the file was then damaged, `<new file>` opens no key. Installed by hand, it leaves backup as broken as before, and the key listing "below" fails. Nothing is lost: the old key is still in the repository and the old password is in the kit.
  - The quoted line `the installed password opens no key` appears for a wrong (truncated) password only (`rotate-restic-password.sh:89`, `:104`). A missing file stops compose before the one-off starts, and I expect an empty one to give `restic key list exited 1` (`:90`). I reasoned both from the script; I did not run restic.
- Fix: open the paragraph with the condition ("if a run printed `the old key is <id>`, …; otherwise take the old password from the kit"), and say the run may stop on a different line when the file is missing or empty.

**N2. The `db_*` install failure can misstate the role's password on a run again.** `scripts/rotate-db-role.sh:222-225`, `:238`.
- Failure path: a first run installs, alters the role, and its restart fails (line 238 is unguarded, so `set -e` exits with no message of the script's). A run again whose install then fails says "`<role>` keeps its old password; nothing was restarted". The role has the new one, and so does the file, which is "unchanged" from the first run's install. This needs two independent failures.
- Fix: say what this run did, not what the role holds: "this run changed nothing: `<file>` is as it was and nothing was restarted".

**N3. A staged file can outlive a SIGKILL or a power loss, and nothing removes or mentions it.** `scripts/rotate-restic-password.sh:190-191`; `scripts/rotate-db-role.sh:137-138`; `docs/RUNBOOK.md:578`.
- Failure path: my KILL probe left `.restic_password.XXXXXX` in the secrets directory. In production it could hold the whole new password, at root:10010 0440, the secret's own protection. A run again makes a new temporary file and leaves the old one.
- Fix: one RUNBOOK sentence ("a leftover `.<name>.*` in the secrets directory is a cut-off install: delete it"), or remove stale `.$name.??????` files at the start of `install_new`.
- Note, not a finding: the rename is not preceded by an fsync, so a power loss just after it can still leave a short file on some filesystems. Ruling F6-3 puts a file damaged from outside the script in the RUNBOOK, and `docs/RUNBOOK.md:607` names "a disk", so I do not raise it.

**N4. The new INT and TERM traps end the script without its "what is left to do" message.** `scripts/rotate-restic-password.sh:65-66`, `:274-277`; `scripts/rotate-db-role.sh:59-60`.
- Failure path:
  - A Ctrl-C or TERM during the backup restart (which can wait 10 minutes for a job, `docs/RUNBOOK.md:599`) exits 130 or 143 as soon as the child returns. Before the fix, a child that handled the signal and exited non-zero led to the `die` naming the old key and "restart it, then remove the old key by hand".
  - The state is still recoverable: the key's id was printed earlier (`:264`), and a run again reports `holds the new password already` and says the same (`:258-260`).
  - With TERM sent to the script alone, bash also waits for the running step to end before it exits.
- Fix: in the trap, print one line before exiting when the install is already done ("interrupted after the install: run it again to see what is left"), or leave it and say so in the header.

**Drift between the RUNBOOK, the scripts and the workflow.** I found none beyond N1.
- The messages the RUNBOOK quotes match the scripts: `docs/RUNBOOK.md:606-608` with `rotate-restic-password.sh:247`, `:269`; `docs/RUNBOOK.md:557-558`, `:577-581` with `rotate-db-role.sh:216-218`, `:223-224`.
- The spec's rotation line (`docs/superpowers/specs/2026-10-16-job-hunter-design.md:6526`) asks for the same `install -o root -g 10010 -m 0440` command. The scripts run that command onto the staged file, and the installed secret has the same owner, group and mode.
- The PR body, the RUNBOOK and the workflow test agree on "the commit this workflow made".

### What I ran

Every command ran with the six key variables unset. No Docker, no sudo, no network call, and nothing read under `.secrets/` or `.fixtures-raw/`. I did not read `rereview-codex.md`.

| Command | Result |
|---|---|
| `git log --oneline 8812840..8255622`; `git show` of 8fa5e0e, 74158a0 and 9bb8032 | four commits, read in full |
| `uv run --group docparse pytest -W error -q -p no:cacheprovider` on `test_rotate_restic_password_script.py`, `test_rotate_db_role_script.py`, `test_prebuilt_stack.py`, `test_workflows_m5.py` and `test_runbook.py` | 89 passed |
| The same three script and helper test files in a scratch copy (`git archive 8255622`), with the two scripts and two helpers taken from 8812840 | 31 failed, 29 passed |
| Signal probe of `rotate-restic-password.sh` in the scratchpad: a fake `compose`, an `install` that writes part of the file and sleeps, a scratch secrets directory, then TERM, HUP and KILL | the table under I1 |
| In-process probe of `prebuilt.to_build` with the image mapping of `deploy/compose.test.env` and `prodstack.BUILT_SERVICES` | the table under I2; `embeder` fails by name |

Not run: any stack test, the drill, restic itself, actionlint and gitleaks (the implementer's report covers those). The `db_*` install path has run on fakes only, as the report says; `test_rotation_drill` would run it as root on PT.
