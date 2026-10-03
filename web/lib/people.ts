import type { PersonRow } from "./types";

export function importanceText(p: PersonRow): string {
  if (p.importance.user !== null) return `Importance ${p.importance.user}/5 (set by you)`;
  if (p.importance.inferred) return `Computed importance ${Math.round(p.importance.inferred * 100)}%`;
  return "No importance yet";
}
