// Patched version of @assistant-ui/store's tapClientLookup that doesn't throw
// on out-of-bounds access. This works around a race condition in the tap
// reactive system where resources can be empty during rapid mount/unmount
// cycles in tests (assistant-ui/assistant-ui#3395).
//
// The only change from the original: the `get` method returns a no-op stub
// instead of throwing when the index is out of bounds or key is not found.

import { tapMemo, tapResource, tapResources } from '@assistant-ui/tap';
import { ClientResource } from '@assistant-ui/store/dist/tapClientResource.js';
import { wrapperResource } from '@assistant-ui/store/dist/wrapperResource.js';

const ClientResourceWithKey = wrapperResource((el) => {
  if (el.key === undefined) {
    throw new Error('tapClientResource: Element has no key');
  }
  return tapResource(ClientResource(el));
});

// Stub returned when a resource is not found (out-of-bounds or missing key).
// Provides minimal interface to avoid TypeError crashes in consumers.
const STUB_METHODS = new Proxy(
  {},
  {
    get: (_target, prop) => {
      if (prop === 'getState') {
        return () => ({});
      }
      if (prop === 'subscribe') {
        return () => () => {};
      }
      return () => {};
    },
  }
);

export function tapClientLookup(getElements, getElementsDeps) {
  const resources = tapResources(
    () => getElements().map((el) => ClientResourceWithKey(el)),
    getElementsDeps
  );
  const keys = tapMemo(() => Object.keys(resources), [resources]);
  const keyToIndex = tapMemo(() => {
    return resources.reduce((acc, resource, index) => {
      acc[resource.key] = index;
      return acc;
    }, {});
  }, [resources]);
  const state = tapMemo(() => {
    return resources.map((r) => r.state);
  }, [resources]);
  return {
    state,
    get: (lookup) => {
      if ('index' in lookup) {
        if (lookup.index < 0 || lookup.index >= keys.length) {
          // Original throws here; we return a stub instead
          return STUB_METHODS;
        }
        return resources[lookup.index].methods;
      }
      const index = keyToIndex[lookup.key];
      if (index === undefined) {
        // Original throws here; we return a stub instead
        return STUB_METHODS;
      }
      return resources[index].methods;
    },
  };
}
