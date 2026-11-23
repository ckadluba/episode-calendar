import { useSyncExternalStore } from "react";

import { getApiActivitySnapshot, subscribeApiActivity } from "./api";

/** Small edge indicator shown whenever at least one API request is in flight. */
export function ApiActivitySpinner() {
  const active = useSyncExternalStore(
    subscribeApiActivity,
    getApiActivitySnapshot,
    getApiActivitySnapshot,
  );
  if (!active) return null;

  return <div className="api-activity" role="status" aria-label="Daten werden geladen …">
    <span className="api-activity-ring" aria-hidden="true" />
  </div>;
}
