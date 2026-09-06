import { UserManager, WebStorageStateStore, User } from 'oidc-client-ts'

const authority = import.meta.env.VITE_OIDC_AUTHORITY ?? ''
const clientId = import.meta.env.VITE_OIDC_CLIENT_ID ?? ''

export const userManager = new UserManager({
  authority,
  client_id: clientId,
  redirect_uri: window.location.origin,
  post_logout_redirect_uri: window.location.origin,
  response_type: 'code',
  scope: 'openid profile email urn:zitadel:iam:org:project:roles',
  userStore: new WebStorageStateStore({ store: window.sessionStorage }),
  automaticSilentRenew: true,
})

// R660.2 (OpusLogic Block 220). A shared secret, presented once as
// http://localhost:7501/#token=… and kept in sessionStorage, replaces the
// Zitadel login for the tunnel case. The collector compares it against
// PULSE_SHARED_TOKEN. The OIDC path below is untouched.
const SHARED_KEY = 'pulse_shared_token'

function takeSharedTokenFromHash(): void {
  const m = window.location.hash.match(/[#&]token=([^&]+)/)
  if (m) {
    sessionStorage.setItem(SHARED_KEY, decodeURIComponent(m[1]))
    window.history.replaceState({}, '', window.location.pathname + window.location.search)
  }
}

export function sharedToken(): string | null {
  return sessionStorage.getItem(SHARED_KEY)
}

export async function ensureAuthenticated(): Promise<User | null> {
  takeSharedTokenFromHash()
  if (sharedToken()) return null
  if (window.location.search.includes('code=') && window.location.search.includes('state=')) {
    const user = await userManager.signinRedirectCallback()
    const ret = sessionStorage.getItem('pulse_return_path') || '/'
    sessionStorage.removeItem('pulse_return_path')
    window.history.replaceState({}, '', ret)
    return user
  }

  const existing = await userManager.getUser()
  if (existing && !existing.expired) return existing

  const path = window.location.pathname + window.location.search
  if (path !== '/') sessionStorage.setItem('pulse_return_path', path)
  await userManager.signinRedirect()
  return new Promise(() => {})
}

export async function getAccessToken(): Promise<string | null> {
  const shared = sharedToken()
  if (shared) return shared
  let u = await userManager.getUser()
  if (!u || u.expired) {
    // Token gone or expired (e.g. tab woke after long sleep). Try silent renew
    // first; if Zitadel won't issue without user interaction, redirect to login.
    try {
      u = await userManager.signinSilent()
    } catch {
      const path = window.location.pathname + window.location.search
      if (path !== '/') sessionStorage.setItem('pulse_return_path', path)
      await userManager.signinRedirect()
      return null
    }
  }
  return u?.access_token ?? null
}

export async function getUsername(): Promise<string> {
  if (sharedToken()) return 'shared token'
  const u = await userManager.getUser()
  return u?.profile?.preferred_username ?? u?.profile?.name ?? ''
}

export async function logout(): Promise<void> {
  if (sharedToken()) {
    sessionStorage.removeItem(SHARED_KEY)
    window.location.reload()
    return
  }
  await userManager.signoutRedirect()
}
