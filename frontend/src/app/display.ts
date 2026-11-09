// The display settings of the signed-in user (`settings.display`), which `AppShell` provides once it has read
// the settings. A module of its own, so that what formats times (`system/format.ts`) does not import the shell.
import { createContext, useContext } from "react";

/** The settings' own default time zone (§13.1), until the settings are read. */
export const DEFAULT_TIMEZONE = "Europe/Berlin";

export interface Display {
  /** The IANA time zone times are shown in (`settings.display.timezone`). */
  timezone: string;
}

export const DisplayContext = createContext<Display>({ timezone: DEFAULT_TIMEZONE });

/** The display settings of the signed-in user. */
export function useDisplay(): Display {
  return useContext(DisplayContext);
}
