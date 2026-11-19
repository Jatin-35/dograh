# BotrixAI Changelog

Our own changes on top of upstream Dograh, newest first. Upstream's release
notes live in `CHANGELOG.md`; we never edit that file, so upstream merges stay
conflict-free.

**How to read an entry**
- `abc1234`: the commit, on branch `botrix-main`.
- **Live**: deployed to the prod server. **Not deployed**: committed, not yet on prod.
- ⚠️ marks what has not been checked on a real call or against the real provider.

**Where prod is now:** everything up to `e7898766` (19 Nov 2026) is live, on
Dograh v1.46.0 with pipecat 1.7. The Vectus lookup service (`vectus-api`) is a separate container
with its own deploys.

**Adding an entry:** add a line under today's date with every commit, and mark
it **Live** after the deploy.

---

## 2026-11-19: Upgraded to Dograh v1.46 (Checkpoint B), live
- `6e7ffdd4` Merged upstream **v1.45** (pipecat 1.5 → 1.7). `58666050` merged
  **v1.46**. `e7898766` build fixes (OpenCV 4.14 pin, five new pins, Linux
  lockfile).
- **New from upstream:**
  - **Call dispositions:** business outcomes per agent, mapping to your own codes.
  - **Telephony:** SIP trunks, setup checklist, and inactive configurations.
  - **Pre-call fetch** can be limited to inbound or outbound calls.
  - **Tools:** URL path parameters and nested JSON bodies.
  - **Noveum** tracing node.
  - **Termination funnel**, so call endings are recorded in order.
  - **Gemini 3.x** no longer gets a "nudge" after a tool result, which used to
    make the bot cut itself off.
- **Kept our way:**
  - Preset tool values beat the AI's values, so the caller's real number wins.
  - Dograh-managed SIP stays **off** unless `MANAGED_SIP_PROVISIONING_ENABLED=true`.
  - Campaign failures are counted once per call, never from busy or no-answer.
  - A rejected provider key still ends the call.
  - Gemini Live keeps the bot's words beside a tool call when reseeding.
  - langfuse stays on 3.9.3.
- **Bugs the merge would have shipped, fixed before deploy:**
  - Soniox couldn't connect.
  - Every call crashed when ending.
  - The API wouldn't start (VoiceLink/TATA spec).
  - Custom Tools got switched off on saving preferences.
  - VoiceLink/TATA calls were missing from call-history filters.
  - Agent settings validation broke.
- **Data migrations:** Gemini 2.5 → 3.5 Flash (changed 0 rows; none in use),
  telephony inactive flag, trunks, pre-call fetch mode. DB backup taken first:
  `~/backups/pre-v1.46-20261119-1617.dump` on the VM.
- Live-tested: Gemini 3.1 with a tool, Gemini 3.8, VoiceLink in/out, Soniox.
  ⚠️ Not tested: a campaign on a rejected key.

## 2026-11-19, live (api + ui)
- `4a45f0f5` **"Hire an Expert" renamed "Build My AI Agent"** in the sidebar
  button, the agent-builder nudge ("Our team builds it for you") and the
  billing message.
- `4a45f0f5` **Lead email subject** is now `BotrixAI Voicebot Lead – Build My AI Agent: <company>`
  (or `– Enterprise: …`). It was `[BotrixAI lead] …`.
- **Ops (no code), live:** SMTP set up on prod with Zoho (`smtp.zoho.in`, 465, SSL,
  from contact@botrixai.com). Lead emails go to contact@ and jatin@botrixai.com
  (`LEADS_EMAIL`), and Webhook Sync alert emails now go out too. Tested with a
  real form submission.
- **Ops (no code):** checked Langfuse's v4 migration. Nothing left to do; the
  "Action needed" banner clears by itself after 20 Nov.
- **Shelved, not committed:** the live-transcript spacing fix ("Prag ati" → "Pragati")
  for realtime agents. The patch is in `Desktop\dograh_shelved_2026-11-19`.

## 2026-11-18: Upgraded to Dograh v1.44 (Checkpoint A), live
- `e89207d7` Merged upstream **v1.43**. New run-creation flow; our Gemini Live
  fixes kept.
- `bc6ed9ad` Merged upstream **v1.44**. Added the **Atlas Cloud** (LLM) and
  **LMNT** (TTS) providers; moved the call-rate limiter; VoiceLink and TATA
  updated for the new phone-number check.
