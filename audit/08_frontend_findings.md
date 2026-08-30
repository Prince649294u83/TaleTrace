# 08. Frontend Findings

## Integration & Network Resilience

### Evidence
The `api.js` fetch wrapper `getMe()` catches all exceptions and silently returns `null`.

### Analysis
If the FastAPI backend is temporarily unavailable (e.g. 502, 503) or the network drops, `getMe()` interprets this as a `401 Unauthorized` equivalent and triggers a logout/redirect behavior. The frontend does not differentiate between "user is unauthenticated" and "backend is unreachable".

### Additional Findings
- **Unsafe JSON Parsing**: Responses are parsed via `JSON.parse(text)` without try-catch validation for non-JSON server error pages, resulting in opaque `SyntaxError` crashes on the client.
- **Stale Documentation**: `companion.py`'s module documentation still claims "Not authenticated", despite the introduction of the new authentication architecture.
- **Debug Endpoint Exposure**: `/api/debug/camera` is completely unauthenticated, exposing raw camera frames locally.
