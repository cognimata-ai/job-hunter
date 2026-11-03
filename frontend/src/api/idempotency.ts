// Keyed mutations (spec §14.1, D29): one `Idempotency-Key` per user action.
import { useMutation, type UseMutationOptions, type UseMutationResult } from "@tanstack/react-query";
import { useState } from "react";
import { type ProblemError, toProblem } from "./client";
import { isTransient } from "./queryClient";

export type KeyedMutationOptions<V, D> = Omit<UseMutationOptions<D, ProblemError, V>, "mutationFn" | "retry">;

// Each Blob (and so each File) gets an id of its own for as long as it lives: two files are one action only
// when they are the same object.
const blobIds = new WeakMap<Blob, number>();
let nextBlobId = 1;

function blobId(blob: Blob): number {
  let id = blobIds.get(blob);
  if (id === undefined) {
    id = nextBlobId++;
    blobIds.set(blob, id);
  }
  return id;
}

/** The identity of an action: its variables' JSON, a Blob or File as its object's id, a FormData as its
 * entries in order (strings as they are, files by their id). */
export function actionId(vars: unknown): string {
  // wrapped in an array, so that no variables at all (undefined) still have a JSON form
  return JSON.stringify([vars], (_name, value: unknown) => {
    if (value instanceof Blob) {
      return { blob: blobId(value) };
    }
    if (value instanceof FormData) {
      return {
        formData: [...value.entries()].map(([name, entry]) => [
          name,
          typeof entry === "string" ? entry : { blob: blobId(entry) },
        ]),
      };
    }
    return value;
  });
}

/**
 * A mutation of a keyed route: `fn` gets the action's variables and its key.
 *
 * Each action has a key of its own (`crypto.randomUUID()`), held by its identity (`actionId`): the variables
 * compared as JSON, where a Blob or a File counts as its object, so the same file object with the same
 * fields is the same action and a different file is another (the CV upload relies on this), and a FormData
 * counts as its entries in order. A key is kept after a network failure or a 5xx, so that a manual retry of
 * that action, even after other actions in between, sends it again and the server replays instead of acting
 * twice; any other answer, a success or a 4xx, drops it, and the next time that action gets a new one.
 * The mutation never retries by itself, and any failure is a `ProblemError` (`toProblem`).
 */
export function useKeyedMutation<V, D>(
  fn: (vars: V, key: string) => Promise<D>,
  opts: KeyedMutationOptions<V, D> = {},
): UseMutationResult<D, ProblemError, V> {
  const [keys] = useState(() => new Map<string, string>());
  return useMutation<D, ProblemError, V>({
    ...opts,
    retry: false,
    mutationFn: async (vars) => {
      const action = actionId(vars);
      let key = keys.get(action);
      if (key === undefined) {
        key = crypto.randomUUID();
        keys.set(action, key);
      }
      try {
        const data = await fn(vars, key);
        if (keys.get(action) === key) {
          keys.delete(action);
        }
        return data;
      } catch (error) {
        if (!isTransient(error) && keys.get(action) === key) {
          keys.delete(action);
        }
        throw toProblem(error);
      }
    },
  });
}
