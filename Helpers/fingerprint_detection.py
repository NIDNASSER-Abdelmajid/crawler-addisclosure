"""Fingerprinting API instrumentation injected into the page before navigation."""

import re

_VALID_JS_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


def fingerprint_detection_script(binding_name: str = "calledAPIEvent") -> str:
    """Return the API-call instrumentation script bound to a page function name."""
    if not _VALID_JS_IDENTIFIER_RE.fullmatch(binding_name):
        raise ValueError(f"Invalid JavaScript binding name: {binding_name!r}")

    script = r"""
(function() {
  const MAX_DETAILED_CALLS_PER_API = 100;
  const STACK_LINE_REGEXP = /(\()?(https?:\/\/[^)]+):[0-9]+:[0-9]+(\))?/;
  const accessCounts = Object.create(null);
  let totalObserved = 0;

  function getSourceAndStack() {
    let source = 'inline_or_unknown';
    let fullStack = '';
    try {
      fullStack = String(new Error().stack || '');
      const lines = fullStack.split('\n');
      for (let line of lines) {
        const match = line.match(STACK_LINE_REGEXP);
        if (match && match[2]) {
          source = match[2];
          break;
        }
      }
    } catch (_) {}
    return { source, stack: fullStack };
  }

  function simplifyValue(value, depth) {
    const level = depth || 0;
    if (value === null || value === undefined) return value;
    if (typeof value === 'string') {
      if (value.startsWith('data:')) {
        return value.slice(0, 80) + '... [truncated ' + value.length + ' chars]';
      }
      if (value.length > 500) {
        return value.slice(0, 500) + '... [truncated ' + value.length + ' chars]';
      }
      return value;
    }
    if (typeof value === 'number' || typeof value === 'boolean') {
      return value;
    }
    if (typeof value === 'bigint') {
      return value.toString();
    }
    if (typeof value === 'function') {
      return '[Function ' + (value.name || 'anonymous') + ']';
    }
    if (value instanceof Error) {
      return value.message;
    }
    if (level >= 1) {
      return Object.prototype.toString.call(value);
    }
    if (Array.isArray(value)) {
      return value.slice(0, 5).map(item => simplifyValue(item, level + 1));
    }
    try {
      return JSON.parse(JSON.stringify(value));
    } catch (_) {
      return Object.prototype.toString.call(value);
    }
  }

  function emitCall(callDetails) {
    try {
      if (typeof window.__API_CALL_BINDING__ === 'function') {
        callDetails.timestamp_ms = Date.now();
        try {
          callDetails.frameUrl = window.location ? window.location.href : null;
        } catch (_) {
          callDetails.frameUrl = null;
        }
        const maybePromise = window.__API_CALL_BINDING__(callDetails);
        if (maybePromise && typeof maybePromise.catch === 'function') {
          maybePromise.catch(() => undefined);
        }
      }
    } catch (_) {}
  }

  function interceptFunctionCall(elementType, funcName) {
    if (!elementType || !elementType.prototype) return;
    const origDesc = Object.getOwnPropertyDescriptor(elementType.prototype, funcName);
    if (!origDesc || typeof origDesc.value !== 'function' || origDesc.configurable === false) return;
    const origFunc = origDesc.value;

    Object.defineProperty(elementType.prototype, funcName, {
      configurable: true,
      writable: true,
      value: function() {
        const retVal = origFunc.apply(this, arguments);
        const calledFunc = elementType.name + '.' + funcName;
        accessCounts[calledFunc] = (accessCounts[calledFunc] || 0) + 1;
        totalObserved++;
        const callCnt = accessCounts[calledFunc];
        const { source, stack } = getSourceAndStack();

        if (callCnt <= MAX_DETAILED_CALLS_PER_API) {
          emitCall({
            description: calledFunc,
            accessType: 'call',
            args: simplifyValue(Array.from(arguments), 0),
            retVal: simplifyValue(retVal, 0),
            source: source,
            stack: stack,
            capture_status: 'captured',
            total_observed_count: callCnt,
          });
        } else if (callCnt === MAX_DETAILED_CALLS_PER_API + 1 || callCnt % 1000 === 0) {
          // Do not flood IPC on every call — record truncation marker and periodic running total count
          emitCall({
            description: calledFunc,
            accessType: 'call',
            args: null,
            retVal: null,
            source: source,
            stack: null,
            capture_status: 'truncated',
            total_observed_count: callCnt,
          });
        }

        return retVal;
      },
    });
  }

  function interceptPropAccess(elementType, propertyName) {
    if (!elementType || !elementType.prototype) return;
    const origDesc = Object.getOwnPropertyDescriptor(elementType.prototype, propertyName);
    if (!origDesc || (!origDesc.get && !origDesc.set) || origDesc.configurable === false) return;
    const accessedProp = elementType.name + '.' + propertyName;

    Object.defineProperty(elementType.prototype, propertyName, {
      enumerable: origDesc.enumerable !== false,
      configurable: true,
      get: origDesc.get
        ? function() {
            const returnVal = origDesc.get.call(this);
            accessCounts[accessedProp] = (accessCounts[accessedProp] || 0) + 1;
            totalObserved++;
            const accessCnt = accessCounts[accessedProp];
            const { source, stack } = getSourceAndStack();

            if (accessCnt <= MAX_DETAILED_CALLS_PER_API) {
              emitCall({
                description: accessedProp,
                accessType: 'get',
                args: null,
                retVal: simplifyValue(returnVal, 0),
                source: source,
                stack: stack,
                capture_status: 'captured',
                total_observed_count: accessCnt,
              });
            } else if (accessCnt === MAX_DETAILED_CALLS_PER_API + 1 || accessCnt % 1000 === 0) {
              emitCall({
                description: accessedProp,
                accessType: 'get',
                args: null,
                retVal: null,
                source: source,
                stack: null,
                capture_status: 'truncated',
                total_observed_count: accessCnt,
              });
            }

            return returnVal;
          }
        : undefined,
      set: origDesc.set
        ? function(value) {
            origDesc.set.call(this, value);
            accessCounts[accessedProp] = (accessCounts[accessedProp] || 0) + 1;
            totalObserved++;
            const accessCnt = accessCounts[accessedProp];
            const { source, stack } = getSourceAndStack();

            if (accessCnt <= MAX_DETAILED_CALLS_PER_API) {
              emitCall({
                description: accessedProp,
                accessType: 'set',
                args: simplifyValue(value, 0),
                retVal: undefined,
                source: source,
                stack: stack,
                capture_status: 'captured',
                total_observed_count: accessCnt,
              });
            } else if (accessCnt === MAX_DETAILED_CALLS_PER_API + 1 || accessCnt % 1000 === 0) {
              emitCall({
                description: accessedProp,
                accessType: 'set',
                args: null,
                retVal: null,
                source: source,
                stack: null,
                capture_status: 'truncated',
                total_observed_count: accessCnt,
              });
            }
          }
        : undefined,
    });
  }

  interceptFunctionCall(CanvasRenderingContext2D, 'fillText');
  interceptFunctionCall(CanvasRenderingContext2D, 'strokeText');
  interceptPropAccess(CanvasRenderingContext2D, 'fillStyle');
  interceptPropAccess(CanvasRenderingContext2D, 'strokeStyle');
  interceptFunctionCall(HTMLCanvasElement, 'toDataURL');
  interceptFunctionCall(CanvasRenderingContext2D, 'save');
  interceptFunctionCall(CanvasRenderingContext2D, 'restore');
  interceptFunctionCall(CanvasRenderingContext2D, 'measureText');
  interceptFunctionCall(CanvasRenderingContext2D, 'isPointInPath');
  interceptPropAccess(CanvasRenderingContext2D, 'font');

  interceptFunctionCall(RTCPeerConnection, 'createDataChannel');
  interceptFunctionCall(RTCPeerConnection, 'createOffer');
  interceptPropAccess(RTCPeerConnection, 'onicecandidate');
  interceptPropAccess(RTCPeerConnection, 'localDescription');

  interceptFunctionCall(OfflineAudioContext, 'createOscillator');
  interceptFunctionCall(OfflineAudioContext, 'createDynamicsCompressor');
  interceptPropAccess(BaseAudioContext, 'destination');
  interceptFunctionCall(OfflineAudioContext, 'startRendering');
  interceptPropAccess(OfflineAudioContext, 'oncomplete');
})();
"""
    return script.replace("__API_CALL_BINDING__", binding_name)