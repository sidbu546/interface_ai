"""The surface seam: how we perceive and act on an application.

Everything above this module is surface-agnostic. A ``UINode`` contains no
web-specific concepts -- no CSS, no XPath, no DOM -- so the same shape can be
produced by a desktop accessibility API later without changing the artifact
schema, the agent loop, or the replay engine.

Perception is accessibility-tree first, via CDP ``Accessibility.getFullAXTree``,
walked across every frame. That is deliberate: the target environment is full
of applications with no clean DOM, and the AX tree is the one representation
that survives them (and exists on desktop too).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from playwright.sync_api import Browser, BrowserContext, Frame, Page, sync_playwright

# AX roles that carry no information for an operator; dropped from snapshots to
# keep the model's context focused on things it can actually act on or read.
_NOISE_ROLES = {
    "none",
    "generic",
    "InlineTextBox",
    "LineBreak",
    "GenericContainer",
    "presentation",
    "RootWebArea",
}

# Roles a person can interact with, as opposed to read.
INTERACTIVE_ROLES = {
    "button",
    "link",
    "textbox",
    "checkbox",
    "radio",
    "combobox",
    "listbox",
    "menuitem",
    "tab",
    "switch",
    "slider",
}


@dataclass
class UINode:
    """One control or piece of content, described the way an operator sees it.

    Note what is absent: no selector, no element handle, no tag name. A step in
    a capability artifact refers to a node by role, name and container -- never
    by markup -- which is what makes the recorded flow portable.
    """

    ref: int
    role: str
    name: str
    value: str = ""
    frame: str = ""
    interactive: bool = False
    # Nearby text that gives an unnamed control its meaning. On legacy tables
    # this is often the *only* thing identifying an input.
    anchor: str = ""
    _backend_id: int | None = field(default=None, repr=False)


    def render(self) -> str:
        bits = [f"[{self.ref}]", self.role]
        if self.name:
            bits.append(f'"{self.name}"')
        if self.value:
            bits.append(f"value={self.value!r}")
        if self.anchor and not self.name:
            bits.append(f"(labelled by: {self.anchor!r})")
        if self.frame:
            bits.append(f"frame={self.frame}")
        return " ".join(bits)


@dataclass
class UISnapshot:
    url: str
    title: str
    nodes: list[UINode]
    # In a frameset the top-level URL barely changes while the content frame
    # navigates freely. A URL assertion has to be able to see both.
    frame_urls: list[str] = field(default_factory=list)
    # Every piece of text on screen, including StaticText that is too granular
    # to list as a node. Detectors match against this: a condition rendered as
    # plain prose is still a condition, and missing it means proceeding blind.
    text: str = ""

    def render(self, limit: int = 220) -> str:
        """A compact text rendering -- what the model actually reads."""
        lines = [f"URL: {self.url}", f"TITLE: {self.title}", "SCREEN:"]
        for node in self.nodes[:limit]:
            lines.append("  " + node.render())
        if len(self.nodes) > limit:
            lines.append(f"  ... {len(self.nodes) - limit} more nodes omitted")
        return "\n".join(lines)

    def by_ref(self, ref: int) -> UINode | None:
        return next((n for n in self.nodes if n.ref == ref), None)


class Surface(Protocol):
    """What every surface must provide. Web today; desktop behind the same shape.

    This is the seam. Everything above it -- the compiler, the artifact schema,
    the resolution ladder, the detectors, the recovery model -- is written
    against these methods and nothing else, so a desktop or remoted surface
    plugs in by implementing them over UIA/AX/AT-SPI.

    The list is deliberately what the engines *actually* call. An earlier
    version declared a single generic ``act(action, **kwargs)`` that nothing
    ever invoked, while discovery and replay called concrete methods directly --
    which made the seam look narrower than it was and would have stranded anyone
    implementing a second surface against the declared interface.
    """

    # --- perceiving -------------------------------------------------------
    def observe(self) -> UISnapshot: ...
    def resolve(self, ref: int) -> UINode: ...

    # --- acting -----------------------------------------------------------
    def navigate(self, url: str) -> str: ...
    def click(self, ref: int) -> str: ...
    def type_text(self, ref: int, text: str) -> str: ...
    def select_option(self, ref: int, value: str) -> str: ...
    def submit_form(self, ref: int) -> str: ...

    # --- housekeeping -----------------------------------------------------
    def screenshot(self, path: str) -> None: ...
    def wait(self, milliseconds: int) -> None: ...


class WebSurface:
    """A browser surface driven through the accessibility tree."""

    def __init__(
        self,
        headless: bool = True,
        viewport: tuple[int, int] = (1280, 900),
        faults=None,
    ):
        self._pw = sync_playwright().start()
        self._browser: Browser = self._pw.chromium.launch(headless=headless)
        # Pinned so the same inputs render the same way on every run -- part of
        # what makes replay deterministic later.
        self._context: BrowserContext = self._browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]},
            locale="en-US",
            timezone_id="UTC",
        )
        self.page: Page = self._context.new_page()
        self._snapshot: UISnapshot | None = None
        self._cdp = None
        if faults is not None:
            from .faults import install

            install(self.page, faults)

    # ------------------------------------------------------------------- cdp

    def _page_cdp(self):
        """One CDP session for the whole page.

        Same-origin child frames do NOT get their own session -- they are part
        of the parent's. Asking for one raises, so the frame tree is walked by
        frameId against this single session instead.
        """
        if self._cdp is None:
            self._cdp = self._context.new_cdp_session(self.page)
            self._cdp.send("Page.enable")
            self._cdp.send("DOM.enable")
            self._cdp.send("Accessibility.enable")
        return self._cdp

    def _frame_entries(self) -> list[tuple[str, str]]:
        """(frameId, name) for the main frame and every descendant."""
        tree = self._page_cdp().send("Page.getFrameTree")["frameTree"]
        entries: list[tuple[str, str]] = []

        def walk(node: dict) -> None:
            frame = node["frame"]
            entries.append((frame["id"], frame.get("name") or ""))
            for child in node.get("childFrames") or []:
                walk(child)

        walk(tree)
        return entries

    # ---------------------------------------------------------------- observe

    def observe(self) -> UISnapshot:
        self.page.wait_for_load_state("domcontentloaded")
        nodes: list[UINode] = []
        all_text: list[str] = []
        ref = 0
        cdp = self._page_cdp()

        for frame_id, frame_name in self._frame_entries():
            try:
                tree = cdp.send("Accessibility.getFullAXTree", {"frameId": frame_id})
            except Exception as exc:
                # Never swallow this silently: an empty snapshot caused by a
                # skipped frame looks exactly like a genuinely empty screen,
                # which is a very expensive thing to debug from a transcript.
                print(f"  [surface] warning: AX tree unavailable for frame "
                      f"{frame_name or frame_id}: {type(exc).__name__}")
                continue

            raw = tree.get("nodes", [])
            # Index by id so we can look "left" for an unnamed control's label.
            texts_by_parent: dict[str, list[str]] = {}
            for n in raw:
                parent = n.get("parentId", "")
                name = (n.get("name") or {}).get("value", "")
                if name:
                    texts_by_parent.setdefault(parent, []).append(name)

            previous_text = ""
            for n in raw:
                if n.get("ignored"):
                    continue
                role = (n.get("role") or {}).get("value", "")
                name = ((n.get("name") or {}).get("value", "") or "").strip()
                value = ((n.get("value") or {}).get("value", "") or "").strip()

                if role in _NOISE_ROLES:
                    continue
                if name:
                    all_text.append(name)
                if role == "StaticText":
                    # Keep as an anchor candidate but don't list separately;
                    # its text is already on the containing cell.
                    if name:
                        previous_text = name
                    continue
                if not name and not value and role not in INTERACTIVE_ROLES:
                    continue

                ref += 1
                nodes.append(
                    UINode(
                        ref=ref,
                        role=role,
                        name=name,
                        value=value,
                        frame=frame_name,
                        interactive=role in INTERACTIVE_ROLES,
                        anchor=previous_text if not name else "",
                        _backend_id=n.get("backendDOMNodeId"),
                    )
                )

        self._snapshot = UISnapshot(
            url=self.page.url,
            title=self.page.title() or "",
            nodes=nodes,
            frame_urls=[f.url for f in self.page.frames],
            text="\n".join(all_text),
        )
        return self._snapshot

    # -------------------------------------------------------------------- act

    def resolve(self, ref: int):
        if self._snapshot is None:
            raise RuntimeError("observe() must be called before act()")
        node = self._snapshot.by_ref(ref)
        if node is None:
            raise ValueError(f"no node with ref {ref} in the current screen")
        if node._backend_id is None:
            raise ValueError(f"node {ref} cannot be acted on")
        return node

    def _handle(self, node: UINode):
        """Turn an AX node back into something clickable, via CDP.

        backendNodeId is document-wide within the page session, so nodes inside
        same-origin frames resolve through the same session that produced them.
        """
        cdp = self._page_cdp()
        resolved = cdp.send("DOM.resolveNode", {"backendNodeId": node._backend_id})
        return cdp, resolved["object"]["objectId"]

    def click(self, ref: int) -> str:
        node = self.resolve(ref)
        cdp, object_id = self._handle(node)
        outcome = cdp.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                # scrollIntoView then click: works for <a>, <input type=submit>
                # and, critically, a <div onclick> that has no button role.
                "returnByValue": True,
                "functionDeclaration": """
                function(){
                  this.scrollIntoView({block:'center'});
                  // An accessibility node often maps to the CONTAINER of the real
                  // control -- a <td> wrapping a <div onclick>. Clicking the
                  // container silently does nothing, which looks identical to a
                  // click that worked. Descend to the thing that actually handles
                  // the click before dispatching.
                  const SELF = '[onclick],button,a,input,select,textarea,[role=button]';
                  const target = this.matches(SELF)
                    ? this
                    : (this.querySelector(SELF) || this);
                  target.click();
                  return target.tagName + (target === this ? '' : ' (descended)');
                }""",
            },
        )
        self.page.wait_for_load_state("domcontentloaded")
        self.page.wait_for_timeout(250)
        dispatched = outcome["result"].get("value", "")
        return f"clicked {node.role} {node.name or node.anchor!r} -> <{dispatched}>"

    def type_text(self, ref: int, text: str) -> str:
        """Set a field's value, fire the events a server-rendered form expects,
        and confirm the value actually stuck."""
        node = self.resolve(ref)
        cdp, object_id = self._handle(node)
        outcome = cdp.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "returnByValue": True,
                "functionDeclaration": (
                    "function(v){ this.focus(); this.value = v;"
                    " this.dispatchEvent(new Event('input',{bubbles:true}));"
                    " this.dispatchEvent(new Event('change',{bubbles:true}));"
                    " return this.value; }"
                ),
                "arguments": [{"value": text}],
            },
        )
        landed = outcome["result"].get("value")
        if landed != text:
            raise ValueError(
                f"field did not accept the text (wanted {text!r}, holds {landed!r})"
            )
        return f"typed into {node.role} {node.name or node.anchor!r}"

    def submit_form(self, ref: int) -> str:
        """Submit the form a field belongs to, as pressing Enter would."""
        node = self.resolve(ref)
        cdp, object_id = self._handle(node)
        cdp.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": "function(){ if (this.form) this.form.submit(); }",
            },
        )
        self.page.wait_for_load_state("domcontentloaded")
        self.page.wait_for_timeout(250)
        return "submitted the form"

    def select_option(self, ref: int, value: str) -> str:
        """Choose an option by its value *or* its visible label, and verify it took.

        The snapshot shows a dropdown's visible label ("13566 (SAVINGS)"), so
        that is what a caller naturally passes back -- but the option's value
        attribute is just "13566". Assigning the label to .value silently does
        nothing: the assignment succeeds, no option matches, and the field
        stays blank. Matching both forms and asserting the result is what turns
        that into an actionable error instead of a silent no-op.
        """
        node = self.resolve(ref)
        cdp, object_id = self._handle(node)
        outcome = cdp.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "returnByValue": True,
                "functionDeclaration": """
                function(wanted){
                  const opts = Array.from(this.options || []);
                  if (!opts.length) return {ok:false, reason:'not a dropdown'};
                  const norm = s => (s||'').trim().toLowerCase();
                  const w = norm(wanted);
                  const opt = opts.find(o => norm(o.value) === w)
                           || opts.find(o => norm(o.textContent) === w)
                           || opts.find(o => norm(o.textContent).startsWith(w))
                           || opts.find(o => w.startsWith(norm(o.value)));
                  if (!opt) return {ok:false, reason:'no option matches',
                                    available: opts.map(o => o.value + ' = ' + o.textContent.trim())};
                  this.value = opt.value;
                  this.dispatchEvent(new Event('change',{bubbles:true}));
                  return {ok: this.value === opt.value, selected: opt.value,
                          label: opt.textContent.trim()};
                }""",
                "arguments": [{"value": value}],
            },
        )
        result = outcome["result"]["value"]
        if not result.get("ok"):
            available = result.get("available")
            raise ValueError(
                f"could not select {value!r}: {result.get('reason')}"
                + (f"; available options: {available}" if available else "")
            )
        return f"selected {result['selected']} ({result['label']})"

    def navigate(self, url: str) -> str:
        self.page.goto(url, wait_until="domcontentloaded")
        return f"navigated to {url}"

    def wait(self, milliseconds: int) -> None:
        self.page.wait_for_timeout(milliseconds)

    def screenshot(self, path: str) -> None:
        self.page.screenshot(path=path, full_page=False)

    def close(self) -> None:
        try:
            self._context.close()
            self._browser.close()
        finally:
            self._pw.stop()
