// A value that follows another `ms` after its last change (the place and company searches, plan M4 Task 23).
import { useEffect, useState } from "react";

export function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => { setSettled(value); }, ms);
    return () => { clearTimeout(timer); };
  }, [value, ms]);
  return settled;
}
