// The polite live region (spec §15.9: run and apply progress are announced). `useAnnounce()` gives the
// components inside `<LiveRegion>` a function that announces a text; the same text announced twice is read
// twice (the region is emptied first).
import { createContext, type ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";

const ANNOUNCE_AFTER_MS = 100;

const AnnounceContext = createContext<((message: string) => void) | null>(null);

export function useAnnounce(): (message: string) => void {
  const announce = useContext(AnnounceContext);
  if (announce === null) {
    throw new Error("useAnnounce is used outside <LiveRegion>");
  }
  return announce;
}

export function LiveRegion({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState("");
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const announce = useCallback((text: string) => {
    clearTimeout(timer.current);
    setMessage("");
    timer.current = setTimeout(() => { setMessage(text); }, ANNOUNCE_AFTER_MS);
  }, []);
  useEffect(() => () => { clearTimeout(timer.current); }, []);
  return (
    <AnnounceContext.Provider value={announce}>
      {children}
      <div className="visually-hidden" role="status" aria-live="polite">{message}</div>
    </AnnounceContext.Provider>
  );
}
