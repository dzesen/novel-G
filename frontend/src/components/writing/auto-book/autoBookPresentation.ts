export const AUTO_BOOK_PURPOSES = {
  readiness: "start",
  runs: "operations",
  "generation-runs": "history",
  diagnostics: "diagnostics",
  retrospective: "retrospective",
} as const;

export type AutoBookView = keyof typeof AUTO_BOOK_PURPOSES;
export type AutoBookPurpose = (typeof AUTO_BOOK_PURPOSES)[AutoBookView];

export function autoBookPurpose(view: AutoBookView): AutoBookPurpose {
  return AUTO_BOOK_PURPOSES[view];
}
