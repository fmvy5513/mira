import { useEffect, useState, type ReactNode } from "react"

import { AuthContext, type AuthUser } from "./auth-context"

const API_BASE = import.meta.env.VITE_API_URL || ""

function applyTheme(theme: string) {
  // Sync with the ThemeProvider by setting localStorage + class
  localStorage.setItem("theme", theme)
  const root = document.documentElement
  root.classList.remove("light", "dark")
  root.classList.add(theme)
}

const LOGIN_TIMEOUT_MS = 20_000

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetch(`${API_BASE}/api/auth/me`, { credentials: "include" })
      .then((res) => {
        if (res.ok) return res.json()
        return null
      })
      .then((data) => {
        if (data && data.id) {
          setUser(data)
          applyTheme(data.theme)
        }
      })
      .finally(() => setLoading(false))
  }, [])

  const login = async (username: string, password: string): Promise<string | null> => {
    // Never leave the form spinning: a hung server, a dropped connection or a
    // non-JSON error page from the reverse proxy (e.g. 502 while the container
    // restarts) all surface as a message instead of an endless "Signing in...".
    const controller = new AbortController()
    const timer = window.setTimeout(() => controller.abort(), LOGIN_TIMEOUT_MS)
    let res: Response
    try {
      res = await fetch(`${API_BASE}/api/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ username, password }),
        signal: controller.signal,
      })
    } catch (err) {
      return controller.signal.aborted
        ? `The server did not respond within ${LOGIN_TIMEOUT_MS / 1000} seconds. Please try again shortly.`
        : `Could not reach the server: ${err instanceof Error ? err.message : String(err)}`
    } finally {
      window.clearTimeout(timer)
    }
    let data: { user?: AuthUser; error?: string } | null = null
    try {
      data = await res.json()
    } catch {
      // Not JSON: usually a proxy error page while the backend is down.
    }
    if (res.ok && data?.user) {
      setUser(data.user)
      applyTheme(data.user.theme)
      return null
    }
    if (data?.error) return data.error
    return res.ok ? "Login failed" : `Login failed: the server returned HTTP ${res.status}.`
  }

  const logout = async () => {
    await fetch(`${API_BASE}/api/auth/logout`, {
      method: "POST",
      credentials: "include",
    })
    setUser(null)
  }

  return (
    <AuthContext.Provider value={{ user, loading, login, logout }}>
      {children}
    </AuthContext.Provider>
  )
}
