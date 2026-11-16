### Verdict

**APPROVE.** Re-reviewed `8812840..8255622` as `codex`. Both blocking findings are fixed: the rotation scripts stage secrets before replacement, and both stack helpers exclude services sharing a prebuilt image reference (`scripts/rotate-restic-password.sh:190`; `scripts/rotate-db-role.sh:137`; `tests/support/prebuilt.py:55`; `tests/support/prodstack.py:156`). All round-1 minors are addressed. One new Minor concerns failed cleanup of a staged secret; it does not defeat the atomic replacement. Independent verification was limited to static inspection and read-only, in-memory probes because pytest could not start in this sandbox.

### Findings mapped

- **Codex I1 / Fable M2 — fixed.** Both scripts create the temporary file inside the selected secrets directory, install it with `root:10010` ownership and mode `0440`, then rename it over the live secret. The database helper uses this path for both `pg_bootstrap` and `db_*` (`scripts/rotate-restic-password.sh:183`; `scripts/rotate-db-role.sh:130`, `:215`, `:222`). This preserves the ownership contract in `docs/superpowers/specs/2026-10-16-job-hunter-design.md:6268`.

  Ordinary install and rename failures remove the temporary file; EXIT, INT and TERM handling covers interrupted execution once its path is recorded. Cleanup is not guaranteed when removal itself fails—see N1—and traps cannot guarantee cleanup after SIGKILL or power loss (`scripts/rotate-restic-password.sh:64`, `:195`; `scripts/rotate-db-role.sh:58`, `:142`).

  The new installation commands carry paths, ownership and mode, not password values. They introduce no password expansion into argv, output or the environment. Existing container-local authentication environments remain: PostgreSQL uses `PGPASSWORD`, and restic receives S3 credentials through its environment (`scripts/rotate-restic-password.sh:81`, `:191`; `scripts/rotate-db-role.sh:69`, `:90`, `:138`). The retained source file is named accurately on the targeted installation-failure paths (`scripts/rotate-restic-password.sh:269`; `scripts/rotate-db-role.sh:216`, `:223`).

  The RUNBOOK now covers missing or damaged installed restic secrets: install the retained, previously verified new password, restart backup, then remove the recorded old key. Outside a rotation, it directs the operator to the recovery kit. The rerun decision remains unchanged, as F6-3 requires (`docs/RUNBOOK.md:606`, `:615`, `:619`; `.superpowers/sdd/2026-11-13-m5-follow-ups/progress.md:64`).

  The regression tests exercise failure after writing and failure at rename, checking unchanged live bytes, no temporary leftovers, and recovery. They therefore detect the original defect rather than merely checking command spelling (`tests/unit/test_rotate_restic_password_script.py:607`; `tests/unit/test_rotate_db_role_script.py:468`, `:476`, `:504`). My mocked shell comparison reproduced live-secret modification with the old installation statement and preservation with the new functions. The implementer’s reported RED/GREEN results are separate evidence, not independently rerun here (`.superpowers/sdd/2026-11-13-m5-follow-ups/task-F6-report.md:311`).

- **Codex I2 — fixed.** `to_build` excludes every service whose resolved image reference belongs to the prebuilt set. Both helpers use that selection and start with `--no-build`; `prodstack.up` checks presence even with `build=False` (`tests/support/prebuilt.py:55`, `:117`; `tests/support/prodstack.py:156`). Unknown names fail naming the service, and absent images fail naming both service and image, before building or starting (`tests/support/prebuilt.py:45`, `:63`, `:103`). Shared-tag tests cover both helpers and the actual development Compose YAML (`tests/unit/test_prebuilt_stack.py:104`, `:116`, `:146`). My in-memory comparison reproduced the sibling builds at `8812840` and their exclusion at the reviewed head.

- **Codex M1 — fixed.** Both restic success messages now identify the removed old key, confirm that the new password works, and state that other keys remain unchanged. The RUNBOOK agrees (`scripts/rotate-restic-password.sh:139`, `:287`; `docs/RUNBOOK.md:583`).

- **Fable M1 — fixed.** The tracked deployment checklist requires the Python/uv refresh before first deployment, the eligibility window, rebuilding, and a HIGH/CRITICAL scan. It names the deferred findings and requires recording remaining results (`docs/RUNBOOK.md:1039`).

- **Fable M3 — fixed.** The drill module names all four authorized root command shapes, their PT/test-secret scope, and M5-U6/M5-U11. The helper’s docstring agrees (`tests/stack/test_prod_drills.py:43`, `:365`).

- **Fable M4 — fixed.** The PR body identifies the commit the workflow created. The RUNBOOK prefers dispatched CI and requires any additional triggering commit to preserve the tree. This matches the workflow’s tagging of its newly created digest commit (`.github/workflows/release.yml:163`, `:170`; `docs/RUNBOOK.md:870`, `:877`; `tests/unit/test_workflows_m5.py:563`).

- **Fable M5 — fixed.** The restore-test alert row now includes failure to remove the scratch directory (`docs/RUNBOOK.md:427`).

### New findings

**Critical:** None.

**Important:** None.

**Minor N1 — Failed temporary-file removal discards the cleanup path.**

**Where:** `scripts/rotate-restic-password.sh:195` and `scripts/rotate-db-role.sh:142`; corresponding EXIT traps at `scripts/rotate-restic-password.sh:64` and `scripts/rotate-db-role.sh:58`.

**Failure path:** After an install or rename failure, `rm -f` can itself fail—for example, following a filesystem error. Both functions nevertheless clear `staged`. Because they execute under `if ! install_new`, `set -e` does not stop that sequence. The EXIT trap then sees an empty path and cannot retry. A staged file containing the new password can remain after subsequent recovery and deletion of the original source file. My in-memory shell probe reproduced one failed cleanup attempt, an empty `staged`, and a remaining temporary file in both scripts. The live secret remains unchanged.

**Fix:** Clear `staged` only after successful removal. On cleanup failure, retain it for the EXIT trap and explicitly report the leftover path and required manual removal, without printing its contents. Add a cleanup-failure regression alongside the current install/rename failure cases (`tests/unit/test_rotate_restic_password_script.py:607`; `tests/unit/test_rotate_db_role_script.py:468`).

### What I ran

- Read the round-1 reviews, F6-1 through F6-5, the fix-round report and concerns, relevant revision-3.7 spec clauses, and the four commits in `git log --oneline 8812840..8255622`.
- `git diff --check 8812840..8255622` — passed.
- `bash -n scripts/rotate-restic-password.sh scripts/rotate-db-role.sh` — passed.
- Twelve in-memory prebuilt-helper checks using mocked subprocess calls, including actual development Compose YAML, shared tags, unknown names, missing images, and `build=False`.
- Fourteen shell decision checks with filesystem operations mocked, covering successful installation, install failures, rename failure, TERM cleanup, failed cleanup, and the old direct-install failure.
- Attempted the prescribed focused pytest command for the rotation, prebuilt, workflow and RUNBOOK unit files, with offline mode and bytecode writes disabled. **No tests executed:** uv failed creating its cache-lock temporary file on the read-only filesystem.
- Reviewed the reported operational drill as supplied evidence. The report explicitly says the changed `db_*` path ran only on fakes in that verification round (`.superpowers/sdd/2026-11-13-m5-follow-ups/task-F6-report.md:375`, `:398`).

No files were written. No Docker, sudo, network, paid calls or sub-agents were used. Nothing under `.secrets/` or `.fixtures-raw/` was read, listed or searched.