Web Bridge complete usage guide
===============================

Protocol metadata
-----------------

name: webbridge
version: 1.11.5

Web Bridge lets AI control the user's real browser — navigate, click, type,
read, screenshot, and interact with any website using the user's actual login
sessions. Use it whenever the user wants to interact with websites, automate
browser tasks, scrape web content, or perform any action requiring a real
browser. Also use it when the user mentions "browser", "webpage", "open URL",
"screenshot", or asks to read/interact with any website. Use even for
simple-sounding browser requests — the daemon handles all complexity.

Web Bridge
----------

Control the user's real browser (with their login sessions) via a local daemon
at http://127.0.0.1:10086.

Tools
-----

Tool           CLI arguments/options                         Returns / note
navigate       URL [--new-tab] [--group-title TITLE]         {success, url, tabId}. First call opens a tab; group_title sets the group's visible label.
find_tab       URL [--active]                                {success, url, tabId, borrowed}. Re-select a tab this session opened; --active borrows the tab the user is viewing.
snapshot       —                                             {url, title, tree} with @e refs. Accessibility tree (text): use this to read page content and locate elements.
click          SELECTOR                                      {success, tag, text}. Synthetic el.click().
fill           SELECTOR VALUE                                {success, tag, mode}. Works on <input>/<textarea> and [contenteditable] (ProseMirror/Lexical/Slate). mode is "value" or "contenteditable".
mouse_click    SELECTOR                                      {success, x, y, tag, text}. Uses CDP trusted mouse events at the element's center.
evaluate       CODE                                          {type, value}. Supports async/await.
cdp            METHOD [PARAMS_JSON]                          Raw CDP response. Raw chrome.debugger passthrough — what evaluate is to JS, cdp is to CDP. Low-level escape hatch for cases the tools above don't cover.
key_type       TEXT                                          {success, length}. Inserts text at the current focus through CDP.
send_keys      KEYS [--repeat 1-100]                         {success, dispatched, os}. Sends keys and modifier combinations through CDP.
screenshot     [--format png|jpeg] [--quality 0-100]         {format, path, sizeBytes, mimeType}. Optional --selector (@e/CSS) and --path. Returns a file path, not base64.
network        start|stop|list|detail [--filter FILTER]      Request/response data. Optional --request-id ID.
upload         SELECTOR FILE [FILE ...]                      {success, fileCount}.
save_as_pdf    [--paper-format FORMAT] [--landscape]         {path, sizeBytes, mimeType, pageTitle}. Optional --scale, --no-print-background, and --path.
list_tabs      —                                             {success, tabs:[{tabId, url, title, active, groupTitle}]}.
close_tab      —                                             {success, closed: bool}. Close the current tab in the session.
close_session  —                                             {success, closed: int}. Close all tabs in the session; closed is the count.

Tabs and the current tab
------------------------

Single-tab tools (snapshot, click, fill, screenshot, save_as_pdf) act on the
current tab — the one you most recently opened with navigate or selected with
find_tab.

* Opening pages: use --new-tab when pages should coexist (comparing,
  cross-referencing); omit it to send the current tab to a new URL.
* Going back to an earlier tab: call find_tab to make a tab you opened earlier
  in this session the current one again. Pass the tab's full URL — take it from
  list_tabs or the earlier navigate result. A bare root domain (example.com) may
  miss a www.example.com tab, so prefer the exact URL. By default find_tab searches
  only this session's own tabs — it never reaches into the user's other tabs or
  windows.
