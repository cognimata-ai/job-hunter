// The E2E stack from a spec (plan M4 Task 32): the four-file dev stack's compose command, psql as the bootstrap
// role, the score lane's bucket, polling, the fake Batches API's script, the smoke user's credentials and E9's
// grounded job. It only runs `exec` and `run` against the running stack, never `up`, so a local override
// layered at `up` (the SPA served from the host's build) survives. A command's stdout is returned, never printed; a failure
// names the command's first words, its exit status and the tail of its stderr.
import { spawn } from "node:child_process";
import { REPO_ROOT } from "./paths";

const COMPOSE_FILES = [
  "deploy/compose.dev.yaml",
  "deploy/compose.fixtures.yaml",
  "deploy/compose.fakellm.yaml",
  "deploy/compose.e2e.yaml",
] as const;

/** `docker compose` with the digest lock and the four files of the E2E stack (project `jobhunter-dev`). */
export const DC: string[] = [
  "docker",
  "compose",
  "--env-file",
  `${REPO_ROOT}/deploy/images.lock`,
  ...COMPOSE_FILES.flatMap((file) => ["-f", `${REPO_ROOT}/${file}`]),
];

/** The fake Batches API's script file on the worker (`JOBHUNTER_FAKE_BATCH_SCRIPT`, deploy/compose.e2e.yaml). */
export const BATCH_SCRIPT_PATH = "/tmp/fake-batch-script.json";
const COMMAND_TIMEOUT_MS = 300_000; // a one-off container may start its dependencies
const POLL_MS = 1_000;
const STDERR_TAIL = 400;

export interface Ran {
  code: number;
  stdout: string;
  stderr: string;
}

export interface RunOptions {
  /** Sent to the command's stdin (and then closed). */
  input?: string;
  cwd?: string;
  timeoutMs?: number;
}

function tail(text: string): string {
  return text.trim().split("\n").slice(-3).join(" | ").slice(0, STDERR_TAIL);
}

/** `args` run in `REPO_ROOT` (or `cwd`): its exit status and output, whatever the status. */
export function run(args: readonly string[], options: RunOptions = {}): Promise<Ran> {
  const [command, ...rest] = args;
  const timeoutMs = options.timeoutMs ?? COMMAND_TIMEOUT_MS;
  return new Promise((resolve, reject) => {
    const child = spawn(command, rest, { cwd: options.cwd ?? REPO_ROOT, stdio: ["pipe", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout?.setEncoding("utf8").on("data", (chunk) => (stdout += chunk));
    child.stderr?.setEncoding("utf8").on("data", (chunk) => (stderr += chunk));
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`${command} ${rest[0] ?? ""} timed out after ${String(timeoutMs / 1000)} s`));
    }, timeoutMs);
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code: code ?? -1, stdout, stderr });
    });
    child.stdin?.on("error", () => undefined); // a command that exits before reading its stdin
    child.stdin?.end(options.input ?? "");
  });
}

/** `$DC <args>`: its stdout; a non-zero exit throws with the tail of its stderr. */
export async function compose(args: readonly string[], options: RunOptions = {}): Promise<string> {
  const ran = await run([...DC, ...args], options);
  if (ran.code !== 0) {
    throw new Error(`docker compose ${args.slice(0, 3).join(" ")} exited ${String(ran.code)}: ${tail(ran.stderr)}`);
  }
  return ran.stdout;
}

/** `sql` as the bootstrap role (`psql -tAc`): its output without the trailing line break. */
export async function psql(sql: string): Promise<string> {
  const out = await compose(["exec", "-T", "postgres", "psql", "-U", "jobhunter_bootstrap", "-d", "jobhunter", "-tAc", sql]);
  return out.trimEnd();
}

/** `expression` (over `module`, a script of scripts/) evaluated with `uv run python -c`: its text, never
 * printed. */
