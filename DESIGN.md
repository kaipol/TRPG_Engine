# Design

## Source of truth
- Status: Active
- Last refreshed: 2026-06-08
- Primary product surfaces: GM console, launch/config screen, multiplayer room flow, campaign loader.
- Evidence reviewed: README.md, web/index.html, web/multiplayer.html, docs/multiplayer_integration.md, server/main.py, server/multiplayer.py.

## Brand
- Personality: Quiet command center for live TRPG play; technical, focused, and a little arcane.
- Trust signals: Clear engine/provider status, deterministic save/load actions, visible room code for remote play.
- Avoid: Dense always-visible module bars, decorative controls, and settings exposed during active narration.

## Product goals
- Goals: Keep in-play actions immediately available; make campaign setup automatic after load; support room-code remote players.
- Non-goals: Marketing landing pages, separate duplicate control surfaces, speech input/output.
- Success signals: A GM can load a campaign, start a room, choose a model intentionally, and run scenes without scanning a crowded toolbar.

## Personas and jobs
- Primary personas: GM/KP running a session; remote players joining by room code.
- User jobs: Prepare campaign data, narrate and branch scenes, manage entities/map/timeline when needed, share a live room.
- Key contexts of use: Desktop GM screen during active play, occasionally shared over LAN or a reachable hosted URL.

## Information architecture
- Primary navigation: Launch screen for campaign/provider setup; game screen for play; settings drawer/modal for advanced management.
- Core routes/screens: GM console at /, player display, phone display, multiplayer room table.
- Content hierarchy: Scene content first; party/entity state second; advanced world data and automation behind settings.

## Design principles
- Principle 1: The top bar should answer only "where am I, what model is active, is the room live, can I save?"
- Principle 2: Advanced systems remain one click away but do not compete with narration controls.
- Tradeoffs: Fewer persistent buttons means one extra click for maps/triggers/lore, accepted to reduce live-play clutter.

## Visual language
- Color: Existing dark neutral panels with restrained status accents.
- Typography: Existing Noto Sans SC and JetBrains Mono for identifiers/model/room codes.
- Spacing/layout rhythm: Dense but grouped controls; avoid nested cards.
- Shape/radius/elevation: Existing small-radius panels and modals.
- Motion: Minimal, only status pulses and loading spinners already in use.
- Imagery/iconography: Phosphor icons already loaded in the page.

## Components
- Existing components to reuse: btn, inp, panel, tag, modal overlays, model picker menu.
- New/changed components: Compact header model picker, game settings modal, multiplayer room strip.
- Variants and states: Connected/disconnected room, loading, empty model list, advanced settings collapsed.
- Token/component ownership: Keep inline CSS patterns in web/index.html until the app is split into components.

## Accessibility
- Target standard: Practical keyboard and contrast improvements within current inline Vue app.
- Keyboard/focus behavior: Inputs/selects must remain tabbable; dropdowns open only by explicit focus/click.
- Contrast/readability: Preserve current high-contrast text on dark surfaces.
- Screen-reader semantics: Buttons must use meaningful titles where icon-only.
- Reduced motion and sensory considerations: Do not add continuous decorative animation.

## Responsive behavior
- Supported breakpoints/devices: Desktop-first GM console with guarded overflow on small screens.
- Layout adaptations: Header controls wrap only through grouped menus; top bar should not require all modules to fit.
- Touch/hover differences: Main commands remain button-sized; advanced tools live in modal lists.

## Interaction states
- Loading: Use existing spinners and status text.
- Empty: Show explicit empty room/model/campaign text.
- Error: Inline error messages for model fetch and multiplayer operations.
- Success: Brief status messages for room creation/join and campaign auto setup.
- Disabled: Disable unsafe actions while network requests are pending.
- Offline/slow network, if applicable: Room status should degrade to disconnected while keeping local play usable.

## Content voice
- Tone: Direct operational Chinese labels for live GM use.
- Terminology: Use "房间码" for multiplayer, "模型" for chat model, "高级设置" for hidden systems.
- Microcopy rules: Avoid explaining obvious shortcuts in the main play surface.

## Implementation constraints
- Framework/styling system: Single inline Vue app in web/index.html; FastAPI backend in server/main.py.
- Design-token constraints: Reuse existing CSS variables and class names.
- Performance constraints: Do not block campaign loading on embedding rebuild.
- Compatibility constraints: Room-code play reuses existing /api/multiplayer and /ws/rooms endpoints.
- Test/screenshot expectations: Run Python compile checks, frontend script syntax check, and browser smoke when the server is available.

## Open questions
- [ ] Whether remote public deployment should add a mandatory access token beyond existing room/member tokens / owner: product / impact: public hosting security.