* Acting on a page the user already has open: pass --active ("use my open X
  tab" / "the X page I'm viewing"). It borrows the tab the user is currently
  viewing (returns borrowed:true); the borrowed tab is operated in place — it
  is not pulled into the session's tab group.
* If find_tab errors with "no tab matching … in this session", the page isn't
  open in this session — navigate with --new-tab instead.

    webbridge k26-research find_tab https://www.example.com --active

Call Format
-----------

Every command carries a session naming the current task. The CLI accepts it as
the first positional argument and constructs the top-level request body itself:

    webbridge SESSION ACTION [ACTION_ARGS...]

macOS / Linux:

    webbridge my-task navigate https://example.com --new-tab --group-title "My task"

Windows PowerShell / cmd:

    webbridge my-task navigate https://example.com --new-tab --group-title "My task"

Pass URLs, selectors, JavaScript, JSON, and text values as separate, properly
quoted shell arguments. webbridge serializes them as UTF-8 JSON internally, so
inline request JSON, shell-specific escaping workarounds, and temporary request
body files are not needed on macOS, Linux, or Windows. Each invocation sends one
request directly to the daemon.

Optional global connection settings go before SESSION:

    webbridge --endpoint http://127.0.0.1:10086/command --timeout 60 my-task snapshot

WEBBRIDGE_URL may also set the default endpoint.

Sessions
--------

One task = one session = one tab group. A session collects every tab the task
opens into one tab group, so the user sees a single group for "what the agent is
doing right now". Pass it as the first positional CLI argument.

* Pick one session name at the task's start, put it on every command, and never
  switch mid-task — even across different sites. Switching session names per
  site is the #1 cause of fragmented tab groups.
* Name it after the task, not the site (camping-research, phone-compare). Use
  multiple sessions only for genuinely unrelated parallel tasks.
* group_title is the human-readable group label — write it in the user's
  language, on the first navigate of the task. Pass it with --group-title.
* When you create the group (the first navigate of a task), tell the user once
  that this task's pages are collected under group «title», and that you'll
  close them whenever they ask.

First tab: set session and a human label in the user's language:

    webbridge docs-research navigate https://www.example.com --new-tab --group-title "Documentation research"

Another site, same task, same session — it joins the same group automatically:

    webbridge docs-research navigate https://developer.mozilla.org --new-tab

Closing is always user-initiated: call close_session only when the user
explicitly asks ("close those", "clear the tabs"). It clears the whole group in
one call.

Screenshots
-----------

The daemon writes the image to disk and returns
{format, path, sizeBytes, mimeType} — never base64, since the model can't read
raw image bytes. Take the .path and open it with the Read tool to actually see
it.

Default: PNG of the visible viewport; the daemon picks a temp path:

    webbridge my-task screenshot

Options are independent: JPEG quality, element-only via @e/CSS selector, or a
custom output path:

    webbridge my-task screenshot --format jpeg --quality 60
    webbridge my-task screenshot --selector @e123
    webbridge my-task screenshot --path /tmp/my-task-page.png

A caller-supplied path is honored verbatim (parent directories are created,
existing file overwritten) — use a unique name to avoid clobbering.
save_as_pdf follows the same rule.

Prefer snapshot over CSS/JS selectors
-------------------------------------

snapshot returns interactive elements with @e refs based on semantic role/name.
Use them directly with click/fill — they survive CSS class hash changes that
break manually-written selectors.

Fall back to evaluate (JS) only when:

* The target has no @e ref in the snapshot.
* You need attributes not in the snapshot (for example, href).
* You need to dispatch complex event sequences, or scroll.

Evaluate Tips
-------------

* Always use compact JSON.stringify(data) — never add null, 2 formatting.
  Indentation and newlines can inflate the response several times over, causing
  truncation during transmission.
* evaluate calls share the page's JS realm — re-declaring the same const/let
  across two calls throws SyntaxError. Wrap in an IIFE for a fresh scope:

    webbridge my-task evaluate '(() => { const x = location.href; return x; })()'

Text input — use `fill`
-----------------------

fill (selector = CSS or @e ref, plus the value) works on <input>/<textarea>
(returns mode: "value") and on [contenteditable] rich editors — ProseMirror,
TipTap, Lexical, Slate, Quill, etc. (returns mode: "contenteditable"), firing
the right input events so the page reacts.

fill is clear-and-insert: existing content is replaced. To append, read the
current value via evaluate, concatenate, then fill with the result.

Form submit / special keys
--------------------------

The original guide submits forms by clicking the submit button directly (click
on the @e ref or selector). This CLI also exposes the execution layer's
send_keys tool, so trusted Enter/Escape input can be sent directly:

    webbridge my-task send_keys Enter
    webbridge my-task send_keys Escape

For a DOM-level event instead, use evaluate:

    webbridge my-task evaluate "document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))"

Trusted mouse and keyboard input
--------------------------------

The extension also exposes three CDP input tools that were implemented by the
browser execution layer but were not listed in the original public guide:

* mouse_click scrolls an element into view, finds the center of its layout box,
  and dispatches mouseMoved, mousePressed, and mouseReleased events. Use it when
  a site rejects the synthetic DOM click action.
* key_type inserts literal text at the current browser focus using
  Input.insertText.
* send_keys dispatches named keys and modifier combinations. Supported names
  include Enter, Escape, Tab, Backspace, Delete, Space, arrow keys, Home, End,
  PageUp, PageDown, F1-F12, letters, and digits. Mod maps to Command on macOS
  and Control on Windows/Linux.

Examples:

    webbridge my-task mouse_click @e42
    webbridge my-task key_type "literal text"
    webbridge my-task send_keys "Mod+A"
    webbridge my-task send_keys "Enter Escape" --repeat 2

Save the current page as PDF
----------------------------

save_as_pdf renders the current page to PDF and returns the file path. All args
are optional:

* --paper-format: letter (default) | a4 | legal | a3 | tabloid
* --landscape: false by default; include the flag to set true
* --scale: 1.0 by default, range [0.1, 2.0]
* print_background: true by default; use --no-print-background to set false
* --path: caller-supplied output path; if absent, the daemon picks a default
  under the OS temp directory using the page title as the filename

    webbridge my-task save_as_pdf --paper-format a4 --landscape --scale 0.8 --path /tmp/my-task-page.pdf

path semantics match screenshot: written verbatim, parent dirs are
auto-created, and existing files are overwritten.

The decoded PDF cap is 100 MB. Above that the daemon refuses; reduce --scale or
split the page.

Known limitations
-----------------

* Sites that strictly check event.isTrusted (some banking portals, captchas)
  ignore click / fill because those fire DOM-level synthetic events
  (isTrusted=false). Try mouse_click or send_keys when the same interaction can
  be expressed as trusted CDP input. Captchas still require manual interaction.
* Cross-origin iframes: fill, click, evaluate, and snapshot operate on the top
  frame. If a target element lives in a same-page iframe from a different origin
  (for example, embedded sandbox demos), navigate to the iframe's URL directly.

If a tool call fails (daemon or extension not ready)
----------------------------------------------------

This CLI is only a client. If the default endpoint can't reach the daemon, the
CLI automatically runs the separately installed daemon's start command and
retries the original request once. HTTP errors, extension errors, action
failures, and custom endpoints do not trigger automatic startup. This project
does not provide stop or restart commands.

* Local help: http://127.0.0.1:10086/

Version mismatches
------------------

If a tool returns an error containing "Please update the Web Bridge
extension", the user's browser extension is older than this CLI/daemon. Don't try to
reconcile versions yourself — just tell the user, in their language, to update
the extension and retry:

* Local help: http://127.0.0.1:10086/
