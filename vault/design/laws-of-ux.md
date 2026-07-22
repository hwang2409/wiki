---
type: reference
tags: [design, ux, frontend]
created: 2026-07-21
updated: 2026-07-21
---

# Laws of UX (themed reference)

Paraphrased summary of the 30 principles curated at [lawsofux.com](https://lawsofux.com/) (Jon Yablonski). Source content is a public survey of well-known cognitive-psychology and HCI findings — the site's book expands the same material. All definitions and takeaways below are re-stated in own words; use the source for the canonical write-ups and academic citations. Mining pass 2026-07-21 via workflow `laws-of-ux-mining`.

Purpose here: quick reference when designing any UI (wiki-app, agent dashboards, tooling frontends). Grouped by theme so you can find the relevant law by *what problem you're solving*, not just alphabetically.

## One-line mnemonics (all 30)

- **Aesthetic-Usability Effect** — Pretty feels usable; polish buys forgiveness for real friction.
- **Choice Overload** — Too many options at once paralyzes decisions and tanks satisfaction.
- **Chunking** — Group related bits into small clusters so the brain can hold them.
- **Cognitive Bias** — Users decide by shortcuts, not analysis; design for that.
- **Cognitive Load** — Every UI element taxes working memory; cut extraneous effort ruthlessly.
- **Doherty Threshold** — Under 400ms feels alive; anything slower breaks flow.
- **Fitts's Law** — Bigger and closer targets are faster and less error-prone to hit.
- **Flow** — Match challenge to skill with instant feedback to lock focus.
- **Goal-Gradient Effect** — Motivation accelerates as the finish line comes into view.
- **Hick's Law** — More options mean slower decisions; prune to speed up choice.
- **Jakob's Law** — Users expect your UI to work like every other one they know.
- **Law of Common Region** — A shared enclosure signals these items form one group.
- **Law of Proximity** — Nearness alone tells the eye these things belong together.
- **Law of Prägnanz** — The eye defaults to the simplest possible reading of a scene.
- **Law of Similarity** — Shared visual traits make items read as one group.
- **Law of Uniform Connectedness** — Visible connections group items harder than proximity or similarity.
- **Mental Model** — Users act on the model they imported from other products.
- **Miller's Law** — Working memory holds about seven chunks; group accordingly.
- **Occam's Razor** — Prefer the design with fewer parts and fewer assumptions.
- **Paradox of the Active User** — Users skip docs and dive in; teach inside the interface.
- **Pareto Principle** — A small share of features drives most of the value.
- **Parkinson's Law** — Tasks bloat to fill whatever time or fields you allow.
- **Peak-End Rule** — People remember the emotional peak and the final moment.
- **Postel's Law** — Accept input generously; emit output strictly and predictably.
- **Selective Attention** — Users filter to their goal; irrelevant pixels get ignored.
- **Serial Position Effect** — First and last items in a series stick; middles fade.
- **Tesler's Law** — Complexity is conserved; absorb it so users do not.
- **Von Restorff Effect** — One item that breaks the pattern is the one remembered.
- **Working Memory** — Short-lived mental workspace; prefer recognition over recall always.
- **Zeigarnik Effect** — Unfinished tasks nag the mind and pull users back.

## Themes

### Perception & grouping

*Unifying principle:* The eye stitches raw pixels into groups and shapes before the brain reads meaning; spatial nearness, shared enclosure, connectors, matching style, and simplicity dictate what 'belongs together.'

*Builder prompt:* If I squinted at this screen, would the visual groupings match the actual semantic relationships?

Member laws:

#### Law of Proximity
*Core:* People automatically perceive elements placed close together as belonging to the same group. Spatial nearness signals relationship before any label, color, or line does.
*Origin:* Rooted in early 20th-century Gestalt psychology as one of the grouping principles (alongside Similarity, Continuity, Closure, and Connectedness) tied to the broader concept of Prägnanz.
*Takeaways:*
- Use whitespace, not borders, as the primary grouping tool — proximity does the work before you reach for dividers.
- Items that share function should sit close; items that don't should be pushed apart, or users will infer a relationship that isn't there.
- Tighten spacing within a group and widen spacing between groups to make structure legible at a glance.
- Grouping via proximity speeds scanning and comprehension, so use it to reduce cognitive load on dense screens.
- Treat spacing as information architecture: layout gaps encode hierarchy just as much as headings do.
*Caveats:*
- Accidental proximity creates accidental groups — unrelated elements placed near each other will be read as related.
- Over-reliance on borders/backgrounds while ignoring spacing produces cluttered layouts where proximity fights the visual containers.
- Uniform spacing everywhere destroys grouping cues; without contrast between intra-group and inter-group gaps, everything reads as one flat list.
*Examples:*
- Google search results: consistent vertical spacing separates each result into a distinct scannable unit.
- Form design: labels sit closer to their input than to the next field, so the label-input pairing is unambiguous.
- Navigation menus: related links clustered with tight spacing, section breaks marked by larger gaps rather than dividers.
- Card grids and dashboards: metrics belonging to the same entity clustered inside a shared spacing envelope.

#### Law of Common Region
*Core:* People perceive elements as belonging together when those elements share an enclosed area — a border, a background, or any clearly bounded region signals "these things are one group."
*Origin:* Rooted in early-20th-century Gestalt psychology's grouping principles (Prägnanz); Common Region itself is typically attributed to Stephen Palmer (1992).
*Takeaways:*
- Wrap related items in a shared container (card, panel, box) to communicate grouping without extra labels.
- A subtle background fill is enough — borders aren't required to establish a common region.
- Use common region to impose structure on dense layouts so users can parse relationships at a glance.
- Combine with proximity: enclosure overrides distance, so a boundary can group items that sit farther apart.
- Reserve enclosure for elements that genuinely belong together; every card implies a semantic unit.
*Caveats:*
- Over-boxing everything creates visual noise and destroys the hierarchy enclosure is meant to create.
- Nested regions can confuse users about which grouping matters most.
- Enclosure can accidentally imply relationships between items that are only visually adjacent, not semantically related.
*Examples:*
- Card layouts on dashboards where each card bundles a metric with its label and trend.
- Settings screens that group related toggles inside a bordered or tinted panel.
- Form sections separated by background color to distinguish billing vs shipping fields.
- Notification or comment threads visually grouped by a shared background block.

#### Law of Uniform Connectedness
*Core:* When UI elements share a visible connection — a shared background, an enclosing frame, or a literal line between them — people read them as belonging together, more strongly than if they were merely near each other or styled alike.
*Origin:* Rooted in Gestalt psychology's grouping principles; the "uniform connectedness" principle specifically was articulated by Stephen Palmer and Irvin Rock (1994) as an addition to the classic Gestalt set.
*Takeaways:*
- Wrap related controls or content in a shared container (card, frame, background fill) to signal they belong to the same group.
- Use explicit connectors — lines, arrows, brackets — when the relationship between two elements needs to be made unambiguous.
- Connectedness beats proximity and similarity: if items must feel grouped, a visible link is the strongest cue.
- Reserve enclosure and connectors for genuine relationships; boxing unrelated items forces users to invent a meaning that isn't there.
- Combine with hierarchy — the container itself carries weight, so nest and layer connections carefully to avoid over-boxing the page.
*Caveats:*
- Overusing borders, cards, and dividers creates visual clutter and can fragment a layout into competing regions.
- Falsely connecting unrelated elements misleads users about structure and function.
- Heavy containers can compete with content hierarchy, making the frame more prominent than what's inside.
*Examples:*
- Google search results using bordered cards around featured snippets and video results to set them apart from standard blue-link results.
- Settings pages that group related toggles inside a shared rounded card so the section reads as one unit.
- Form fieldsets that visually enclose a set of inputs (billing address, shipping address) to show they belong to one logical block.
- Diagram tools drawing arrows between nodes to make dependency or flow relationships explicit.

#### Law of Similarity
*Core:* Elements that share visual traits — color, shape, size, orientation — are perceived as belonging together, even when they aren't physically adjacent. The eye stitches them into a single group by default.
*Origin:* Rooted in early 20th-century Gestalt psychology; similarity is one of the classic grouping principles alongside Proximity, Continuity, Closure, and Connectedness, all governed by the Prägnanz principle.
*Takeaways:*
- Use shared visual attributes (color, shape, size, motion) to signal that items belong to the same category or perform the same function.
- Conversely, break similarity when items must NOT be confused for one another — differentiate destructive actions, disabled states, and non-interactive text.
- Make links and interactive elements look distinct from body copy so users don't group them with static text.
- Consistency of styling across a UI is itself a similarity cue — it teaches users what to expect from a class of elements.
- Combine similarity with proximity: aligned, evenly-spaced items with matching styling read as one coherent set.
*Caveats:*
- Accidental similarity misleads users — e.g., static text styled like a link, or two unrelated buttons sharing a color, creates false groupings.
- Over-uniform styling can flatten hierarchy and hide the primary action in a sea of look-alike elements.
- Relying on a single similarity cue like color alone fails for color-blind users; pair with shape, icon, or label.
*Examples:*
- Grouping navigation links with a shared color/weight so they read as one menu, distinct from body prose.
- Styling all primary CTAs identically across a product so users learn the pattern.
- Card grids where uniform card shape and size signal that each item is a peer in the same collection.
- Form field styling — matching input borders and labels group inputs as one form while a differently-styled submit button stands apart.

#### Law of Prägnanz
*Core:* When faced with ambiguous or visually complex input, people default to reading it as the simplest, most orderly shape available, because that interpretation is the cheapest for the brain to process.
*Origin:* Coined by Gestalt psychologist Max Wertheimer in 1910, after watching alternating railroad-crossing lights read as one moving light — a founding observation of Gestalt perception theory.
*Takeaways:*
- Reduce visual noise: the eye actively hunts for order in complex scenes, so simpler compositions land faster.
- Prefer unified silhouettes over fragmented parts — the visual system will collapse pieces into a single form anyway, so design toward that reading.
- Simple figures are recalled and processed more reliably than ornate ones, so lean on primitive shapes for anything that must be understood at a glance.
- Treat simplicity as a cognitive-load lever, not just an aesthetic choice — 'clean' UI is literally cheaper to perceive.
- When a layout feels heavy, look for structure the user's brain is being forced to invent, and pre-resolve it in the design.
*Caveats:*
- Oversimplifying can strip out meaningful distinctions — users may collapse two different elements into one 'simplest form' and miss the difference.
- The law describes what the eye defaults to, not what's correct — the simplest reading isn't always the intended reading, so test that the shortcut lands on the right meaning.
- Applies to perception of form, not to information architecture — 'make it simpler' can be misused as a blanket excuse to remove necessary detail.
*Examples:*
- Logos built from a single unified silhouette (rather than many small parts) so the mark resolves instantly.
- Icon sets drawn from primitive geometric shapes — circles, squares, triangles — so they read at small sizes.
- Simplified illustrations and diagrams where complex systems are abstracted into a few clean shapes for faster comprehension.
- Reducing decorative flourishes in a UI so the underlying structure (grid, hierarchy) is what the eye locks onto first.

#### Chunking
*Core:* Chunking is the practice of splitting information into small, meaningful groups so people can perceive, understand, and remember it more easily. In UI, it means arranging content into visually distinct clusters that mirror how the brain processes and holds information.
*Origin:* Coined by George A. Miller in his 1956 paper "The Magical Number Seven, Plus or Minus Two," which observed short-term memory limits that classical information theory couldn't explain.
*Takeaways:*
- Group related items into discrete visual modules so users can scan instead of read
- Use clear hierarchy (size, weight, position) to signal which chunks matter most
- Apply separation devices — whitespace, dividers, cards, sections — to make group boundaries obvious
- Keep each chunk small enough to fit comfortably in working memory (echo Miller's ~7 ± 2 limit)
- Let chunk structure mirror the relationships in the underlying content, not arbitrary layout convenience
- Pair chunking with strong labels so users can jump straight to the group they need
*Caveats:*
- Over-chunking fragments content and creates visual noise that hurts scannability
- Chunks that don't reflect real semantic relationships mislead users about what belongs together
- Weak separation between chunks collapses the benefit — groupings must be visually unambiguous
- Chunking cannot rescue content that is fundamentally too much; it must be paired with prioritization
*Examples:*
- Phone numbers formatted as 3-3-4 digit groups instead of a 10-digit string
- Long forms split into labeled sections (Contact, Shipping, Payment) rather than one flat field list
- Navigation menus grouped by category with headers and dividers
- Dashboard laid out as distinct cards, each holding one metric or module
- Article pages using subheadings, short paragraphs, and bulleted lists to break up prose

### Memory & attention budget

*Unifying principle:* Users have a tiny, filtered attention and memory workspace; the design must budget for it — offload state to the UI, prioritize the vital slice, and make the important thing pop without shouting everywhere.

*Builder prompt:* What am I asking the user to hold in their head, and what could the interface remember for them instead?

Member laws:

#### Cognitive Load
*Core:* Every interface taxes a user's limited working memory; the mental effort required to parse, understand, and act on what's on screen determines whether the experience feels effortless or overwhelming. Good design minimizes the load unrelated to the actual task.
*Origin:* John Sweller, 1988 — "Cognitive Load Theory, Learning Difficulty, and Instructional Design"; builds on George Miller's earlier information-processing work.
*Takeaways:*
- Treat working memory as a hard budget — when incoming info exceeds it, users miss details, slow down, and disengage.
- Separate intrinsic load (effort tied to the actual task) from extraneous load (effort caused by the design itself), and ruthlessly cut the latter.
- Strip out decorative or superfluous UI elements that consume attention without advancing the user's goal.
- Chunk information into small, scannable groups so users can process one unit at a time instead of a wall of content.
- Lean on familiar patterns and conventions so users don't burn cycles learning new mental models.
- Progressive disclosure: reveal complexity only when it's needed, keeping the default surface minimal.
*Caveats:*
- Not all load is bad — intrinsic load is inherent to meaningful tasks; over-simplifying can strip away needed context.
- Hiding too much behind progressive disclosure can trade extraneous load for navigation load and discoverability problems.
- Minimal aesthetics alone don't equal low cognitive load; ambiguous icons and hidden affordances can look clean but increase effort.
*Examples:*
- Checkout flows that break a long form into short, single-purpose steps instead of one dense page.
- Search interfaces that surface a few high-quality suggestions rather than exhaustive filter panels up front.
- Dashboards that highlight one primary metric and tuck secondary data behind drill-downs.
- Onboarding that introduces features contextually as users need them rather than in an upfront tour.

#### Working Memory
*Core:* Working memory is the short-lived mental workspace where people hold and juggle information while completing a task. It has tight capacity and time limits, so interfaces should minimize what users must keep in their heads.
*Origin:* Term coined by George A. Miller, Eugene Galanter, and Karl H. Pribram in the 1960s; formalized as a "short-term store" by Richard C. Atkinson and Richard M. Shiffrin in 1968. Earlier neuroscience groundwork by Eduard Hitzig and David Ferrier.
*Takeaways:*
- Assume users can only hold roughly 4-7 chunks of info at a time, and only for 20-30 seconds — show just what's relevant to the current step.
- Design for recognition, not recall: surface prior context (visited-link styles, breadcrumbs, recently viewed) instead of asking users to remember it.
- Push memory work onto the system: persist selections, inputs, and comparisons across screens so the user doesn't re-derive them.
- Use side-by-side patterns (comparison tables, sticky summaries) when users must weigh multiple options against each other.
- Chunk related information into meaningful groups to stretch effective capacity.
*Caveats:*
- Overloading a screen with 'just in case' content quickly blows past working-memory limits and increases cognitive load.
- Forcing users to remember values from a previous screen (e.g. an ID, a price) to use on the next screen is a common working-memory failure.
- Hiding previously seen state (no visited styling, no history, no breadcrumbs) forces recall where recognition would have worked.
*Examples:*
- Visited-link styling and breadcrumb trails so users recognize where they've already been.
- Comparison tables that hold multiple products' specs on one screen instead of asking users to remember them across pages.
- Persistent order summaries and sticky cart totals during multi-step checkout.
- Autofill, saved form values, and 'recently viewed' rails that keep prior context available without recall.

#### Miller's Law
*Core:* Working memory holds only about seven items (give or take two) at a time, so interfaces should respect that limit by grouping related information into digestible chunks rather than dumping long undifferentiated lists on users.
*Origin:* George A. Miller, 1956 (paper on immediate memory span and absolute judgment capacity)
*Takeaways:*
- Chunk long strings, lists, and forms into small logical groups so users can process and recall them.
- Treat 7 plus or minus 2 as a rough heuristic about short-term memory, not a hard cap on menu items or nav links.
- Design for the individual's context: prior knowledge and expertise change how much someone can hold at once.
- Use visual grouping (whitespace, dividers, sections) to make chunks obvious at a glance.
- When information must be dense, offload from memory to the interface: labels, breadcrumbs, persistent context.
*Caveats:*
- The 'magical number seven' is widely misapplied as a rule that menus, tabs, or options must never exceed seven — Miller's paper was about memory span, not UI element counts.
- Capacity varies by person and situation; treating it as a fixed universal ceiling leads to arbitrary constraints.
- Chunking helps only when the groupings are meaningful to the user — arbitrary grouping adds noise without aiding recall.
*Examples:*
- Formatting phone numbers as 3-3-4 digit chunks instead of a 10-digit run.
- Grouping remote-control buttons (e.g., Apple TV remote) by function so users scan clusters rather than individual keys.
- Sectioning long signup forms into labeled steps or fieldsets.
- Splitting long navigation menus into categorized submenus with headings.

#### Selective Attention
*Core:* People filter the environment down to a narrow slice of stimuli that matches their current goal, ignoring almost everything else. Interfaces have to earn a spot inside that filtered slice or they will simply not be seen.
*Origin:* Rooted in mid-20th-century attention research: E. Colin Cherry (1953, cocktail party effect), Donald Broadbent (1958, filter/bottleneck theory), Anne Treisman (1960, attenuation model), and Deutsch & Deutsch (1963, late-selection theory).
*Takeaways:*
- Design for a filtered viewer: assume users will drop anything not tied to their goal, so make the goal-relevant path visually louder than everything else.
- Actively guide attention with hierarchy, contrast, and placement instead of hoping users will scan the whole screen.
- Avoid ad-like styling (banner shapes, promotional layouts, adjacency to real ads) for anything users actually need to see, or it will be tuned out.
- When you change the UI, add strong visual cues to the change site; silent changes can be missed entirely (change blindness).
- Don't stack multiple simultaneous changes or animations that fight for the same attention budget - they cancel each other out.
- Cut noise: fewer competing elements means the important one actually gets through the filter.
*Caveats:*
- Banner blindness: content that looks like an ad (or sits near ads) gets ignored even when it's important.
- Change blindness: meaningful UI updates can go completely unnoticed if not signaled, breaking flows that assume the user saw the change.
- Competing simultaneous changes can mask each other, so a well-intended animation or update can defeat another.
- Attention is a limited resource - overloading a screen with signals collapses back into noise and nothing stands out.
*Examples:*
- Primary CTAs styled with distinct color/size so they survive the user's goal-driven filter.
- Keeping important notices, cookie banners, or promos away from ad slots and off ad-like visual treatments.
- Highlighting a newly added item, updated field, or cart change with motion, color, or a badge so change blindness doesn't hide it.
- Sequencing UI transitions one at a time rather than animating several regions in parallel.
- Stripping decorative chrome around a key action so the eye lands on it first.

#### Von Restorff Effect
*Core:* When several similar items appear together, the one that visually breaks the pattern is the one people remember. Also called the "isolation effect."
*Origin:* Hedwig von Restorff, German psychiatrist, 1933 memory research on isolated items among categorically similar ones.
*Takeaways:*
- Give the single most important element (primary CTA, key stat, active state) a distinctive visual treatment so it stands out from its neighbors.
- Emphasize sparingly — if too many things shout, nothing stands out and highlighted elements start reading as ads.
- Never encode contrast through color alone; pair it with weight, size, iconography, or shape so it works for color-blind and low-vision users.
- If you use motion to create contrast, respect reduced-motion preferences and users with vestibular sensitivity.
- Contrast is relative — the isolated item only pops because everything around it is uniform, so preserve visual consistency in the surrounding set.
*Caveats:*
- Overusing highlights nukes the effect — competing standouts cancel each other out.
- Overly loud emphasis (bright fills, badges, animation) can be mistaken for advertising and get ignored via banner blindness.
- Color-only differentiation excludes users with color vision deficiency.
- Motion-based emphasis can harm users with motion sensitivity if not gated behind reduced-motion checks.
*Examples:*
- A single filled primary button among ghost/secondary buttons in a form.
- One pricing tier card scaled up and color-accented as the 'recommended' plan.
- Highlighting the current step in a multi-step wizard so it visually separates from completed and upcoming steps.
- A promoted search result or featured row styled distinctly from a uniform list.
- Using a differently colored data point in a chart to draw the eye to the value being discussed.

#### Serial Position Effect
*Core:* People remember items at the beginning and end of a sequence more reliably than items stuck in the middle, thanks to the primacy and recency effects.
*Origin:* Coined by German psychologist Hermann Ebbinghaus (late 1800s).
*Takeaways:*
- Put your most critical actions at the far-left and far-right edges of lists, nav bars, or toolbars where recall is strongest.
- Bury lower-priority items in the middle of a series — they get less working- and long-term-memory real estate anyway.
- Design nav so the first and last items carry weight (e.g., logo/home at start, primary CTA or account at end).
- Order items intentionally by importance, not alphabetically or by convenience, when memorability matters.
- Apply the same shape to onboarding steps, feature carousels, and menu items — front-load and back-load the memorable moments.
*Caveats:*
- Effect weakens when list length or cognitive load is very high — middle items essentially vanish from recall.
- Overloading both ends with heavy actions can create visual imbalance or decision fatigue if every item screams for attention.
- Recency can be disrupted by interference (something happening after the list), so it's fragile in fast-changing UIs.
*Examples:*
- Primary navigation bars placing the brand/logo on the far left and account or primary CTA on the far right.
- Mobile bottom tab bars where Home and Profile bookend less-used tabs.
- E-commerce category menus ordering flagship categories at the top and bottom of a long list.
- Onboarding flows front-loading the value prop and closing with the sign-up CTA.

### Decision cost & choice architecture

*Unifying principle:* Every option is a tax; decision quality and speed degrade with option count and complexity, so ruthlessly prune, pre-rank, and highlight the vital few — while respecting that users decide via biased shortcuts, not rational analysis.

*Builder prompt:* How many decisions am I forcing here, and can I collapse, default, or defer any of them?

Member laws:

#### Hick's Law
*Core:* The time a person needs to reach a decision grows as the number and complexity of available options increases. More choices means slower, more effortful selection.
*Origin:* William Edmund Hick and Ray Hyman, 1952 (also called the Hick-Hyman Law)
*Takeaways:*
- When speed of response matters, cut the option set down to the essentials.
- Chunk complex flows into smaller sequential steps so users only decide one thing at a time.
- Reduce decision friction by pre-highlighting a recommended or default option.
- Use progressive onboarding to reveal features gradually instead of dumping everything at once on new users.
- Push interface complexity elsewhere (another device, a later step) when the primary surface needs to stay focused.
*Caveats:*
- Simplification can go too far — stripping options until the UI becomes abstract or unclear hurts users more than the extra choices would.
- The law measures decision time, not decision quality; fewer options isn't automatically better if it removes something users need.
- Highlighting a 'recommended' choice can shade into manipulation if the recommendation serves the business rather than the user.
*Examples:*
- Google's homepage strips away nav and content so the only real decision is what to type in the search box.
- The Apple TV remote pushes complexity onto the on-screen interface, leaving only a handful of physical buttons to choose between.
- Slack's onboarding bot introduces features one at a time instead of exposing the full product surface on first login.
- Checkout flows that split shipping, payment, and review into separate steps rather than one giant form.
- Landing pages with a single primary CTA above the fold instead of competing buttons.

#### Choice Overload
*Core:* When users face too many options at once, their ability to decide degrades and the whole experience feels worse — often called the "paradox of choice."
*Origin:* Coined as "overchoice" by Alvin Toffler in Future Shock (1970).
*Takeaways:*
- Cap the number of options a user compares at once; more choices erode both decision quality and satisfaction.
- When choice is unavoidable, structure it for direct comparison (e.g. side-by-side pricing tiers with a clear differentiator per column).
- Pre-rank the set for the user: default sort, editor picks, or a highlighted 'most popular' choice removes cognitive load.
- Provide filters, search, and progressive disclosure so users narrow a large catalog down to a manageable shortlist before deciding.
- Treat choice architecture as a design decision, not a data dump — every extra option is a tax on the user.
*Caveats:*
- Related to but distinct from Hick's Law, which specifically models decision time as a function of option count/complexity.
- The page itself lists no experimental limits, but 'more choice = worse' is context-dependent — expert users, high-stakes purchases, and configurable tools can legitimately need broad option sets.
- Aggressive curation can flip into paternalism or hide options users actually want; defaults and filters should be transparent and overridable.
*Examples:*
- Pricing pages that show 3 tiers side-by-side with one marked 'Recommended' instead of a long feature matrix upfront.
- E-commerce category pages that lead with a filter/search rail and a 'best sellers' shelf before the full grid.
- Streaming and content apps that surface a curated 'Top Picks for You' row rather than dumping the entire catalog.
- Signup flows that hide advanced settings behind a 'More options' disclosure to keep the primary path short.

#### Pareto Principle
*Core:* A small share of causes (roughly 20%) tends to produce the majority of effects (roughly 80%), so effort concentrated on that vital minority yields outsized results.
*Origin:* Named after economist Vilfredo Pareto, who in the early 1900s observed that ~80% of Italian land was owned by ~20% of the population.
*Takeaways:*
- Assume inputs and outputs are unevenly distributed; don't treat all features or users as equally valuable.
- Identify the vital few features, flows, or user segments that drive most engagement and prioritize those first.
- Allocate design and engineering effort toward the highest-impact 20% instead of spreading resources evenly.
- Use the 80/20 lens as a heuristic for triage and roadmap decisions, not as a literal ratio.
- Measure usage to locate the disproportionately valuable interactions before optimizing them.
*Caveats:*
- The 80/20 split is approximate — real distributions vary and shouldn't be treated as exact.
- Optimizing only for the majority can neglect edge cases, accessibility needs, or important minority users.
- Misidentifying the vital 20% (e.g., relying on assumptions instead of data) sends effort in the wrong direction.
- Long-tail features can still matter for retention, differentiation, or specific power users even if lightly used.
*Examples:*
- Prioritizing polish on high-traffic flows (signup, checkout, home) over rarely used settings screens.
- Focusing performance work on the handful of pages that account for most sessions.
- Investing in the few features that generate the bulk of engagement or revenue.
- Trimming a bloated UI by surfacing the small set of controls most users actually reach for.

#### Occam's Razor
*Core:* When two designs solve a problem equally well, prefer the one that carries fewer assumptions and moving parts. In UX, this becomes a bias toward stripping an interface down until removing anything more would break it.
*Origin:* Attributed to William of Ockham, English Franciscan friar and philosopher (c. 1287–1347); Latin lex parsimoniae.
*Takeaways:*
- The cheapest way to manage UI complexity is to keep it from ever entering the design.
- Audit every element and cut whatever isn't carrying weight for the core task.
- Treat a design as finished only when the next removal would start damaging function.
- When two solutions look equally good, ship the one with fewer parts and fewer assumptions.
- Use reduction as an active design phase, not a cleanup step at the end.
*Caveats:*
- Parsimony only applies when options predict/perform equally well — don't cut features that were genuinely doing work.
- Minimalism as aesthetic is not the same as minimalism as function; hiding essentials to look clean violates the principle.
- 'Fewest assumptions' is a judgment call — over-reduction can strip context users actually rely on.
*Examples:*
- Trimming a signup form to only the fields absolutely required to create an account.
- Choosing a single primary CTA per screen instead of competing buttons of equal weight.
- Removing decorative UI chrome (dividers, icons, labels) that don't aid comprehension.
- Collapsing redundant navigation entries that route users to overlapping destinations.

#### Cognitive Bias
*Core:* Cognitive biases are predictable, systematic distortions in how people judge information and make choices, driven by mental shortcuts the brain uses in place of full analysis. In UX, they shape how users perceive, interpret, and act on an interface — often in ways that diverge from "rational" behavior.
*Origin:* Coined by Amos Tversky and Daniel Kahneman in 1972, after studying how people fail to reason intuitively about probability and magnitude.
*Takeaways:*
- Assume users take mental shortcuts — design for fast, heuristic-driven judgment rather than careful deliberation.
- Audit your own designs for bias: your intuitions about what users want are themselves biased and need testing.
- Anticipate confirmation bias — users notice and remember evidence that fits what they already believe about your product.
- Naming a bias doesn't neutralize it; build guardrails (defaults, friction, disconfirming feedback) into the flow instead of relying on user vigilance.
- Match interface affordances to how people actually decide (comparisons, anchors, social proof), not to an idealized rational actor.
- Use awareness of biases ethically — to reduce user error, not to manipulate people into choices against their interest.
*Caveats:*
- Awareness of a bias reduces but does not remove it — designers and users both stay susceptible.
- Exploiting biases (dark patterns) is the same mechanism used ethically; the line is user benefit vs. user harm.
- Confirmation bias makes it hard to falsify assumptions in user research — plan studies that can actually disprove your hypothesis.
- Heuristics are usually adaptive; treating every shortcut as an error leads to over-engineered, friction-heavy UI.
*Examples:*
- Confirmation bias: users interpret ambiguous UI copy as supporting their existing view of a product's stance.
- Anchoring: the first price shown on a pricing page frames how users judge every other tier.
- Default bias: pre-selected options (newsletter opt-in, plan choice) capture the majority of users regardless of stated preference.
- Social proof: review counts and 'X people bought this' nudges shortcut a full evaluation.
- Loss aversion framing: 'don't miss out' copy outperforms equivalent gain-framed copy.

### Time, feedback & flow

*Unifying principle:* Speed, motor economy, visible progress, and open loops determine whether users stay locked into a task; sub-400ms feedback, big close targets, matched challenge, and progress momentum keep the loop closed.

*Builder prompt:* Where does the user wait, aim, or lose sight of progress — and how do I collapse or visualize that gap?

Member laws:

#### Doherty Threshold
*Core:* Interactions between a person and a computer should complete in under about 400ms so neither is left waiting on the other; hitting that pace keeps attention locked and productivity climbing.
*Origin:* Walter J. Doherty and Ahrvind J. Thadani, IBM Systems Journal, 1982 — reset the industry benchmark from a 2-second response target down to 400ms and called sub-threshold systems "addicting."
*Takeaways:*
- Aim to acknowledge or complete user actions within roughly 400ms to keep flow and focus intact.
- When real work can't finish that fast, lean on perceived performance — skeletons, optimistic UI, instant echoes — to hide latency.
- Use animation during background loading so the user has something to watch instead of a dead screen.
- Show a progress bar for longer waits; even an imprecise one makes the wait feel shorter and more tolerable.
- Consider deliberately slowing down instantaneous processes when speed would undermine trust or perceived thoroughness (e.g. security scans, financial calculations).
*Caveats:*
- Chasing the 400ms number literally can lead to cutting corners on work that genuinely needs time — perceived performance techniques are the escape hatch, not raw speed.
- Artificial delays are a double-edged sword: helpful for trust in some flows, but insulting when users know the system could be instant.
*Examples:*
- Search-as-you-type interfaces that echo keystrokes and stream partial results well under half a second.
- Skeleton screens on content-heavy apps (feeds, dashboards) that appear instantly while data loads in the background.
- Progress bars on file uploads or exports so a multi-second wait feels bounded rather than open-ended.
- Intentional pacing on things like antivirus scans, credit checks, or AI 'thinking' animations to signal that real work is happening.

#### Fitts's Law
*Core:* The time it takes to point at or tap a target grows with the distance to that target and shrinks as the target gets bigger. In UI terms: bigger, closer controls are faster and less error-prone to hit.
*Origin:* Paul Fitts, 1954 — psychologist studying the human motor system.
*Takeaways:*
- Size interactive targets (buttons, links, tap zones) large enough to acquire without careful aiming — especially on touch devices.
- Place controls close to where the user's attention or cursor already is; every pixel of travel costs time.
- Leave enough spacing between adjacent targets so users don't mis-tap neighbors while moving quickly.
- Exploit 'infinite' edges and corners of the screen — they act as arbitrarily large targets because the pointer stops there.
- Prioritize size and proximity for the most frequently used or highest-stakes actions in a flow.
*Caveats:*
- Speed-accuracy trade-off: shrinking targets or forcing faster movement pushes error rates up sharply.
- Multiple formal variants of the law exist; treat it as a design heuristic, not a precise predictive equation for every interaction.
- Making everything huge is not the goal — oversized targets crowd the layout and steal attention from the primary action.
*Examples:*
- Enlarging primary CTA buttons on mobile so thumbs can hit them without zoom or precision.
- Placing OS menu bars flush against the top edge of the screen so the cursor can't overshoot.
- Right-click / radial context menus that surface options directly under the cursor instead of in a distant toolbar.
- Increasing hit areas around small icons (close buttons, checkboxes) beyond their visible bounds.
- Grouping related toolbar actions near the current selection or caret rather than in a far-off panel.

#### Flow
*Core:* Flow is the absorbed, energized mental state a person enters when an activity's challenge is well-matched to their skill, producing focused involvement and enjoyment. Interfaces that hit this balance keep users engaged rather than bored or frustrated.
*Origin:* Coined by psychologist Mihaly Csikszentmihalyi in 1975, though the underlying idea predates the term across many traditions.
*Takeaways:*
- Calibrate challenge to the user's current skill — too hard breeds frustration, too easy breeds boredom.
- Give clear, immediate feedback so users always know the effect of their actions and their progress.
- Cut friction: fast response times, minimal interruptions, and no unnecessary steps between intent and outcome.
- Make content and next actions discoverable so users don't stall out searching for what to do next.
- Design for a sense of control — predictable behavior, reversible actions, and stable interaction patterns.
- Support sustained focus by minimizing modal distractions, notifications, and context switches during core tasks.
*Caveats:*
- Flow is fragile — a single jarring interruption, latency spike, or unclear state can break immersion.
- Optimizing purely for engagement/flow can shade into dark-pattern territory (compulsive use, lost time awareness).
- Skill/challenge balance is user-specific: novices and experts need different difficulty curves, so one-size-fits-all UX often lands wrong for someone.
- Feedback that is noisy or excessive can be as disruptive as no feedback at all.
*Examples:*
- Well-tuned games that ramp difficulty as the player improves, keeping them in the challenge-skill sweet spot.
- Writing/design tools (Figma, Google Docs) with instant feedback, autosave, and minimal chrome so users stay heads-down in the work.
- Search and command palettes (Cmd-K) that collapse many possible actions into a single low-friction path.
- Onboarding flows that progressively disclose complexity as the user demonstrates competence, rather than dumping every feature at once.
- Video/reading apps that autoplay or preload the next item, reducing the friction that would otherwise pull the user out of the session.

#### Parkinson's Law
*Core:* Work stretches to fill the time you give it — so if a task has a loose deadline or a slow interface, users (and systems) will consume every last minute available rather than finish sooner.
*Origin:* Cyril Northcote Parkinson, 1955 essay in The Economist; expanded in the 1958 book "Parkinson's Law: The Pursuit of Progress."
*Takeaways:*
- Anchor completion times to what users already expect, then aim to beat that expectation.
- Finishing faster than the perceived duration reads as quality — under-promise, over-deliver on speed.
- Strip friction from repetitive input (autofill, saved details, smart defaults) so tasks don't expand to fill the form.
- Treat every extra field or step as time the task will bloat to consume.
- Design flows around the minimum time a task actually needs, not the time the user has allotted for it.
*Caveats:*
- Compressing deadlines without accounting for real task complexity backfires and hurts quality.
- Speed optimizations that skip confirmation or hide state can trade time saved for user anxiety or mistakes.
- Autofill and shortcuts must respect privacy and correctness — wrong prefilled data is worse than none.
*Examples:*
- Checkout autofill (address, card, contact) that collapses a multi-minute form into seconds.
- Booking flows that pre-select likely dates or travelers to prevent the task from ballooning.
- Progress indicators that show a task finishing ahead of the user's mental estimate.
- One-click reorder / resubscribe patterns that shortcut work users would otherwise stretch out.

#### Goal-Gradient Effect
*Core:* People push harder toward a goal the closer they perceive themselves to be to finishing it, so motivation accelerates as the finish line comes into view.
*Origin:* Clark Hull, 1932 (goal-gradient hypothesis); demonstrated in his 1934 rat-maze experiments.
*Takeaways:*
- Show progress explicitly so users can see how close they are to the finish line.
- Give users a head start (endowed progress) to make the goal feel closer and worth pursuing.
- Break long tasks into visible milestones so each step keeps the goal within reach.
- Increase reinforcement or feedback density as users near completion to amplify the acceleration effect.
- Design reward and loyalty structures around perceived proximity, not just absolute distance to the reward.
*Caveats:*
- Most rigorous evidence comes from animal studies; human application is still under-researched, so validate with your own users.
- Fake or misleading progress can feel manipulative and erode trust if the remaining work does not match the indicator.
- If early progress feels trivial or padded, users may disengage instead of accelerating.
*Examples:*
- Loyalty punch cards or rewards programs that pre-stamp the first few slots so the goal feels partially achieved.
- Multi-step onboarding or checkout flows with a progress bar and step counter.
- Profile-completion meters that nudge users to fill remaining fields as the bar approaches 100%.
- Fitness and habit apps that visualize streaks and daily goal proximity.

#### Zeigarnik Effect
*Core:* Unfinished or interrupted tasks stick in memory more strongly than tasks that have been completed, creating a lingering mental tension that pulls people back toward closing the loop.
*Origin:* Bluma Zeigarnik, Soviet psychologist, 1920s memory research.
*Takeaways:*
- Show progress indicators so users can see how close they are to completion — the visible gap keeps them pulling toward the finish.
- Seed a small amount of pre-filled or 'artificial' progress at the start of a task; a bar that already shows some movement is more motivating than an empty one.
- Use signifiers (peeking content, cut-off text, teaser cards) to signal there's more to uncover and create an itch to continue.
- Break long flows into visibly discrete steps so each unfinished step registers as an open loop worth closing.
- Lean on the effect to bring lapsed users back — reminders about half-finished carts, drafts, or onboarding steps tap directly into the tension of incompletion.
*Caveats:*
- Manufactured or dishonest progress (fake bars, endless 'almost done' states) can feel manipulative once users notice, eroding trust.
- Too many open loops at once creates anxiety and decision fatigue rather than motivation — the effect works best with a small number of salient unfinished tasks.
*Examples:*
- Multi-step onboarding with a progress bar that starts partially filled to make step one feel like momentum, not a cold start.
- Checkout or profile-completion flows that surface an explicit percentage-complete indicator.
- Article previews and 'read more' fades that reveal enough content to create curiosity about the rest.
- Re-engagement emails or notifications reminding users of an abandoned cart, unfinished draft, or half-completed course.

### Expectation & mental models

*Unifying principle:* Users arrive with pre-loaded models from every other product they use and won't read the manual; absorb complexity in the system, accept messy input generously, and match conventions so cognition goes to the task, not to the tool.

*Builder prompt:* What does this UI assume the user already knows or will bother to learn — and is that assumption honest?

Member laws:

#### Jakob's Law
*Core:* People spend the bulk of their browsing time on sites other than yours, so they arrive expecting your product to behave like the ones they already use. Meeting those inherited expectations lets them focus on the task instead of relearning the interface.
*Origin:* Coined by Jakob Nielsen (Nielsen Norman Group), a usability pioneer known for "discount usability engineering."
*Takeaways:*
- Lean on existing conventions — users will port mental models from familiar products onto yours whether you want them to or not.
- Design around established patterns so cognitive effort goes to the task, not to decoding the UI.
- When you must break a pattern, soften the jump: let users opt in, preview, or temporarily revert to the old version.
- Audit competitor and category-standard flows before inventing your own — deviation should be a deliberate choice, not an accident.
- Treat familiarity as a feature: matching platform idioms (iOS vs Android, ecommerce checkout norms) is often higher-leverage than novel visual design.
*Caveats:*
- Blind conformity can suppress genuinely better ideas — familiarity is a default, not a mandate.
- Mimicking a competitor's pattern that is itself broken propagates the flaw.
- Abrupt redesigns violate the law even when the new design is objectively better; migration paths matter as much as the endpoint.
- Cross-cultural or cross-platform assumptions can misfire — the 'familiar' pattern depends on which other sites your users actually use.
*Examples:*
- Digital form controls (toggles, radio buttons, checkboxes) that visually echo their physical-world equivalents so their behavior is self-evident.
- YouTube's 2017 Material Design rollout let users preview the new UI, toggle back to the old one, and submit feedback before the cutover.
- Ecommerce checkouts converging on cart icon top-right, multi-step checkout, and 'continue as guest' — deviating here tanks conversion.
- SaaS apps placing account/settings under an avatar in the top-right because that's where users have learned to look.

#### Mental Model
*Core:* A mental model is the compressed internal picture a person holds of how a system works, built from prior experience. Users approach new interfaces expecting them to behave like similar ones they already know, so friction shows up wherever the product's actual behavior diverges from that expectation.
*Origin:* Kenneth Craik, 1943 (The Nature of Explanation)
*Takeaways:*
- Design to match the model users already carry in from competing or adjacent products, not the one that lives in your team's head.
- Reuse conventional patterns (nav placement, checkout steps, form layouts) so users transfer knowledge instead of relearning.
- Close the designer-user model gap with real research: interviews, personas, usability tests, journey mapping.
- When you must break a convention, make the new behavior discoverable and self-explanatory on first contact.
- Treat mismatches between expectation and behavior as the primary source of usability friction, not aesthetics.
*Caveats:*
- Designers' mental models drift from users' because they know the internals; assuming parity is the default failure mode.
- Copying conventions blindly can entrench outdated patterns and block legitimately better interactions.
- Mental models vary by audience segment, region, and expertise level — one 'standard' rarely fits everyone.
*Examples:*
- E-commerce product cards, cart icons, and checkout flows follow a shared template so shoppers can operate a new store immediately.
- Settings gears, trash-can delete icons, and hamburger menus survive because they map to well-established user expectations.
- Migrations and redesigns that preserve familiar object names and flows retain users better than ground-up rethinks.

#### Paradox of the Active User
*Core:* People skip manuals and dive straight into using software, even though sitting down to learn the system first would save them time later. Designers must therefore build guidance into the flow instead of assuming users will study docs.
*Origin:* Mary Beth Rosson and John Carroll, 1987, in "Interfacing thought: cognitive aspects of human-computer interaction."
*Takeaways:*
- Assume users will not read the manual — design as if the product is the only teacher they get.
- Embed help inline (tooltips, empty-state hints, contextual coach marks) so guidance meets the user inside the task, not outside it.
- Optimize the first-run path so trial-and-error still leads somewhere useful; failed exploration is the default mode.
- Reserve deep documentation for advanced or edge-case flows, not the core happy path.
- Surface next-step affordances at the moment of confusion instead of forcing users to leave and search.
*Caveats:*
- Assuming any user segment will 'just read the docs first' — even power users tend to jump in.
- Over-stuffing the UI with tooltips and modals can create noise that users learn to dismiss on sight.
- Contextual help that only fires once can leave repeat-confused users stranded; consider persistent access.
- Fixing the paradox by adding onboarding overlays doesn't excuse a confusing underlying interface.
*Examples:*
- A design tool showing an inline tip on a canvas element the first time a user hovers it, rather than linking to a help center article.
- Empty-state screens that demonstrate what the feature does and provide a one-click sample action.
- Form fields with example placeholder text and format hints, so users don't need to consult docs for expected input.
- Progressive disclosure of advanced settings — beginners see a simple flow, advanced controls appear once basic use is established.

#### Postel's Law
*Core:* Also called the Robustness Principle: accept a wide range of user input generously, but respond back with output that is precise, predictable, and standards-conformant. In UX terms, interfaces should absorb human messiness while behaving reliably in what they emit.
*Origin:* Jon Postel, 1980 — originally a TCP/IP design guideline ("be conservative in what you do, be liberal in what you accept from others").
*Takeaways:*
- Design with empathy for the full range of ways users will actually type, tap, and interact — not just the happy path you specified.
- Assume variance in inputs, devices, and capabilities; build interfaces that stay stable across all of them.
- Translate messy input into valid state internally (trim, normalize, infer) rather than rejecting it outright.
- Set clear boundaries on what input is acceptable and communicate them upfront, before the user commits.
- When input is ambiguous or invalid, give specific, actionable feedback instead of a generic error.
- Emit output that is strict and consistent, even while you accept generously — reliability compounds trust.
*Caveats:*
- Being too liberal in what you accept can hide bugs and let malformed data propagate downstream.
- Silent normalization of input can surprise users if the system 'guesses' wrong about intent.
- Over-flexible parsing invites security issues and spec drift when downstream systems disagree on meaning.
- The page itself does not enumerate misuse patterns in depth — treat it as guidance, not license to skip validation.
*Examples:*
- Form fields that accept phone numbers with or without dashes, spaces, or parentheses and normalize on submit.
- Search inputs that tolerate typos, casing, and extra whitespace but return one canonical result set.
- Date pickers that accept typed strings ('tmrw', '7/21', 'July 21') and resolve them to a single ISO value.
- Payment forms that auto-format credit card numbers as the user types instead of rejecting formatting.
- APIs that accept multiple input shapes but always respond with one strict, versioned schema.

#### Tesler's Law
*Core:* Every system contains an irreducible minimum of complexity — you can shift it around, but not eliminate it. Whatever complexity isn't absorbed by the product's design and engineering ends up dumped on the user.
*Origin:* Larry Tesler at Xerox PARC, mid-1980s. Also known as the Law of Conservation of Complexity.
*Takeaways:*
- Assume complexity is fixed — your only real choice is who eats it, the team or the user, and the team should eat as much as possible.
- Spend extra engineering and design time up front so millions of downstream users don't pay a small friction tax each.
- Design for real, irrational, distracted humans rather than an idealized power user who reads every label.
- When complexity truly can't be hidden, surface guidance inline at the moment of use instead of shunting people to docs.
- Don't chase infinite simplification — expect that simpler tools invite users to attempt harder tasks (Tognazzini's counterpoint).
*Caveats:*
- Over-simplifying the UI can just relocate complexity into hidden state, magic behavior, or brittle assumptions that break later.
- Bruce Tognazzini's counterpoint: as an app gets simpler, users attempt more ambitious tasks, so perceived complexity may not actually drop.
- Absorbing complexity on the engineering side can balloon internal system complexity and maintenance cost if done carelessly.
*Examples:*
- Email clients auto-filling the sender, date, and threading so the user only writes the message body.
- Address forms that infer city/state from a ZIP code instead of asking the user to type them.
- Signup flows with smart defaults and progressive disclosure — advanced options exist but aren't shown until needed.
- Inline hints, tooltips, and empty-state guidance that teach a new feature at the exact point of use.

### Emotion, memory & polish

*Unifying principle:* Users remember experiences by their peaks, endings, and how they looked — not by averaging every moment; visual craft and a strong finish buy tolerance and imprint the whole product.

*Builder prompt:* What single moment and what final screen will the user actually carry away from this flow?

Member laws:

#### Peak-End Rule
*Core:* People remember an experience mostly by its most intense moment (the peak) and how it ended, not by averaging every moment along the way. The overall impression is anchored to those two snapshots rather than to duration or cumulative quality.
*Origin:* Kahneman, Fredrickson, Schreiber, and Redelmeier, 1993 — "When More Pain Is Preferred to Less: Adding a Better End."
*Takeaways:*
- Invest disproportionate design effort in the emotional highs and the final moment of a flow — they define the memory of the whole thing.
- Engineer at least one deliberate positive peak (delightful, useful, or funny) so users have something vivid to recall.
- End experiences on an upswing; a strong finish can rehabilitate a mediocre middle.
- Weight negative peaks more heavily than positive ones when prioritizing fixes — bad moments imprint harder.
- Audit journeys for accidental negative peaks (errors, waits, dead ends) and either remove them or reframe the perception around them.
*Caveats:*
- Optimizing only averages or aggregate metrics misses what users will actually remember.
- A single sharp negative peak can dominate recall even if the rest of the flow is smooth.
- Manufactured 'delight' moments feel hollow if they aren't tied to something genuinely helpful or valuable.
*Examples:*
- Mailchimp uses illustration, animation, and playful humor at confirmation moments to plant a positive peak.
- Uber reduced cancellations by reshaping how riders perceive wait time, softening a would-be negative peak.
- Ending a checkout or onboarding flow with a warm confirmation screen rather than a bare success state.
- Adding a small celebratory moment after a task-completion milestone so the memory of the tool skews positive.

#### Aesthetic-Usability Effect
*Core:* People tend to judge good-looking interfaces as easier to use, even when the underlying usability is unchanged. Visual polish shifts perception of function, not the function itself.
*Origin:* Masaaki Kurosu and Kaori Kashimura (Hitachi Design Center), 1995 — ATM interface study with 252 participants across 26 layout variants.
*Takeaways:*
- Invest in visual craft early — attractive UIs earn goodwill and forgiveness for small friction.
- Aesthetics buys tolerance, not correctness: users still hit the same bugs, they just complain less.
- During usability testing, actively probe past the polish — beautiful prototypes hide real problems.
- Pair visual reviews with task-based testing so perception scores don't drown out behavioral failure signals.
- Treat 'feels usable' and 'is usable' as two separate metrics; track both.
*Caveats:*
- Pretty designs can mask genuine usability defects and delay fixes.
- User-testing feedback on polished mockups skews positive, producing false confidence.
- Teams may over-invest in surface style while ignoring structural interaction problems.
*Examples:*
- Apple product UIs where high visual polish makes minor interaction quirks feel acceptable.
- Landing pages with strong typography and imagery converting better despite similar copy and flow.
- Well-designed error states and empty states softening frustration when something goes wrong.
- Redesigns that score higher on satisfaction surveys even when task-completion times are identical.

## Agent-UI relevance subset

Laws with the highest leverage specifically for CLI-agent frontends (wiki-app, code-review UIs, agent orchestrator dashboards).

- **Doherty Threshold** — Agent tool calls, LSP queries, and code reviews often take seconds or minutes; without sub-400ms acknowledgment (streaming tokens, tool-start echoes, skeleton output blocks) the CLI feels dead and operators start hammering Ctrl-C. Perceived performance IS the UX for agent UIs.
- **Selective Attention** — In a Wiki.app run stream or CLI log, users scan for the one line that matters (a failure, a merge-ready signal, a diff). Anything that looks like decorative log noise gets tuned out — critical events need Von-Restorff-style visual breaks (color, indent, badges) or they vanish in scrollback.
- **Peak-End Rule** — An agent run is remembered by its worst spike (a hallucinated edit, a broken PR) and its final message. A clean, well-summarized end screen — merge-ready confirmation, PR link, files touched — reshapes the memory of an otherwise messy trajectory.
- **Working Memory** — Operators supervising multi-agent fleets can't hold 12 tmux windows in their head. Dashboards must surface persistent state (which worker is on which ticket, last activity, PR status) so users recognize rather than recall — a review UI without sticky context forces re-derivation every glance.
- **Mental Model** — Agent UIs are new territory; users port models from chat apps, IDEs, and CI dashboards. If a 'run' behaves like a chat thread in one place and like a job in another, the mismatch burns trust. Pick one metaphor per surface and honor it.
- **Postel's Law** — CLIs and agent prompts must accept messy human input (typos in ticket IDs, informal steer messages, ambiguous slash commands) and normalize internally, while emitting strict machine-readable artifacts (JSON tool calls, structured PR bodies). This is the core contract of an agent runtime.
- **Tesler's Law** — The complexity of agent orchestration (worktrees, tmux windows, merge gates, review loops) is real and irreducible. Skills and orchestrator prompts should absorb it — one command to spawn, monitor, steer — so operators don't manually juggle git, tmux, and Linear at once.
- **Zeigarnik Effect** — Long-running agent runs create open loops (PRs awaiting review, tickets in progress, merge-ready signals). Surfacing an explicit 'in-flight work' inbox / todo view leverages the tension of incompletion to bring operators back before things stall.
- **Aesthetic-Usability Effect** — Agent dashboards that look raw (unstyled logs, mono terminal walls) are perceived as flaky even when correct. A polished code-review UI with good typography and diff hierarchy makes operators trust the underlying agent more and forgive minor tool errors.
- **Chunking** — Agent transcripts, tool-call streams, and multi-file diffs are wall-of-text by default. Grouping into collapsible sections (per-tool-call, per-file, per-worker) turns unparseable logs into scannable structure — the difference between a usable and useless review UI.

## Common anti-patterns (violations)

- Spinner-only loading state with no skeleton, progress, or streaming output — violates doherty-threshold and flow: user has no signal the system is alive under 400ms, breaks focus.
- Kitchen-sink settings screen exposing every toggle at once — violates hicks-law, choice-overload, cognitive-load, and occams-razor: users freeze, scan slower, and can't find the vital few controls.
- Silent success (no confirmation, no summary) at end of a long agent run — violates peak-end-rule: the memory of the whole run defaults to the last log noise instead of a positive close.
- Uniform log stream where errors, tool calls, and user messages all look identical — violates selective-attention and von-restorff-effect: the important line disappears into the pattern.
- Redesigning a familiar checkout / nav with a novel metaphor and no opt-out — violates jakobs-law and mental-model: users lose transferred knowledge and blame the product, not the change.
- Multi-step form that forgets prior fields on back navigation — violates working-memory and postels-law: forces recall of values the system had already captured.
- Boxing every single element on the page in its own card — violates law-of-common-region and law-of-uniform-connectedness: over-connection destroys hierarchy and creates competing regions.
- Tight, uniform spacing between unrelated items with the same distance as related items — violates law-of-proximity: users infer relationships that don't exist.
- Styling non-interactive text like links (or vice versa) — violates law-of-similarity: users mis-target and lose trust in what's clickable.
- Forcing users to read a docs page before the product is usable — violates paradox-of-the-active-user and teslers-law: dumps irreducible complexity onto the user instead of embedding guidance inline.
- Progress bar that jumps to 90% instantly then stalls forever — violates goal-gradient-effect, zeigarnik-effect, and peak-end-rule: fake proximity followed by a bad ending erodes trust more than showing honest progress.
- Tiny close buttons and dense adjacent icon rows on mobile — violates fittss-law: raises error rate and forces careful aiming that breaks flow.

## Provenance

Workflow `laws-of-ux-mining` 2026-07-21: 30 parallel WebFetch agents (one per law page) + 1 synthesis. Single-pass, all content paraphrased. Verify against lawsofux.com or the source citations before quoting in external artifacts.

Related: [[harness]], [[harness-best-of-breed]], [[wiki-app-ui-direction]], [[ui-demo]].
