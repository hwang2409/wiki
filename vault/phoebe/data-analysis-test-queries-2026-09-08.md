---
type: reference
tags: [phoebe, data-analysis, audit]
created: 2026-09-08
updated: 2026-09-08
---

# Production audit: realistic v3 data-analysis queries

## Findings

### Scope and method

I reviewed live General-agent user messages from the production analytics read
replica. The bounded window covered 2026-04-30 through 2026-09-08. It contained
16,506 user messages from 203 customer organizations.

The filter excluded Phoebe's internal organizations, admin-probe fixtures,
hidden chats, admin probe runs, and internal read-only v3 runs. I used
high-signal keyword groups, then read samples from different organizations and
months. The counts below are keyword hits, not exclusive classifications. They
can overlap and should be treated as rough frequencies.

### Recurring ask categories

1. **Counts and workload status: about 184 messages across 74 organizations.**

   Common forms were open-shift counts, total hours, and outreach volume.

   - "How many open unfilled shifts do we have over the next seven days?"
     Evidence: run `019faf13-f3fb-74c7-80ba-d3aca1a97cdc`; conversation
     `50ce05af-e3bd-451e-8648-439ced7f6715`.
   - "How many weekly completed hours did we have over the past 30 completed
     days?" Evidence: run `01a0445a-1369-7705-9caf-ebdd56fa727e`;
     conversation `92b06a1f-1077-4c12-adfc-ebc92f33cca2`.
   - "Break down how many outreaches Phoebe sent in the same 30-day period."
     Evidence: run `019ff25d-3e55-75e2-8a02-a32470c53722`; conversation
     `33af19c2-f127-4aae-a69a-0183d6de995f`.

2. **Reports, exports, and charts: about 174 messages across 69 organizations.**

   Users often start with a report or CSV. Some later ask for a chart or PDF.

   - "Give me a report of which caregivers said yes to shifts in the past 30
     days. Include shifts offered and shifts picked up." Evidence: run
     `01a06a28-de87-7e66-b12e-bbf9b09e23fd`; conversation
     `c0dd2517-170c-49b9-afb4-58761361c936`.
   - "Make a comprehensive clock-reminder report for this week, then compile
     it into a PDF." Evidence: run `01a048f1-f3f6-7543-a04d-4926c95334a2`;
     conversation `906873d2-750c-41da-bb55-b98bf9698b15`.
   - "Create a CSV of the caregiver responses." Evidence: run
     `019f5cfb-c2fb-7e61-9aeb-2efcbbf7c296`; conversation
     `f3d6c8de-7eba-4849-b104-5462808902b1`.

3. **Breakdowns, ratios, and rankings: about 59 messages across 33 organizations.**

   Users ask for per-client or per-caregiver cuts after seeing an initial total.

   - "Break it down day by day and client by client." Evidence: run
     `019ff6df-5b3e-75a9-898a-58209e26923d`; conversation
     `d17369ef-ed83-42df-8365-094da8d2d132`.
   - "Sort the last report by clients with the fewest hours each week."
     Evidence: run `01a01c25-e06f-701e-ba64-15fececf3303`; conversation
     `cb4963f2-7de9-415d-a943-f84f16374031`.
   - "Which caregivers had the most missed clock-ins and clock-outs last
     month?" Evidence: run `019fb4f1-5d57-7739-b17d-7a623fdd4ba7`;
     conversation `a5302658-5822-4136-ad2d-db55844ef90c`.

4. **Data-quality and reconciliation checks: about 39 messages across 23 organizations.**

   The common shape is a mismatch between Phoebe, the EHR, and expected hours.

   - "These shifts are in our EHR but not showing in Phoebe. Which sync is
     stale?" Evidence: run `019febfb-f9bf-7e36-b004-62d5e6cfb98b`;
     conversation `0b68bfc0-71bb-4480-896f-f0aec9212a8b`.
   - "List clients who are scheduled above their authorization hours and show
     by how much." Evidence: run `01a03f74-0e3e-7ac0-a4b5-e2e908be3c55`;
     conversation `1f6ae385-b53f-4b09-a14a-8e1237c23e3e`.

