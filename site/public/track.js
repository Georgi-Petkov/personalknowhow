// Delegated click tracking: any element with data-track="<event-name>" fires
// a best-effort beacon. New buttons just need the attribute -- no new JS --
// but the event name must also be added to TRACKABLE_EVENTS in src/index.ts.
document.addEventListener("click", (event) => {
  const target = event.target;
  const el = target instanceof Element ? target.closest("[data-track]") : null;
  if (!el) return;
  const name = el.getAttribute("data-track");
  if (!name) return;
  try {
    navigator.sendBeacon("/api/track", JSON.stringify({ event: name }));
  } catch {
    // Never block the click on analytics.
  }
});
