import { useEffect } from "react";

const SUFFIX = "Credence";
const DEFAULT_TITLE = "Credence — Trust-and-fit scoring for B2B prospects";

/**
 * Sets `document.title` to `${title} — ${SUFFIX}` while the calling component
 * is mounted, restoring the previous title on unmount. Pass `null`/`""` to
 * keep the default title (used by the landing page so the SEO-tuned default
 * survives).
 */
export function useDocumentTitle(title: string | null | undefined) {
  useEffect(() => {
    if (!title) {
      document.title = DEFAULT_TITLE;
      return;
    }
    const prev = document.title;
    document.title = `${title} — ${SUFFIX}`;
    return () => {
      document.title = prev;
    };
  }, [title]);
}
