// Keyed mutations (spec §14.1, D29): one `Idempotency-Key` per user action.
import { useMutation, type UseMutationOptions, type UseMutationResult } from "@tanstack/react-query";
import { useRef } from "react";
import { type ProblemError, toProblem } from "./client";
import { isTransient } from "./queryClient";

interface HeldKey {
  key: string;
  /** The action the key was made for: its variables' JSON (`undefined` for no variables). */
  action: string | undefined;
}

export type KeyedMutationOptions<V, D> = Omit<UseMutationOptions<D, ProblemError, V>, "mutationFn" | "retry">;

/**
 * A mutation of a keyed route: `fn` gets the action's variables and its key. A key is made
 * (`crypto.randomUUID()`) for each new action and kept after a network failure or a 5xx, so that a manual
 * retry of the same action (the same variables, compared as JSON) sends it again and the server replays
 * instead of acting twice; any other answer, a success or a 4xx, drops it, and the next action gets a new one.
 * The mutation never retries by itself, and any failure is a `ProblemError` (`toProblem`).
 */
export function useKeyedMutation<V, D>(
  fn: (vars: V, key: string) => Promise<D>,
  opts: KeyedMutationOptions<V, D> = {},
): UseMutationResult<D, ProblemError, V> {
  const held = useRef<HeldKey | null>(null);
  return useMutation<D, ProblemError, V>({
    ...opts,
    retry: false,
    mutationFn: async (vars) => {
      // JSON.stringify answers undefined for undefined, which its declared type leaves out
      const action: string | undefined = JSON.stringify(vars);
      let current = held.current;
      if (current?.action !== action) {
        current = { key: crypto.randomUUID(), action };
        held.current = current;
      }
      try {
        const data = await fn(vars, current.key);
        if (held.current === current) {
          held.current = null;
        }
        return data;
      } catch (error) {
        if (!isTransient(error) && held.current === current) {
          held.current = null;
        }
        throw toProblem(error);
      }
    },
  });
}
