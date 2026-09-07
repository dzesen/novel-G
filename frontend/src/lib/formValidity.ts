/** Reveal an invalid field before asking the browser to focus and explain it. */
export function reportFormValidity(form: HTMLFormElement | null): boolean {
  if (!form) return false;
  const invalid = form.querySelector(":invalid");
  const details = invalid?.closest("details");
  if (details) details.open = true;
  return form.reportValidity();
}
