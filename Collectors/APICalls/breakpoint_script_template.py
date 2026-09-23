"""JavaScript snippet template used for CDP breakpoint conditions.

The APICall tracker injects this JavaScript into Debugger.setBreakpointOnFunctionCall
conditions. Keeping it in a Python module removes the need for .js files inside
Collectors/APICalls while preserving behavior.
"""

BREAKPOINT_SCRIPT_TEMPLATE = """// @ts-nocheck
/* eslint-disable no-undef */
const stack = (new Error()).stack;
if (typeof stack === "string") {
    if (stack.indexOf('__adgraph') !== -1 || stack.indexOf('wrappedGet') !== -1 || stack.indexOf('wrappedMethod') !== -1) {
        shouldPause = false;
    } else {
        const lines = stack.split('\\n');
        const STACK_SOURCE_REGEX = /(\\()?(https?:[^)]+):[0-9]+:[0-9]+(\\))?/i;
        let url = null;

        for (let line of lines) {
            const lineData = line.match(STACK_SOURCE_REGEX);
            if (lineData) {
                url = lineData[2];
                break;
            }
        }

        let capturedArgs = null;
        ARGUMENT_COLLECTION

        const data = {
            description: 'DESCRIPTION',
            stack: stack,
            url: url,
            args: capturedArgs,
            saveArguments: SAVE_ARGUMENTS,
            capture_mechanism: 'breakpoint'
        };
        try {
            if (typeof window.registerAPICall === 'function') {
                window.registerAPICall(JSON.stringify(data));
            }
        } catch (_) {}

        if (!url) {
            shouldPause = true;
        }
    }
} else {
    shouldPause = true;
}
"""

WRAPPER_INIT_SCRIPT = """// @ts-nocheck
(function() {
    if (window.__adgraph_wrappers_installed__) return;
    window.__adgraph_wrappers_installed__ = true;

    function safeStringify(val, maxLen) {
        if (val === undefined) return { type: 'undefined', repr: 'undefined', len: 9 };
        if (val === null) return { type: 'null', repr: 'null', len: 4 };
        const t = typeof val;
        if (t === 'number' || t === 'boolean') {
            return { type: t, repr: String(val), len: String(val).length };
        }
        if (t === 'string') {
            return { type: 'string', repr: val.slice(0, maxLen || 500), len: val.length };
        }
        if (t === 'function') {
            return { type: 'function', repr: 'function() {}', len: 13 };
        }
        if (t === 'symbol') {
            return { type: 'symbol', repr: String(val), len: String(val).length };
        }
        if (t === 'object') {
            if (val instanceof Promise) {
                return { type: 'Promise', repr: '[object Promise]', len: 16 };
            }
            if (typeof Element !== 'undefined' && val instanceof Element) {
                return { type: 'Element', repr: '<' + val.tagName + '>', len: val.tagName.length + 2 };
            }
            try {
                const s = JSON.stringify(val);
                return { type: 'object', repr: (s ? s.slice(0, maxLen || 500) : '[object Object]'), len: s ? s.length : 15 };
            } catch (_) {
                return { type: 'object', repr: '[object Object]', len: 15 };
            }
        }
        return { type: t, repr: String(val).slice(0, maxLen || 500), len: String(val).length };
    }

    function resolveObject(path) {
        if (!path) return null;
        if (path.endsWith('.prototype')) {
            const base = path.slice(0, -10);
            return window[base] ? window[base].prototype : null;
        }
        return window[path] || null;
    }

    window.__adgraph_wrap_getter__ = function(objPath, propName, descName) {
        try {
            const target = resolveObject(objPath);
            if (!target) return;
            const desc = Object.getOwnPropertyDescriptor(target, propName);
            if (!desc || typeof desc.get !== 'function' || desc.get.__adgraph_wrapped__) return;
            const origGet = desc.get;
            const wrappedGet = function() {
                let ret;
                let caught = null;
                let threw = false;
                try {
                    ret = Reflect.apply(origGet, this, []);
                } catch (e) {
                    caught = e;
                    threw = true;
                }
                try {
                    const isProm = ret instanceof Promise;
                    const serializedRet = threw ? null : (isProm ? { type: 'Promise' } : safeStringify(ret, 500));
                    if (typeof window.registerAPICall === 'function') {
                        window.registerAPICall(JSON.stringify({
                            description: descName,
                            api_name: descName,
                            operation_type: 'property_get',
                            stack: (new Error()).stack,
                            url: null,
                            args: null,
                            returnValue: serializedRet,
                            hasReturnValue: !threw && !isProm,
                            isAsync: isProm,
                            threw: threw,
                            capture_mechanism: 'wrapper'
                        }));
                    }
                } catch (_) {}
                if (threw) throw caught;
                return ret;
            };
            wrappedGet.__adgraph_wrapped__ = true;
            Object.defineProperty(target, propName, {
                get: wrappedGet,
                set: desc.set,
                enumerable: desc.enumerable,
                configurable: desc.configurable
            });
        } catch (_) {}
    };

    window.__adgraph_wrap_method__ = function(objPath, methodName, descName) {
        try {
            const target = resolveObject(objPath);
            if (!target) return;
            const origMethod = target[methodName];
            if (typeof origMethod !== 'function' || origMethod.__adgraph_wrapped__) return;
            const wrappedMethod = function(...args) {
                let ret;
                let caught = null;
                let threw = false;
                try {
                    ret = Reflect.apply(origMethod, this, args);
                } catch (e) {
                    caught = e;
                    threw = true;
                }
                try {
                    const isProm = ret instanceof Promise;
                    const serializedArgs = args.slice(0, 10).map(a => safeStringify(a, 500));
                    const serializedRet = threw ? null : (isProm ? { type: 'Promise' } : safeStringify(ret, 500));
                    if (typeof window.registerAPICall === 'function') {
                        window.registerAPICall(JSON.stringify({
                            description: descName,
                            api_name: descName,
                            operation_type: 'method_call',
                            stack: (new Error()).stack,
                            url: null,
                            args: serializedArgs,
                            returnValue: serializedRet,
                            hasReturnValue: !threw && !isProm,
                            isAsync: isProm,
                            threw: threw,
                            capture_mechanism: 'wrapper'
                        }));
                    }
                } catch (_) {}
                if (threw) throw caught;
                return ret;
            };
            wrappedMethod.__adgraph_wrapped__ = true;
            target[methodName] = wrappedMethod;
        } catch (_) {}
    };
})();
"""