async function scriptValue(module: string, expression: string): Promise<string> {
  const code = `import sys; sys.path.insert(0, "scripts"); import ${module}; sys.stdout.write(str(${expression}))`;
  const ran = await run(["uv", "run", "python", "-c", code]);
  if (ran.code !== 0) {
    throw new Error(`reading ${expression} exited ${String(ran.code)}: ${tail(ran.stderr)}`);
  }
  return ran.stdout;
}

/** A constant of a smoke script (`scripts/<module>.py`). */
function scriptConstant(module: string, name: string): Promise<string> {
  return scriptValue(module, `${module}.${name}`);
}

/** E9's grounded job (`m4_seeds.grounded_job_id`): the lowest `getyourguide` candidate whose current
 * revision has its six grounded answer files on the worker and a published result, of any tuple, whose
 * description evidence is the grounded quotes. It reads no target state, so it holds after profile v2. */
export async function groundedJobId(): Promise<number> {
  const found = Number(await scriptValue("m4_seeds", "m4_seeds.grounded_job_id()"));
  if (!Number.isInteger(found) || found < 1) {
    throw new Error("m4_seeds.grounded_job_id() answered no job id");
  }
  return found;
}

/** The score lane's bucket refilled (`smoke_m3.ADVANCE_BUCKET`, Ruling T28-f): the month row's clock a month
 * back, so the next accrual fills it to its capacity. */
export async function advanceBucket(): Promise<void> {
  const advanced = await psql(await scriptConstant("smoke_m3", "ADVANCE_BUCKET"));
  if (advanced !== "1") {
    throw new Error(`the bucket was not advanced (the month has no budget row): ${advanced}`);
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** `probe()` every second until it answers something other than `null`, for at most `timeoutMs`. */
export async function waitFor<T>(probe: () => Promise<T | null>, timeoutMs: number, what: string): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const found = await probe();
    if (found !== null) {
      return found;
    }
    if (Date.now() >= deadline) {
      throw new Error(`${what} not reached within ${String(Math.round(timeoutMs / 1000))} s`);
    }
    await sleep(POLL_MS);
  }
}

/** The fake Batches API's script for the next batch create (`BatchScript` fields; deploy/compose.e2e.yaml), or
 * none (`null`: every batch follows the default script). Written to a temporary file in the same directory and
 * moved into place, so a create never reads a partial script. */
export async function writeBatchScript(script: object | null): Promise<void> {
  if (script === null) {
    await compose(["exec", "-T", "worker", "rm", "-f", BATCH_SCRIPT_PATH]);
    return;
  }
  const partial = "/tmp/.fake-batch-script.json.partial";
  await compose(["exec", "-T", "worker", "sh", "-c", `cat > ${partial} && mv -f ${partial} ${BATCH_SCRIPT_PATH}`], {
    input: JSON.stringify(script),
  });
}

export interface SmokeUser {
  username: string;
  /** `smoke_m0.PASSWORD`: never printed, never in a message. */
  password: string;
  /** `smoke_m0.USER_CREATE`: the CLI call that creates the user with `--password-stdin`. */
  create: string[];
  /** What `jobhunter user create` says when the user exists already (`smoke_m0.USER_EXISTS_MESSAGE`). */
  exists: string;
}

let smoke: Promise<SmokeUser> | undefined;

/** The smoke user of `scripts/smoke_m0.py`, read once per worker with `uv run python -c`. */
export function smokeUser(): Promise<SmokeUser> {
  smoke ??= (async () => {
    const code =
      'import json, sys; sys.path.insert(0, "scripts"); import smoke_m0 as s; sys.stdout.write(json.dumps({' +
      '"username": s.USERNAME, "password": s.PASSWORD, "create": s.USER_CREATE, "exists": s.USER_EXISTS_MESSAGE}))';
    const ran = await run(["uv", "run", "python", "-c", code]);
    if (ran.code !== 0) {
      throw new Error(`reading the smoke user exited ${String(ran.code)}: ${tail(ran.stderr)}`);
    }
    return JSON.parse(ran.stdout) as SmokeUser;
  })();
  return smoke;
}
