// Shared UI state, keyed by table, tab group, list and current payload. Mutated
// by property and never reassigned, so every importer sees the same object.
export const sort = {};
export const tab = {};
export const pager = {};
export const page = {};

// Payload cache keyed by request URL. A sort, page or tab switch leaves the URL
// unchanged, so the same key serves it without a fetch; a filter or route change
// makes a new key, and a forced reload overwrites the entry to hit the network.
export const cache = new Map();
