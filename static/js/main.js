/**
 * DealMaker v2 — main.js
 * Handles simulation start/stop actions on both the list and detail pages.
 */

async function simAction(storeId, action) {
  try {
    const resp = await fetch(`/simulation/${storeId}/${action}`, { method: 'POST' });
    const data = await resp.json();

    // Update the status badge and action buttons in the list view (if present)
    const statusEl = document.querySelector(`[id="status-${storeId}"]`);
    if (statusEl) {
      const newStatus = action === 'start' ? 'running' : 'stopping';
      statusEl.textContent = newStatus;
      statusEl.className = `badge badge--${newStatus}`;
    }

    // Refresh the page after a short delay so the table reflects the new state
    setTimeout(() => window.location.reload(), 1200);
  } catch (err) {
    console.error('simAction error:', err);
  }
}
