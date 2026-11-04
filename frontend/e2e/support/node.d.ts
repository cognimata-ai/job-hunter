// The few Node APIs the E2E harness uses (plan M4 Task 32). This browser project installs no @types/node, so
// they are written out here, as vite.config.ts writes out the proxy types it uses: only the members the
// harness calls, under the `node:` names it imports them by.
declare module "node:child_process" {
  interface ReadableText {
    setEncoding(encoding: "utf8"): this;
    on(event: "data", listener: (chunk: string) => void): this;
  }
  interface WritableText {
    end(data?: string): void;
    on(event: "error", listener: (error: Error) => void): this;
  }
  export interface ChildProcess {
    readonly stdout: ReadableText | null;
    readonly stderr: ReadableText | null;
    readonly stdin: WritableText | null;
    kill(signal?: "SIGKILL" | "SIGTERM"): boolean;
    on(event: "error", listener: (error: Error) => void): this;
    on(event: "close", listener: (code: number | null) => void): this;
  }
  export interface SpawnOptions {
    cwd?: string;
    stdio?: ["pipe", "pipe", "pipe"];
  }
  export function spawn(command: string, args: readonly string[], options?: SpawnOptions): ChildProcess;
}

declare module "node:url" {
  export function fileURLToPath(url: string | URL): string;
}

declare module "node:process" {
  interface Process {
    readonly env: Readonly<Record<string, string | undefined>>;
    readonly stdout: { write(text: string): boolean };
  }
  const process: Process;
  export default process;
}
