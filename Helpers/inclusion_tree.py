import copy
from urllib.parse import parse_qs


class InclusionTreeBuilder:
    def __init__(self, logger=None, crawl_context=None) -> None:
        self._logger = logger
        self._crawl_context = crawl_context
        self.reset()

    def reset(self) -> None:
        self._resource_requests: dict[str, list[dict]] = {}
        self._inclusion_tree: dict[tuple, dict] = {}
        self._frames: dict[str, dict] = {}
        self._websockets: dict[str, dict] = {}
        self._root_doc: tuple | None = None

    def handle_event(self, method: str, params: dict) -> None:
        if method == "Network.requestWillBeSent":
            self._handle_request_will_be_sent(params)
        elif method == "Network.responseReceived":
            self._handle_response_received(params)
        elif method in {"Page.frameAttached", "Page.frameNavigated", "Runtime.executionContextCreated"}:
            self._handle_frame_event(method, params)
        elif method == "Debugger.scriptParsed":
            self._handle_script_parsed(params)
        elif method.startswith("Network.webSocket"):
            self._handle_websocket(method, params)

    def build_trees(self) -> list[dict]:
        if self._root_doc is None:
            return []

        root = self._inclusion_tree.get(self._root_doc)
        if not isinstance(root, dict):
            return []

        root_url = (root.get("url") or "").strip().lower()
        if not root_url.startswith(("http://", "https://")):
            return []

        tree = copy.deepcopy(root)
        self._prune_tree(tree)
        return [tree]

    def count_nodes(self, node: dict) -> int:
        children = node.get("children") or []
        return 1 + sum(self.count_nodes(child) for child in children if isinstance(child, dict))

    @staticmethod
    def _get_script_id_from_stack_trace(stack: dict | None) -> str | None:
        if not stack:
            return None
        call_frames = stack.get("callFrames") or []
        if not call_frames:
            return None

        fallback = None
        for frame in call_frames:
            script_id = frame.get("scriptId")
            if script_id:
                fallback = script_id
            if not (frame.get("functionName") or "").strip():
                return script_id
        return fallback

    @staticmethod
    def _is_http_url(url: str | None) -> bool:
        if not url:
            return False
        lowered = url.strip().lower()
        return lowered.startswith("http://") or lowered.startswith("https://")

    @staticmethod
    def _node_key_document(frame_id: str, loader_id: str | int) -> tuple:
        return ("document", frame_id, str(loader_id))

    @staticmethod
    def _node_key_script(script_id: str | None) -> tuple:
        return ("script", str(script_id))

    @staticmethod
    def _append_child(parent: dict | None, child: dict) -> None:
        if not parent:
            return
        children = parent.setdefault("children", [])
        if not any(existing is child for existing in children):
            children.append(child)

    def _build_resource_headers(self, events: list[dict], resource_url: str) -> list[dict] | None:
        if not self._is_http_url(resource_url):
            return None

        resource_headers: list[dict] = []
        for index in range(1, len(events)):
            response = events[index].get("redirectResponse")
            if response is None:
                response = events[index].get("response")
            if response is None:
                continue

            request_event = events[index - 1]
            request = request_event.get("request") or {}
            header_entry = {
                "timestamp": request_event.get("wallTime"),
                "method": request.get("method"),
                "status": f"{response.get('status', '')} {response.get('statusText', '')}".strip(),
                "url": (response.get("url") or "").strip(),
                "request": None,
                "response": None,
            }

            if header_entry["method"] == "POST" and "postData" in request:
                raw_post = str(request.get("postData") or "")
                param_names: list[str] = []
                param_hmacs: dict[str, Any] = {}
                if "=" in raw_post:
                    try:
                        parsed = parse_qs(raw_post, keep_blank_values=True)
                        param_names = sorted(parsed.keys())
                        if self._crawl_context:
                            for k, vals in parsed.items():
                                if len(vals) == 1:
                                    param_hmacs[k] = self._crawl_context.hmac_value(vals[0])
                                else:
                                    param_hmacs[k] = [self._crawl_context.hmac_value(v) for v in vals]
                    except Exception:
                        pass
                header_entry["request_body_metadata"] = {
                    "byte_length": len(raw_post.encode("utf-8", errors="replace")),
                    "parameter_names": param_names,
                    "parameter_hmacs": param_hmacs,
                }

            request_headers = response.get("requestHeaders") or request.get("headers")
            if isinstance(request_headers, dict):
                header_entry["request"] = {
                    name: ("<redacted>" if str(name).lower() in {"cookie", "authorization", "proxy-authorization"} else value)
                    for name, value in request_headers.items()
                    if not str(name).startswith(":")
                }

            response_headers = response.get("headers")
            if isinstance(response_headers, dict):
                header_entry["response"] = {
                    name: ("<redacted>" if str(name).lower() in {"set-cookie"} else value)
                    for name, value in response_headers.items()
                    if not str(name).startswith(":")
                }

            resource_headers.append(header_entry)

        return resource_headers

    def _handle_request_will_be_sent(self, params: dict) -> None:
        request_id = params.get("requestId")
        if not request_id:
            return
        self._resource_requests.setdefault(request_id, []).append(params)

    def _handle_response_received(self, params: dict) -> None:
        request_id = params.get("requestId")
        if not request_id or request_id not in self._resource_requests:
            return

        events = self._resource_requests[request_id]
        events.append(params)

        response = params.get("response") or {}
        resource_type = str(params.get("type") or "other").strip().lower()
        resource_url = (response.get("url") or "").strip()
        resource_mime_type = str(response.get("mimeType") or "")
        frame_id = params.get("frameId")
        loader_id = params.get("loaderId")

        if resource_type == "other" and any(token in resource_mime_type.lower() for token in ("javascript", "ecmascript")):
            resource_type = "script"

        resource_headers = self._build_resource_headers(events, resource_url)
        initiator = events[0].get("initiator") or {}
        initiator_script_id = None
        if str(initiator.get("type") or "").strip().lower() == "script":
            initiator_script_id = self._get_script_id_from_stack_trace(initiator.get("stack"))

        inclusion_node = {
            "type": resource_type,
            "url": resource_url,
            "requestId": str(request_id),
            "frameId": str(frame_id) if frame_id is not None else None,
            "loaderId": str(loader_id) if loader_id is not None else None,
            "initiatorScriptId": str(initiator_script_id) if initiator_script_id is not None else None,
            "headers": resource_headers,
            "children": [],
        }

        if resource_type == "document":
            if frame_id is None or loader_id is None:
                del self._resource_requests[request_id]
                return

            document_key = self._node_key_document(str(frame_id), loader_id)
            self._inclusion_tree[document_key] = inclusion_node

            frame_state = self._frames.setdefault(str(frame_id), {})
            if initiator_script_id is not None:
                frame_state["initiatorScriptId"] = str(initiator_script_id)
            frame_state["loaderId"] = str(loader_id)
        else:
            if resource_type == "script" and frame_id is not None:
                self._inclusion_tree[("script", str(frame_id), resource_url)] = inclusion_node

            initiator_key = self._node_key_script(initiator_script_id) if initiator_script_id is not None else None
            if initiator_key is not None and initiator_key in self._inclusion_tree:
                self._append_child(self._inclusion_tree.get(initiator_key), inclusion_node)
            elif frame_id is not None and loader_id is not None:
                document_key = self._node_key_document(str(frame_id), loader_id)
                self._append_child(self._inclusion_tree.get(document_key), inclusion_node)

        del self._resource_requests[request_id]

    def _handle_frame_event(self, method: str, params: dict) -> None:
        if method == "Page.frameAttached":
            frame_id = params.get("frameId")
            if not frame_id:
                return
            frame_state = self._frames.setdefault(str(frame_id), {})
            if "stack" in params:
                initiator_script_id = self._get_script_id_from_stack_trace(params.get("stack"))
                if initiator_script_id is not None:
                    frame_state["initiatorScriptId"] = str(initiator_script_id)
            return

        if method == "Page.frameNavigated":
            frame = params.get("frame") or {}
            frame_id = frame.get("id")
            loader_id = frame.get("loaderId")
            if not frame_id or loader_id is None:
                return

            parent_frame_id = frame.get("parentId")
            url = (frame.get("url") or "").strip()
            frame_state = self._frames.setdefault(str(frame_id), {})
            frame_state["parentId"] = str(parent_frame_id) if parent_frame_id else None
            frame_state["loaderId"] = str(loader_id)

            frame_key = self._node_key_document(str(frame_id), loader_id)
            if frame_key not in self._inclusion_tree:
                self._inclusion_tree[frame_key] = {
                    "type": "document",
                    "url": url,
                    "headers": None,
                    "children": [],
                }
            elif url and not self._inclusion_tree[frame_key].get("url"):
                self._inclusion_tree[frame_key]["url"] = url

            initiator_script_id = frame_state.get("initiatorScriptId")
            if initiator_script_id is not None:
                self._append_child(self._inclusion_tree.get(self._node_key_script(initiator_script_id)), self._inclusion_tree[frame_key])
            elif parent_frame_id is not None:
                parent_loader_id = self._frames.get(str(parent_frame_id), {}).get("loaderId")
                if parent_loader_id is not None:
                    parent_key = self._node_key_document(str(parent_frame_id), parent_loader_id)
                    self._append_child(self._inclusion_tree.get(parent_key), self._inclusion_tree[frame_key])
            elif self._root_doc is None:
                self._root_doc = frame_key

            execution_context_id = frame_state.get("executionContextId")
            if execution_context_id is not None:
                self._inclusion_tree[self._node_key_document(str(frame_id), execution_context_id)] = self._inclusion_tree[frame_key]
            return

        context = params.get("context") or {}
        aux_data = context.get("auxData") or {}
        frame_id = aux_data.get("frameId")
        execution_context_id = context.get("id")
        if not frame_id or execution_context_id is None:
            return

        frame_state = self._frames.setdefault(str(frame_id), {})
        frame_state["executionContextId"] = str(execution_context_id)
        loader_id = frame_state.get("loaderId")
        if loader_id is not None:
            document_key = self._node_key_document(str(frame_id), loader_id)
            if document_key in self._inclusion_tree:
                self._inclusion_tree[self._node_key_document(str(frame_id), execution_context_id)] = self._inclusion_tree[document_key]

    def _handle_script_parsed(self, params: dict) -> None:
        script_id = params.get("scriptId")
        aux_data = params.get("executionContextAuxData") or {}
        frame_id = aux_data.get("frameId")
        if script_id is None or frame_id is None:
            return

        script_url = (params.get("url") or "").strip()
        lowered = script_url.lower()
        if lowered.startswith("extensions::") or lowered.startswith("chrome-extension://"):
            return

        unresolved_key = ("script", str(frame_id), script_url)
        resolved_key = self._node_key_script(str(script_id))
        if unresolved_key in self._inclusion_tree:
            self._inclusion_tree[resolved_key] = self._inclusion_tree.pop(unresolved_key)
            return

        script_node = {
            "type": "script",
            "url": script_url or None,
            "headers": None,
            "children": [],
        }
        self._inclusion_tree[resolved_key] = script_node

        initiator_script_id = self._get_script_id_from_stack_trace(params.get("stack"))
        if initiator_script_id is not None:
            self._append_child(self._inclusion_tree.get(self._node_key_script(initiator_script_id)), script_node)
        else:
            loader_id = self._frames.get(str(frame_id), {}).get("loaderId")
            if loader_id is not None:
                self._append_child(self._inclusion_tree.get(self._node_key_document(str(frame_id), loader_id)), script_node)

    def _handle_websocket(self, method: str, params: dict) -> None:
        request_id = params.get("requestId")
        if not request_id:
            return

        if method == "Network.webSocketCreated":
            new_node = {
                "type": "websocket",
                "url": (params.get("url") or "").strip(),
                "headers": [],
                "data": [],
                "closeTimestamp": None,
            }
            websocket_state = self._websockets.setdefault(
                request_id,
                {"scriptId": None, "wallTime": None, "timestamp": None, "node": None},
            )

            initiator = params.get("initiator") or {}
            if str(initiator.get("type") or "").strip().lower() == "script":
                websocket_state["scriptId"] = self._get_script_id_from_stack_trace(initiator.get("stack"))

            script_id = websocket_state.get("scriptId")
            if script_id is not None and self._node_key_script(script_id) in self._inclusion_tree:
                self._append_child(self._inclusion_tree.get(self._node_key_script(script_id)), new_node)
            elif self._root_doc is not None:
                self._append_child(self._inclusion_tree.get(self._root_doc), new_node)

            websocket_state["node"] = new_node
            return

        websocket_state = self._websockets.get(request_id)
        if websocket_state is None or websocket_state.get("node") is None:
            return

        node = websocket_state["node"]
        if method == "Network.webSocketWillSendHandshakeRequest":
            websocket_state["timestamp"] = params.get("timestamp")
            websocket_state["wallTime"] = params.get("wallTime")
            req_headers = (params.get("request") or {}).get("headers")
            sanitized_req = {}
            if isinstance(req_headers, dict):
                sanitized_req = {
                    name: ("<redacted>" if str(name).lower() in {"cookie", "authorization", "proxy-authorization"} else value)
                    for name, value in req_headers.items()
                    if not str(name).startswith(":")
                }
            node["headers"].append({
                "timestamp": params.get("wallTime"),
                "request": sanitized_req,
            })
        elif method == "Network.webSocketHandshakeResponseReceived":
            if node["headers"]:
                resp_headers = (params.get("response") or {}).get("headers")
                sanitized_resp = {}
                if isinstance(resp_headers, dict):
                    sanitized_resp = {
                        name: ("<redacted>" if str(name).lower() in {"set-cookie"} else value)
                        for name, value in resp_headers.items()
                        if not str(name).startswith(":")
                    }
                node["headers"][-1]["response"] = sanitized_resp
                response = params.get("response") or {}
                node["headers"][-1]["status"] = f"{response.get('status', '')} {response.get('statusText', '')}".strip()
        elif method in {"Network.webSocketFrameSent", "Network.webSocketFrameReceived"}:
            if websocket_state.get("wallTime") is None or websocket_state.get("timestamp") is None:
                timestamp = None
            else:
                timestamp = websocket_state["wallTime"] + params.get("timestamp", 0) - websocket_state["timestamp"]
            resp = params.get("response") or {}
            payload_str = str(resp.get("payloadData") or "")
            payload_hmac = self._crawl_context.hmac_value(payload_str) if (self._crawl_context and payload_str) else ""
            payload = {
                "type": "send" if method == "Network.webSocketFrameSent" else "receive",
                "timestamp": timestamp,
                "opcode": resp.get("opcode"),
                "mask": resp.get("mask"),
                "byte_length": len(payload_str.encode("utf-8", errors="replace")),
                "payload_hmac": payload_hmac,
            }
            node["data"].append(payload)
        elif method == "Network.webSocketClosed":
            if websocket_state.get("wallTime") is not None and websocket_state.get("timestamp") is not None:
                node["closeTimestamp"] = websocket_state["wallTime"] + params.get("timestamp", 0) - websocket_state["timestamp"]

    def _prune_tree(self, node: dict) -> None:
        children = node.get("children")
        if not isinstance(children, list):
            return

        position = 0
        while position < len(children):
            child = children[position]
            if isinstance(child, dict):
                self._prune_tree(child)
                if isinstance(child.get("children"), list) and not child["children"] and child.get("url") is None:
                    del children[position]
                    continue
            position += 1