5. **Caregiver response and engagement: about 28 messages across 24 organizations.**

   Users distinguish replies, acceptances, declines, and no response.

   - "For the last seven days, show offers, acceptances, declines, and no
     responses by caregiver." Evidence: run
     `019f5cfb-c2fb-7e61-9aeb-2efcbbf7c296`; conversation
     `f3d6c8de-7eba-4849-b104-5462808902b1`.
   - "Which caregivers have never responded to Phoebe in the last 30 days?"
     Evidence: run `01a039bd-ff14-7cb8-9747-49354590013e`; conversation
     `07450412-687c-4ee3-87e0-a71355e529a5`.
   - "Show the weekly outreach response rate week over week." Evidence: run
     `019ff6d5-c734-70c1-8296-6fba897716ed`; conversation
     `c16c4c24-d0a2-493b-b63a-228817a03e0d`.

6. **Explicit time trends and period comparisons: about 6 messages across 6 organizations.**

   Direct trend wording was rare. Time-bounded reports were much more common.

   - "Show the weekly outreach response rate week over week." Evidence: run
     `019ff6d5-c734-70c1-8296-6fba897716ed`; conversation
     `c16c4c24-d0a2-493b-b63a-228817a03e0d`.
   - "Compare fill rates before and after our call settings changed, include
     average time to fill, and show filled versus unfilled trends." Evidence:
     run `019f67b2-e19d-7099-b94c-4f4b33a528a6`; conversation
     `566b7d95-0134-4479-8b24-46b9fabb85df`.

7. **Explicit fill-performance asks: about 4 messages across 4 organizations.**

   The phrases were short, but follow-up context asked for richer reports.

   - "What is our outreach shift fill rate?" Evidence: run
     `019ff6d5-c734-70c1-8296-6fba897716ed`; conversation
     `c16c4c24-d0a2-493b-b63a-228817a03e0d`.
   - "Show fill rate, shifts filled, and outreach outcomes." Evidence: run
     `019fd34c-43d1-70db-b396-988f628aa76d`; conversation
     `a2ab0314-440a-4da7-8568-05ec1ab5f392`.
   - "Create a shift-fill performance report with fill count, average fill
     time, and filled versus unfilled trends." Evidence: run
     `019f67b2-e19d-7099-b94c-4f4b33a528a6`; conversation
     `566b7d95-0134-4479-8b24-46b9fabb85df`.

### Coverage gaps

I found no verified customer ask for cohort or retention analysis. The only
keyword hit was unrelated contract text. I marked the cohort test below as
`synthesized`. I also marked the histogram test as `synthesized`. It extends
observed fill-time asks into the skill's distribution output.

## Recommended test queries

`Observed` means the query is a sanitized version of one conversation or a
follow-up sequence in that conversation. `Synthesized` means it extends an
observed pattern to cover a skill capability that did not appear directly.

1. **"How many open, unfilled shifts do we have over the next seven days?"**
   - Grounding: observed
   - Category: count and workload status
   - Expected output level: quick answer
   - Query-tool entities: `shifts`
   - Basis: run `019faf13-f3fb-74c7-80ba-d3aca1a97cdc`; conversation
     `50ce05af-e3bd-451e-8648-439ced7f6715`

2. **"How many completed hours did we deliver each week over the last 30 completed days? Show the weekly totals and note any partial week."**
   - Grounding: observed
   - Category: time trend and workload
   - Expected output level: full analysis
   - Query-tool entities: `shifts`
   - Basis: run `01a0445a-1369-7705-9caf-ebdd56fa727e`; conversation
     `92b06a1f-1077-4c12-adfc-ebc92f33cca2`

3. **"Break down our open shifts for the next four weeks day by day and client by client. Include shift count and open hours."**
   - Grounding: observed
   - Category: grouped breakdown
   - Expected output level: full analysis
   - Query-tool entities: `shifts`, `clients`
   - Basis: run `019ff6df-5b3e-75a9-898a-58209e26923d`; conversation
     `d17369ef-ed83-42df-8365-094da8d2d132`