- `cdc06738` Kept TURN on by default (upstream's default would have switched it off).
- Live-tested: VoiceLink inbound and outbound, editor test call, realtime agent.
- ⚠️ Not tested: campaigns / Webhook Sync, website widget.
- Rollback images are tagged `dograh-local/dograh-{api,ui}:rollback` on the VM.

## 2026-11-16: Langfuse v4 and lead forms, live
- `bf17646c` **Langfuse v4:** v4 ingestion header; trace links include the
  project (the project id is found automatically); traces are **private by
  default**, with a per-org "Public trace links" switch. 628 old links were
  rewritten.
- `99802462` Call details (trace name, session and so on) now show on every
  step of a Langfuse trace.
- `e791adb6` **Hire an Expert / Enterprise forms** now email the team instead of
  going to Dograh. Toasts are accurate, and the phone field defaults to the
  visitor's country.

## 2026-11-11 to 15
- `04bdd2dc` `7b028bad` `da98805c` **Boson Higgs Realtime** speech-to-speech provider.
  ⚠️ It never called tools with real-sized prompts, so it can't be used for
  multi-node agents.
- `eeb36541` Embed: the "Enable Embedding" switch shows and saves correctly.
- `a33548af` **BotrixAI chat widget**: voice and chat on websites. Old Dograh
  embed codes still work. Follow-ups: `4df5d865` (widget calls count as inbound;
  visitors can't set run ids), `6a9daaf7`, `9003b701`.
- `484e5524` Live calls no longer fall back to the editor's sample values.
- `e7499ba1` TURN stays on when only `TURN_SECRET` is set.
- `de4c5c32` **Vartaloop** (renamed from Vartalaap): Indian English voices; the
  speed setting is marked as having no effect.

## 2026-10-29 to 11-07
- `5f56ce11` **60db** real-time transcriber for phone calls. ⚠️ No Hindi/Hinglish
  phone call yet.
- `8daac975` **Vartalaap** streaming voice (Hinglish and English).
- `7ce6ab51` Custom Tools: a refused deploy writes nothing; archived tools don't
  block a name.

## 2026-10-22 to 23: Inworld Realtime and Sarvam
- `a20f55bc` **Inworld Realtime** speech-to-speech provider, plus `508b8b9d`
  (key check), `64f14726` (holds replies; expected words, eagerness),
  `11ee378d` (low-latency, TTS-2 Flash, Soniox STT), `8770439c` (settings
  sections), `ee1d3a71` (our own turn-taking), `faeab406` and `22ddf438` (its
  own tab; pick the transcriber and voice separately), `86ab6857` (masked keys
  saved correctly). ⚠️ No real call recorded yet.
- `60e59f8c` Sarvam transcript style (transcribe / codemix / translit / verbatim).
- `b50fd512` Sarvam **bulbul:v4-flash**; deprecated bulbul:v2 moved to v3.
- `53173626` Custom Tools: env-var buttons stay inside the dialog.
- `c660c10e` `c8ae3487` vectus-api: **/products** lookup (bath-ware, household,
  garden), district fallback, Area Manager per product.

## 2026-10-16 to 21
- `27910ac1` **Soniox** transcriber (stt-rt-v5) with per-agent controls. Checked
  against the real API. ⚠️ No full test call yet.
- `cbbb744d` **Campaigns pause immediately** when a provider rejects the account
  (bad key, no credit). ⚠️ No live wrong-key test yet.
- `569cbdd2` **Inception (Mercury)** LLM. Live-tested: fast, but once invented a
  number, so not for Vectus.
- `196d0b5a` `825668ef` `0e5b7c9a` `d4bcf02e` **Ambient sound**: plays only while
  the caller waits, stops the instant the bot speaks; settings redesigned.
- `e45b3156` **Anthropic (Claude)** LLM provider. ⚠️ Not tested on the real API.
- `13738bce` **Turn-taking upgrade**: Silero v6.2 VAD, interrupts need 2 words,
  turn-end fixes, recovery from empty turns.
- `0af54078` Per-agent **Background Noise Reduction** (RNNoise), off by default.
- `b0d46a7f` **Murf** (falcon-2) voice. ⚠️ Hindi voices and latency untested.
- `98ddf567` OpenAI-compatible routers that refuse the model list are accepted.

## 2026-10-09 to 14
- `9662ed56` Gemini 3.8: short backchannels ("haan", "ok") no longer cut the bot off.
- `e4a5f7d0` Per-agent "Allow Interruption During Greeting".
- `12889f59` Editable base URL for Hopper.
- `fa9714cd` Gemini 3.8: per-agent turn-end silence.
- `ae853e60` `759df310` `5af1a611` `eb7a3cd4` vectus-api: **tank lookup** from
  Vectus's tank sheet, tuned on real calls.
- `636358bc` `bd9cadbf` vectus-api: area managers from the live Mavis table.
- `92ea7a40` `a0659655` Webhook Sync translates CRM values before storing and calling.
- `f898961c` Gemini Live: tool history sent as text on reseed.
- `df69463a` Extraction reads a JSON reply that is missing its closing braces.
- `e3dcf8ca` **Sarvam STT** on saaras:v4 (keyterms, finalized transcripts).
- `ef3d9038` Transfers: choose how the number is sent (keep a landline's 0).
- `362ea3e3` Editor badge links to our site.

## 2026-10-01 to 07
- `c258326a` Superadmin can invite people into existing organizations.
- `4cf46216` `6bbe92bf` Each domain (admin / app / client dashboard) keeps to its
  own users.
- `434fe819` `684828ee` `d89af84e` Webhook Sync: "Only keep mapped fields",
  phone preview, LeadSquared URL check.
- `bf669648` Gemini Live reconnects fresh when Google refuses a resume. ⚠️ Not
  tested against a real drop.
- `ef242b46` `da222c79` Scout: the pending action is never hidden; replies can be stopped.
- `e96e3f1b` Post-call QA uses the agent's endpoint and credentials.
- `bb3b71ae` `1c732836` `d526f264` **GPT-6 / Bedrock Luna** support for agents
  and Scout.
- `86904d7b` `decb5c41` **Webhook Sync phases 2–4**: leads dashboard,
  auto-calling, CRM callbacks, alerts.
- `d4030c75` Sarvam: sarvam-30b replaced, sarvam-105b-conversations added.
- `ccf76b0e` **Hopper** LLM (Gemma 4 31B).
- `c535bd4a` Gemini Live opens silently after a recorded greeting.
- `47a96244` **Gemini 3.8 Live** as its own provider; fixes deafness after node
  transitions.
- `662d146f` `af15648a` **Conditional Webhook** node, with each send's outcome
  in the run report.
- `bc55edf3` Code Editor renamed **Custom Tools**; each org can show or hide it.

## 2026-09-25 to 30
- `d2f5b40d` `02d1075a` Every Python package pinned after a rebuild broke Gemini
  Live (google-genai 2.24.0).
- `09d66aeb` `e06d3ef1` Phone-number formatting in templates (`digits` / `phone_digits`).
- `0dbc1d62` `5b391b98` `e710d895` `934d9a61` `7d6f188b` `7dc6f5bc` TATA
  SmartFlo fixes and an API-token login option. ⚠️ Not tested against real SmartFlo.
- `914715ea` **Webhook Sync phase 1**: receive CRM leads over a webhook.
- `adf7e968` `f72c3105` Channels per phone number for concurrent campaign calls.
- `18a6317f` Voice-prompting guide: section order, exit conditions, closing rules.
- `6de43eec` **Stopped routing users and data to Dograh** (docs links, leads, PostHog).
- `12df6f25` VoiceLink: unanswered calls show as "no-answer".
- `a66dd0ba` Gemini Live: old connections no longer trigger reconnects.
  (`02193def` was reverted in `fb18088b`.)
- `d47c7711` `a29bfb1c` **vectus-api** area-manager lookup, replacing knowledge-base
  answers (which invented numbers).

## 2026-09-15 to 22
- `21852039` VoiceLink: echo the offered subprotocol on inbound.
- `67b70c37` **Prepaid wallet billing**: per-minute, pulse and per-call.
- `42ce33c1` **Per-call reports, client KPI dashboard, phone masking**.
- `7dc69c4f` Code Editor hidden from client dashboards.
- `dc944a95` **TATA SmartFlo** telephony provider.
- `b4315db7` `4721ae7c` Gemini Live refreshes the session to cut audio-history cost.
- Docs white-labelled as BotrixAI (`e2740fcd`, `c6f806ea`, `4e07ad82`,
  `ec9c977e` and others), with VoiceLink docs added (`2c646d67`).
- `9bb878a6` `bcfe6b1d` `b3f45afb` Product and widget say BotrixAI; the code
  keeps the Dograh names.
- `6343c729` Custom tools get the workflow id they run under.
- `c063b506` Support chats no longer go to someone else's inbox.
- `f0aad75a` Code Editor: closed two ways one org could reach another's.
- `74c39f85` `1675fad2` **Code Editor**: custom Python tools, written and run on
  the platform.
- `3907bb8d` `36da5003` `293796cc` `37c99343` Scout edits one node or file in
  place and works in the Code Editor.
- `ebac81f5` `bb7d89ad` `9953b50e` Scout is per-org (off by default);
  oversized results no longer break threads.
- `87501b91` `2aa111cd` `312b1eac` Deploy fixes (MinIO image, source-build detection).

## 2026-09-04 to 13: VoiceLink and Scout
- `8c305bee` **Scout**, the in-editor AI assistant, plus fixes `72d6e6da` to `1c217fcd`.
- `324a3daf` `4435e5b8` `1bd8c975` **VoiceLink transfer** to a human agent
  (confirmed working).
- `701f477c` **Vakyam** voice provider.
- `0fc54191` `7515119c` **VoiceLink** telephony provider (inbound and outbound
  confirmed working).

## 2026-07-20 to 08-15: Fork setup
- `b85611ee` **Rumik Silk** voice provider.
- `e2c74385` `2191d97f` **Stop Campaign** action with a real cancelled state.
- `f317fb5d` Auth page redesign; Overview split by role.
- `ff1c7620` `cb4db2d4` Voice Agents page streaming and report timezone fixes.
- `cd11ff11` `f3fdcfc7` `803c8f0d` `2aa028e7` `69f4ec41` **Superadmin**:
  organization management, cross-org agent browser, impersonation.
- `665b9e23` `09016f2e` `8d4450b3` **Three domains**: admin, app, and
  restricted client dashboard.
- `38c4ce1c` Stack Auth wired into docker-compose.
- `1e1e7bd4` `f0f3461b` `817d579c` `6b76551f` Deploy and certificate fixes.
- `3832af7c` `dc7c3a0b` `e31c98bc` **Rebrand to BotrixAI**, favicon, Super Admin link.
