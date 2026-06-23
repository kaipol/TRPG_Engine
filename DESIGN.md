# Design

## Source of truth
- Status: Active
- Last refreshed: 2026-06-09
- Primary product surfaces: Player table, GM console, top-bar dossier viewer, launch/config screen, multiplayer room flow, campaign loader.
- Evidence reviewed: README.md, web/index.html, web/multiplayer.html, server/main.py, server/rag.py, server/multiplayer.py.

## Brand
- Personality: Quiet command center for live TRPG play; technical, focused, and a little arcane.
- Trust signals: Clear engine/provider status, deterministic save/load actions, visible room code for remote play, explicit room-owner API ownership in multiplayer.
- Avoid: Dense admin-style module grids during active narration, decorative controls, and forcing players into editing forms just to read campaign context.

## Product goals
- Goals: Keep in-play actions immediately available; make campaign setup automatic after load; support room-code remote players; keep solo and multiplayer play conceptually consistent as AI-GM plus player-controlled story characters; make world/map/knowledge references readable from the top bar; scope AI usage to the active account or multiplayer room owner; let multiplayer members play through the room owner's AI provider without configuring their own API.
- Non-goals: Marketing landing pages, separate duplicate control surfaces, speech input/output.
- Success signals: A player can load a campaign, choose a role, read world/map/knowledge context from the top bar, and hand actions to AI-GM; a GM can still open the console, start a room, choose a model intentionally, and edit campaign systems without confusing those edit forms with player reading surfaces.

## Personas and jobs
- Primary personas: Solo player choosing a script role; GM/KP managing a session; remote players joining by room code.
- User jobs: Choose a role, submit character actions to AI-GM, prepare campaign data, manage entities/map/timeline when needed, share a live room.
- Key contexts of use: Desktop player table during active play, GM console for advanced control, occasionally shared over LAN or a reachable hosted URL.

## Information architecture
- Primary navigation: Launch screen for campaign/provider/setup and solo/multiplayer entry; player table for play; top-bar dossier viewer for world/map/knowledge/lore/entity/memory reading; GM console and settings modal for advanced management.
- Core routes/screens: Player-first console at /, GM console surface within /, player display, phone display, multiplayer room table.
- Content hierarchy: Scene content and selected player roles first; top-bar read-only dossier for campaign reference second; party/entity state and GM editing tools behind the GM console/settings; automation and destructive management remain in advanced settings.

## Design principles
- Principle 1: The top bar may expose read-only reference surfaces when they reduce play friction, but it should not expose destructive or admin-heavy controls.
- Principle 2: Viewing and editing are separate modes: players read dossier panels; GMs edit the underlying systems in script parsing/advanced management panels.
- Tradeoffs: More top-bar reference buttons add some density, accepted because maps/worldview/knowledge are frequent play aids and the edit controls remain hidden.

## Visual language
- Color: Existing dark neutral panels with restrained status accents.
- Typography: Existing Noto Sans SC and JetBrains Mono for identifiers/model/room codes.
- Spacing/layout rhythm: Dense but grouped controls; avoid nested cards.
- Shape/radius/elevation: Existing small-radius panels and modals.
- Motion: Minimal, only status pulses and loading spinners already in use.
- Imagery/iconography: Phosphor icons already loaded in the page.

## Components
- Existing components to reuse: btn, inp, panel, tag, modal overlays, model picker menu.
- New/changed components: Solo player table, role picker, compact header model picker, top-bar dossier viewer, draggable map viewer, game settings modal, multiplayer room strip, widened responsive launch shell, expanded launch campaign work area.
- Variants and states: Connected/disconnected room, loading, empty model list, public/hidden knowledge documents, map floor switching, advanced settings collapsed.
- Token/component ownership: Keep inline CSS patterns in web/index.html until the app is split into components.

## Accessibility
- Target standard: Practical keyboard and contrast improvements within current inline Vue app.
- Keyboard/focus behavior: Inputs/selects must remain tabbable; dropdowns open only by explicit focus/click.
- Contrast/readability: Preserve current high-contrast text on dark surfaces.
- Screen-reader semantics: Buttons must use meaningful titles where icon-only.
- Reduced motion and sensory considerations: Do not add continuous decorative animation.

## Responsive behavior
- Supported breakpoints/devices: Desktop-first GM console with guarded overflow on small screens.
- Layout adaptations: Header reference controls wrap as a compact group; dossier panels switch from side navigation to horizontal tabs on small screens; launch/config screen uses a wide three-column desktop shell, a two-column tablet layout, and a stacked full-width mobile flow.
- Touch/hover differences: Main commands remain button-sized; dossier maps support drag and wheel/gesture-like zoom; advanced editing tools live in modal lists.

## Interaction states
- Loading: Use existing spinners and status text.
- Empty: Show explicit empty room/model/campaign text.
- Error: Inline error messages for model fetch and multiplayer operations.
- Success: Brief status messages for room creation/join and campaign auto setup.
- Disabled: Disable unsafe actions while network requests are pending.
- Offline/slow network, if applicable: Room status should degrade to disconnected while keeping local play usable.

## Content voice
- Tone: Direct operational Chinese labels for live play; player-facing surfaces should sound like play, not admin.
- Terminology: Use "AI-GM" for the host role, "房间码" for multiplayer, "模型" for chat model, "GM 控制台" for advanced systems.
- Microcopy rules: Avoid explaining obvious shortcuts in the main play surface.

## Implementation constraints
- Framework/styling system: Single inline Vue app in web/index.html; FastAPI backend in server/main.py.
- Design-token constraints: Reuse existing CSS variables and class names.
- Performance constraints: Do not block campaign loading on embedding rebuild; runtime play uses sparse/keyword RAG by default and must not call embedding unless an import, upload, explicit rebuild, or dense management search asks for it; selecting a pre-generated action direction is deterministic and must not call chat models unless the user explicitly asks AI-GM to generate, expand, or judge new content.
- Compatibility constraints: Room-code play reuses existing /api/multiplayer and /ws/rooms endpoints.
- Test/screenshot expectations: Run Python compile checks, frontend script syntax check, and browser smoke when the server is available.

## Open questions
- [ ] Whether remote public deployment should add a mandatory access token beyond existing room/member tokens / owner: product / impact: public hosting security.