4. **"For last week, show each client's visit count, unique caregivers, and visits per caregiver. Exclude office placeholder clients and add a sorted bar chart with the most spread-out clients first."**
   - Grounding: observed
   - Category: continuity ratio, ranking, and chart
   - Expected output level: full analysis
   - Query-tool entities: `shifts`, `clients`, `caregivers`
   - Basis: run `01a0212f-39a7-74a9-a7f6-8bd0dc5cf431`; conversation
     `5ae98704-a65d-4b78-98a8-8f32c1eff527`

5. **"Which caregivers had the most missed clock-ins and clock-outs last calendar month? Show counts for each caregiver and separate clock-in from clock-out misses."**
   - Grounding: observed
   - Category: ranking and clock compliance
   - Expected output level: full analysis
   - Query-tool entities: `shifts`, `shift_events`, `caregivers`
   - Basis: run `019fb4f1-5d57-7739-b17d-7a623fdd4ba7`; conversation
     `a5302658-5822-4136-ad2d-db55844ef90c`

6. **"Make a comprehensive report of this week's clock reminders. Show who needed reminders, how many attempts each person received, and whether they clocked in and out on time. Compile the final report into a PDF."**
   - Grounding: observed
   - Category: clock compliance and formal reporting
   - Expected output level: formal report
   - Query-tool entities: `outreaches`, `contact_attempts`, `shifts`,
     `shift_events`, `caregivers`
   - Basis: run `01a048f1-f3f6-7543-a04d-4926c95334a2`; conversation
     `906873d2-750c-41da-bb55-b98bf9698b15`

7. **"Create a report for the last seven days showing how many caregivers received shift offers and, by caregiver, how many offers they accepted, declined, or did not answer."**
   - Grounding: observed
   - Category: caregiver response outcomes
   - Expected output level: full analysis
   - Query-tool entities: `outreaches`, `outreach_shifts`,
     `contact_attempts`, `contact_attempt_shift_responses`, `caregivers`
   - Basis: run `019f5cfb-c2fb-7e61-9aeb-2efcbbf7c296`; conversation
     `f3d6c8de-7eba-4849-b104-5462808902b1`

8. **"For the past 30 days, show which caregivers expressed interest in a Phoebe outreach. Include shifts offered, shifts they said yes to, and shifts they later picked up."**
   - Grounding: observed
   - Category: interest-to-fill comparison
   - Expected output level: full analysis
   - Query-tool entities: `outreaches`, `outreach_shifts`,
     `contact_attempts`, `contact_attempt_shift_responses`, `shifts`,
     `caregivers`
   - Basis: run `01a06a28-de87-7e66-b12e-bbf9b09e23fd`; conversation
     `c0dd2517-170c-49b9-afb4-58761361c936`

9. **"Pull our weekly outreach response rate for the last eight complete weeks. Show reply rate and acceptance rate separately, and add a week-over-week line chart."**
   - Grounding: observed
   - Category: response trend and chart
   - Expected output level: full analysis
   - Query-tool entities: `outreaches`, `contact_attempts`,
     `contact_attempt_shift_responses`
   - Basis: run `019ff6d5-c734-70c1-8296-6fba897716ed`; conversation
     `c16c4c24-d0a2-493b-b63a-228817a03e0d`

10. **"What was our outreach shift fill rate over the last 30 complete days? Tell me the numerator, denominator, and any excluded outreach types."**
    - Grounding: observed
    - Category: fill performance
    - Expected output level: quick answer
    - Query-tool entities: `outreaches`, `outreach_shifts`, `shifts`
    - Basis: run `019ff6d5-c734-70c1-8296-6fba897716ed`; conversation
      `c16c4c24-d0a2-493b-b63a-228817a03e0d`

11. **"Create a shift-fill performance report for the last 30 days. Include shifts filled through Phoebe, fill rate before and after our call settings changed, average time to fill, and filled versus unfilled trends."**
    - Grounding: observed
    - Category: period comparison and fill performance
    - Expected output level: full analysis
    - Query-tool entities: `outreaches`, `outreach_shifts`, `shifts`,
      `contact_attempts`, `callout_settings`
    - Basis: run `019f67b2-e19d-7099-b94c-4f4b33a528a6`; conversation
      `566b7d95-0134-4479-8b24-46b9fabb85df`
    - Boundary to test: the agent should ask for the settings-change date if
      the available data does not supply it.

