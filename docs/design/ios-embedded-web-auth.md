# Authenticated embedded iOS pages

## Problem

The native iOS login supplies a Family Assistant bearer credential and an application session
cookie. Its embedded browser does not hold the Cloudflare Access session used by the system login
browser. Public API requests work, but Documents and More pages encounter a second login wall.

## Approach

Serve embedded pages and their frontend assets beneath `/api/app`, using the existing API gateway
JWT policy and Cloudflare API bypass. The normal website remains behind Cloudflare Access. Reuse the
React application with a routing basename and a build whose assets stay under the authenticated
prefix; ordinary browser builds retain their existing URLs.

The token-session bridge copies the presented signed access token into the existing HttpOnly,
Secure, SameSite=Lax API cookie. It does not mint a new token or extend that credential's expiry.
The backend verifies cookie JWTs with the same signature and revocation checks as bearer JWTs.
Native token refresh owns renewal: embedded pages wait for the cookie bridge before loading, and
refresh it while visible and on resume. Embedded pages never turn their application session into a
renewable browser credential.

The native navigation boundary translates embedded URLs back to ordinary app routes, so links to
Chat and Notes reach their native screens, while web destinations keep the authenticated prefix.

## Milestones and verification

1. Backend and frontend: authenticated page/asset routes, cookie bridge, and embedded routing/build.
   Verify unauthenticated, expired and revoked credentials fail; valid cookies load pages, chunks,
   and API data; normal page routing and browser login remain available.
2. iOS: load authenticated destinations after bridging credentials, preserve native navigation, and
   renew from native credentials. Verify routing, refresh and navigation with simulator tests.
3. Deployment: verify the existing `/api` bypass and gateway JWT catch-all cover pages and assets
   without adding an exemption. Verify through the public hostname after release.

## Deliberate simplifications

Embedded pages use the existing API cookie and gateway policy rather than introducing another
hostname, credential type, or Cloudflare session exchange. The native app owns token renewal; web
views cannot renew credentials independently. An unavailable or rejected bridge surfaces an error
with retry rather than sending the user through a second browser login.
