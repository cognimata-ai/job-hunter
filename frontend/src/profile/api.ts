// The profile's reads (spec §12, §14.2): `GET /api/profile`, the active version, every version, the draft in
// progress, the newest query proposal and the profile steps' costs. The Jobs page reads it for its "no profile
// yet" notice (§15.8, plan M4 Task 23); the CV and profile section (Task 27) reads and refreshes it.
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { apiFetch } from "../api/client";
import type { components } from "../api/schema";

export type ProfileListOut = components["schemas"]["ProfileListOut"];

export const PROFILE_KEY = ["profile"] as const;

export function useProfile(): UseQueryResult<ProfileListOut> {
  return useQuery({
    queryKey: PROFILE_KEY,
    queryFn: ({ signal }) => apiFetch<ProfileListOut>("/api/profile", { signal }),
  });
}