12. **"Create a report of all shifts offered through outreach in the last 30 days. Only include shifts with at least one contact attempt. Include the outreach end state and the number of available caregivers for each shift."**
    - Grounding: observed
    - Category: outreach performance breakdown
    - Expected output level: full analysis
    - Query-tool entities: `outreaches`, `outreach_shifts`,
      `contact_attempts`, `contact_attempt_shift_responses`, `shifts`
    - Basis: run `019f2115-960d-724c-896d-619e970d01f8`; conversation
      `df8d1cb1-d7d1-4111-8516-5b16fdd4e4a7`

13. **"Sort clients by their scheduled hours per week, with the clients who have the fewest hours first. Use the last four complete weeks and show each week's hours."**
    - Grounding: observed
    - Category: client comparison and ranking
    - Expected output level: full analysis
    - Query-tool entities: `shifts`, `clients`
    - Basis: run `01a01c25-e06f-701e-ba64-15fececf3303`; conversation
      `cb4963f2-7de9-415d-a943-f84f16374031`

14. **"Two shifts appear in our EHR but not in Phoebe. Check recent shift sync failures and schedule-window coverage, then tell me whether Phoebe's data is stale or the shifts are marked missing."**
    - Grounding: observed
    - Category: data-quality reconciliation
    - Expected output level: quick answer
    - Query-tool entities: `shifts`, `shift_sync_coverage`, `sync_runs`
    - Basis: run `019febfb-f9bf-7e36-b004-62d5e6cfb98b`; conversation
      `0b68bfc0-71bb-4480-896f-f0aec9212a8b`

15. **"List our VA clients who are scheduled above their authorization hours this month. Show authorized hours, scheduled hours, and the overage."**
    - Grounding: observed
    - Category: data-quality boundary check
    - Expected output level: quick answer
    - Query-tool entities: `clients`, `shifts`; no authorization entity is
      currently exposed
    - Basis: run `01a03f74-0e3e-7ac0-a4b5-e2e908be3c55`; conversation
      `1f6ae385-b53f-4b09-a14a-8e1237c23e3e`
    - Boundary to test: the agent should state the missing data surface and
      avoid inventing authorization totals.

16. **"For shift-filling outreaches completed in the last 90 days, show the distribution of hours from outreach start to fill. Break it out by office and add a histogram plus median and 90th percentile."**
    - Grounding: synthesized from observed average-time-to-fill and office
      breakdown asks
    - Category: distribution and office comparison
    - Expected output level: full analysis
    - Query-tool entities: `outreaches`, `outreach_shifts`, `shifts`
    - Basis patterns: run `019f67b2-e19d-7099-b94c-4f4b33a528a6` and run
      `019ff6df-5b3e-75a9-898a-58209e26923d`

17. **"For the last three complete months, compare outreach reply and acceptance rates for first-time contacted caregivers versus caregivers contacted in an earlier month. Show cohort sizes and a monthly comparison chart."**
    - Grounding: synthesized; no verified cohort ask appeared in the audit
    - Category: cohort and retention-style analysis
    - Expected output level: full analysis
    - Query-tool entities: `caregivers`, `contact_attempts`,
      `contact_attempt_shift_responses`, `outreaches`
    - Basis patterns: observed response-rate and caregiver-breakdown asks

18. **"Review the caregiver replies to our holiday-weekend availability message. Group available caregivers by area and available day, note anyone with no clear answer, and create a PDF our team can use as a call-first list."**
    - Grounding: observed
    - Category: response analysis and formal reporting
    - Expected output level: formal report
    - Query-tool entities: `caregivers`, `sms_conversations`, `sms_messages`,
      `availability`
    - Basis: run `01a063f1-1776-7255-84e6-f150ea959adb`; conversation
      `0d47211b-7902-4cd5-89ed-4b2e13c25709`

## Audit notes

- The strongest user pattern is iterative. A coordinator asks for one total,
  then requests a breakdown, sort, chart, or downloadable file.
- Users often leave metric definitions implicit. Response rate needs separate
  reply and acceptance definitions. Fill rate needs an explicit eligible-shift
  denominator.
- Several asks test missing data. The skill should identify absent fields or
  entities instead of creating a plausible value.
- PDF demand is real. Two reviewed conversations asked for PDFs after the
  underlying analysis was complete.
