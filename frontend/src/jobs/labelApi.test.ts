import { afterEach, describe, expect, it, vi } from "vitest";
import { shuffle } from "./labelApi";

afterEach(() => {
  vi.restoreAllMocks();
});

/** `crypto.getRandomValues` filling a Uint32Array with `draws`, in order. */
function drawing(draws: number[]) {
  return vi.spyOn(crypto, "getRandomValues").mockImplementation(<T extends ArrayBufferView | null>(array: T): T => {
    if (array instanceof Uint32Array) {
      array.set(draws.slice(0, array.length));
    }
    return array;
  });
}

describe("shuffle", () => {
  it("is Fisher–Yates over crypto.getRandomValues, into a new array", () => {
    // from the end: i = 3 swaps with 7 % 4 = 3 (itself), i = 2 with 5 % 3 = 2 (itself), i = 1 with 4 % 2 = 0
    const random = drawing([0, 4, 5, 7]);
    const items = ["a", "b", "c", "d"];
    expect(shuffle(items)).toEqual(["b", "a", "c", "d"]);
    expect(random).toHaveBeenCalledTimes(1); // one draw per item, at once
    expect(items).toEqual(["a", "b", "c", "d"]); // the input is not reordered
    // all draws 0: each place from the end swaps with the first
    drawing([0, 0, 0, 0]);
    expect(shuffle(items)).toEqual(["b", "c", "d", "a"]);
  });

  it("gives every order of a batch, each about as often", () => {
    const counts = new Map<string, number>();
    const runs = 6000;
    for (let i = 0; i < runs; i++) {
      const key = shuffle([1, 2, 3]).join("");
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    expect([...counts.keys()].sort()).toEqual(["123", "132", "213", "231", "312", "321"]);
    for (const count of counts.values()) {
      expect(count).toBeGreaterThan(runs / 6 / 2); // far from the 1,000 each only by a broken shuffle
    }
    expect(shuffle([])).toEqual([]);
  });
});
