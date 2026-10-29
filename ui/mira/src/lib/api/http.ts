// Shared fetch helpers for the API client. Domain modules under `lib/api/`
// import these; `lib/api.ts` composes the modules into the single `api` object.

// Empty default → same-origin requests (production single-container).
// In dev, set VITE_API_URL=http://localhost:8100 in ui/mira/.env.local.
export const API_BASE = import.meta.env.VITE_API_URL || ""

/** Turn a thrown API error into a short, human-readable message.
 *
 * Backend errors arrive as `API error <status>: {"detail": "..."}`; return
 * the `detail` text when present, otherwise the raw message. */
export function describeApiError(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err)
  const match = /^API error (\d+): ([\s\S]*)$/.exec(raw)
  if (!match) return raw
  const [, status, body] = match
  try {
    const parsed = JSON.parse(body) as { detail?: unknown; error?: unknown }
    const detail = parsed.detail ?? parsed.error
    if (typeof detail === "string" && detail) return detail
  } catch {
    // not JSON — fall through
  }
  return body.trim() ? `${body.trim()} (HTTP ${status})` : `HTTP ${status}`
}

export async function fetchJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { credentials: "include" })
  if (!res.ok) {
    if (res.status === 401) window.location.href = "/login"
    throw new Error(`API error ${res.status}: ${await res.text()}`)
  }
  return res.json() as Promise<T>
}

export async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`)
  return res.json() as Promise<T>
}

export async function putJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`)
  return res.json() as Promise<T>
}

export async function deleteJson(path: string): Promise<void> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "DELETE",
    credentials: "include",
  })
  if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`)
}

export async function patchJson<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "PATCH",
    credentials: "include",
    ...(body !== undefined
      ? {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      : {}),
  })
  if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`)
  return res.json() as Promise<T>
}
