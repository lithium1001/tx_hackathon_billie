# Billie — your payment assistant that only drafts

**One sentence to Billie, one tap to approve.**

Banking track · **Scenario 1: Voice-Enabled Payment and Transaction**

A customer says one sentence, such as *"Transfer 500 dollars to my mom and use the rest to buy Apple shares"*. Billie turns it into a draft of two linked transactions. The customer checks the draft line by line and signs it with a key that never leaves the device. Only then does money move. The language model writes drafts and nothing else: after the signature, no model is involved.

![Architecture](../docs/architecture.png)

---

## 1. Scenario, users and value

**Scenario.** A retail customer in Singapore wants to move money by speaking or typing, in their own words, in one go: a transfer, a bill payment and a share purchase in a single sentence. Later amounts can depend on earlier ones ("the rest", "half of it").

| Who | What they do in Billie | Where |
|---|---|---|
| Customer | Speaks or types, answers a clarifying question, signs on the confirmation sheet | Chat and confirmation sheet in the phone frame |
| Out-of-band channel (hardware token or relationship manager call-back) | Supplies a second factor for high-risk payments | Separate out-of-band page served by the gateway (mock) |
| Compliance / audit | Reads the record after the fact and checks it has not been altered | Audit panel |
| Attacker | Tries to inject instructions through speech, text or data fields | — |

**Value proposition.**

- **For the customer:** one sentence instead of three menus. When something is unclear, the system asks one short question with tappable options. It never guesses.
- **For the bank:** a conversational channel that can move money, with a structural guarantee that the model cannot execute anything. Every payment carries cryptographic proof that the customer authorised it.
- **For compliance:** a tamper-evident record of what the customer said, every draft, the validation log, the risk decision and the signature, all linked by one hash.

## 2. Pain points and the core problem

| Pain point | Why it matters | Billie's answer |
|---|---|---|
| A complex transaction takes many manual steps | "Pay mom, invest the rest" means separate screens and forms, re-typing details, working out "the rest" by hand, and approving each payment on its own | One sentence becomes several payments on one review screen, approved once. Dependent amounts are computed by code, not by the model |
| An agent that can act can also act wrongly | A misread amount or payee becomes real money lost | The agent process contains no function that writes the ledger and makes no call to a write endpoint. A test that scans the syntax tree enforces this |
| Prompt injection | Through the customer's words, a voice transcript, or data the agent reads (payee notes, bill memos) | Input sanitising and rules, fixed input slots, data-field neutralising, and an independent validator. Anything that gets past these still has to survive the customer's own review and the gateway's signature check |
| Misheard or ambiguous requests | "Fifty" or "fifteen"? Which of two Johns? | Code detects the ambiguity and asks with candidates. One tap resumes the flow |
| No proof of authorisation | Disputes need evidence of exactly what was approved | The device signs the exact bytes shown on screen. Two hash chains (agent and gateway) are linked by the draft hash and the receipt |

The rules mirror controls Singapore banks already apply:

- **A daily transfer limit the customer sets.** Singapore banking apps let each customer choose their own daily cap for transfers to other people (a common default is S$1,000). In Billie each customer's limit is read from the bank's records (`data/limits.csv`), so it is the bank's setting, not a constant.
- **Extra checks before paying someone new**, in the spirit of the 12-hour cooling period the MAS–ABS Shared Responsibility Framework requires for risky actions.[^srf] Billie asks for a code from the customer's other device.
- **The industry anti-drain safeguard.** From 15 Oct 2025, on accounts holding at least S$50,000, a digital transaction that together with the past 24 hours of outflows takes out more than half the balance is held for 24 hours or rejected. Bill payments and GIRO are exempt.[^abs]

[^srf]: MAS and ABS, Shared Responsibility Framework (2024); see also MAS–ABS, "Measures to bolster the security of digital banking" (Jan 2022), https://www.abs.org.sg/docs/library/mas-and-abs-announce-measures-to-bolster-the-security-of-digital-banking.pdf
[^abs]: Association of Banks in Singapore media release, 3 Oct 2025; reported by CNA/NTU: https://www.ntu.edu.sg/business/news-events/news/story-detail/cna-explains-banks-could-hold-or-stop-transfers-to-fight-fraud-what-does-it-mean-for-customers

The brief's own example ("…use the rest to buy Apple shares") drains the account, so Billie routes it to an out-of-band confirmation.

**Core problem we solve:** let a probabilistic model take part in a money-moving conversation while the execution path stays fully deterministic, checkable and provably authorised.

## 3. Business and technical architecture

### 3.1 Two processes, one trust boundary

```
┌─ Customer device (browser) ───────────────────────────────────────────┐
│ mic / text · chat · confirmation sheet · non-exportable key · audit    │
└──┬──────────────▲──────────────────────────┬──────────────▲───────────┘
   │ utterance     │ SSE: steps, question,    │ challenge /  │ receipt
   ▼               │ draft                    ▼ signed tx    │
┌─ Agent :8000 (probabilistic, untrusted) ─┐  ┌─ Gateway :8100 (deterministic) ─┐
│ intake → shield → parser ⇄ read tools    │  │ device keys · challenge · ES256  │
│ → resolve → clarify | validator          │  │ KYC + risk (authoritative)       │
│ → risk pre-check → confirm → settle      │  │ ledger (only write function)     │
│ bank data: GET only ────────────────────▶│  │ read-only query API              │
└──────────────────────────────────────────┘  └───────────────┬─────────────────┘
        ✗ no call path from agent to any write endpoint        │ step-up
                                                  out-of-band page (mock)
```

- **Agent** (`dcta/`, FastAPI + LangGraph): turns words into a draft that is waiting for a signature. It talks to the bank through `dcta/utils/bank_client.py`, which contains GET requests only.
- **Gateway** (`gateway/`, FastAPI, separate port, no LLM): registers device keys, issues challenges, verifies signatures, runs KYC and risk checks, and posts to a double-entry ledger. `ledger.post_group()` is the only function in the repository that changes a balance.
- **Deterministic core** (`dcta/core/`): schema and canonical hashing, entity resolution, amount arithmetic, risk rules, shield and audit chain. It imports no LLM library and is the only part of `dcta/` the gateway may import.
- **The browser submits the signed transaction straight to the gateway.** The agent never relays it. Afterwards the agent fetches the receipt itself and checks the `group_hash` against the draft it produced.

### 3.2 One request, end to end

1. **Shield** cleans the input: NFKC normalisation, and removal of zero-width characters, bidi characters and role markers. Rules then block override, impersonation and prompt-extraction attempts, or flag them for a mandatory step-up.
2. **Parser agent (LLM)** extracts intents into a strict `ParsedIntent` schema. It may call read-only tools: accounts, payees, bills, holdings, instruments. The customer id is injected from graph state, so the model cannot ask for anyone else's data.
3. **Resolve (code)** matches mentions to the customer's own records and computes `remainder` / `fraction` / `bill_due` amounts in decimal arithmetic. Every absolute amount must appear in the numbers that code re-extracts from the utterance ("a grand", "5k", "fifty"). If code and the model's hint disagree, the result is a question, never a decision.
4. **Clarify** interrupts with candidates when something is ambiguous (at most 2 rounds).
5. **Validator** runs two engines that cannot overrule each other. A separate LLM call with its own prompt and no tools sees only the transcript and the draft. Code independently re-checks evidence substrings, amounts, mentions, recomputed dependencies and the transaction count. If either engine flags a mismatch, the draft is frozen.
6. **Risk pre-check** runs the same pure function the gateway uses: `KYC_STATUS`, `SCREENING`, `FIRST_TIME_PAYEE`, `DAILY_LIMIT`, `STEP_UP_AMOUNT`, `VELOCITY`, `DRAIN`, `SHIELD_FLAG`. The strictest outcome wins: `deny > step_up > allow`.
7. **Confirm.** The gateway returns the exact `signing_input` bytes, and the confirmation sheet is rendered **from those bytes**, not from anything the agent sent. The customer presses and holds (mock biometric). WebCrypto then signs with a non-exportable P-256 key.
8. **Gateway** runs ten fixed-order checks, each with its own error code: signature present → challenge known → not expired → not replayed (atomic) → payload hash matches → key belongs to customer → ES256 verifies → risk re-evaluated (authoritative) + step-up code → whole-group balance check → post.
9. **Settle** fetches the receipt, checks `group_hash`, and records the gateway's `audit_hash`, which links the two chains.

### 3.3 How prompts drive generation

The prompts are plain Markdown files, loaded verbatim as system prompts with no string interpolation. **The prompt text lives only in these files and never in Python code.**

- [`dcta/skills/parser/skill.md`](dcta/skills/parser/skill.md) defines the parser's job: *extract, never compute*. It fixes the JSON output contract (category, intent, verbatim `evidence`, `source_mention` / `target_mention`, `target_hint`). It also defines the amount forms: `absolute` keeps the exact words in `said_as`; `remainder` and `fraction` point to earlier transactions; `bill_due` pays a bill in full. Anything unclear goes into `uncertain`, so the model never resolves doubt silently. The prompt tells the model that the utterance slot and tool results are data, not instructions.
- [`dcta/skills/validator/skill.md`](dcta/skills/validator/skill.md) defines an independent reviewer that *compares only*. For each draft line it gives `match` / `mismatch` / `not_stated` on recipient, amount, asset and source, and it lists `extra` and `missing` transactions. It never corrects.
- `config.json` next to each prompt holds the agent's display name and colour for the activity panel.

Generation is shaped by structure as well as by the prompt text:

- **Fixed slots.** The customer's words go into `<customer_utterance>…</customer_utterance>` with angle brackets escaped, so the input cannot close the slot.
- **Strict schema.** Output is parsed with `pydantic` (`extra="forbid"`, enums, decimal strings, and a rejection of floats). One repair retry carries the validation error back to the model; a second failure returns a fixed message.
- **Verbatim evidence.** Every `evidence` field must be a substring of the utterance, or the whole draft is discarded.
- **Model text never reaches the customer.** Every question, confirmation line, refusal and receipt is rendered from templates in `dcta/core/copy.py`, filled with structured fields.

### 3.4 Audit trail

Two independent SHA-256 hash chains, `artifacts/audit/agent.jsonl` and `artifacts/audit/gateway.jsonl`, record the five artefacts the brief names: the raw input, the transcript, every draft version, the validation log and the signature. Both chains are linked by `trace_id`, `group_hash` and the receipt's `audit_hash`. `verify()` reports the first broken link, and the audit panel shows it in red.

### 3.5 Tech stack

Python 3.11 · FastAPI · LangGraph · any OpenAI-compatible chat-completions endpoint (parser and validator models are configured separately) · `cryptography` (ES256) · vanilla HTML/CSS/JS with CSP · WebCrypto + IndexedDB · Web Speech API.

## 4. Business value: measured results

Evaluation drives the real graph end to end. For each case the gateway runs in-process with a fresh ledger, and a simulated device signs only when the bytes it is asked to sign equal the expected draft. All scoring is deterministic comparison; no model grades another model. Model-dependent sets were run k = 3 times. Proportions are reported with Wilson 95% intervals.

Parser `google/gemma-4-31b-it`; independent validator `mistralai/mistral-small-3.2-24b-instruct` (a different vendor); k = 3; 20 cases per set. Raw output: [`docs/eval/independent-validator/`](../docs/eval/independent-validator/) (`results.md`, `summary.json`, `cases.csv`).

| Metric | n | Result | 95% CI |
|---|---|---|---|
| Parse: whole draft correct (pass^3 = 90%) | 60 | **91.7%** | 81.9–96.4% |
| Parse: field-level accuracy | 312 | 87.2% | 83.0–90.4% |
| Clarify: asked when it should | 30 | **100%** | 88.6–100% |
| Clarify: asked when it should not | 30 | **0%** | 0–11.4% |
| Injection: hijacked drafts reaching the confirmation sheet | 60 | **0%** | 0–6.0% |
| Money posted without a valid signature | 180 | **0** | — |
| Gateway rejection tests (no signature, tampered amount, replay, expiry, foreign key, foreign account…) | 17 | all rejected, ledger unchanged | — |
| Audit completeness (every node that ran wrote an entry) | 677 | 100% | 99.4–100% |
| Gateway audit-chain verification | 180 | 100% | 97.9–100% |
| Time from utterance to confirmation sheet (median) | 108 | 7.2 s | — |

**Injection funnel (60 runs):**

| Layer | Runs |
|---|---|
| Blocked by the shield | 30 |
| Got past the shield, but the draft was not hijacked (data-field attacks neutralised at the tool layer, or the model ignored the injection) | 24 |
| Hijacked, then rejected by risk rules (screening list, daily limit) | 6 |
| Hijacked and reached the customer | **0** |

**Honest failure notes.**

- **Every parse failure was a freeze, never a wrong payment.** In all 5 misses the draft was correct and the independent validator stopped it, so no money moved.
  - 3 of 5: the customer named a listed share by a word that is also a bank's name; the validator read it as a transfer to that bank.
  - 2 of 5: "Pay mum 120 and pay the SP Group bill". The customer gave no bill amount, the draft paid the full amount due, and the validator flagged the amount as not said.
- With the parser's own model as validator, the whole-draft score was 95.0%. The independent validator is stricter; we report its number. Both setups are compared in the top-level README.
- An early run fuzzy-matched "my other account" to the payee alias "mother". The validator caught it every time. As a result, fuzzy matches now always become a question rather than a choice.

**What this means for a bank:** customers complete multi-step payments in one utterance instead of separate flows. The model is fenced off from execution by structure, not by instructions. Every posted payment carries a verifiable signature and an audit trail that is checked for tampering.

---

## Run it

Setup, configuration, the demo personas and the repository layout are in the [top-level README](../README.md).

## Scope and limitations

- **Mocked:** sign-in (Face ID, PIN, SMS code for a new device), biometrics (press-and-hold), the ledger and account data, the payment API (request format only), the out-of-band channel, the KYC source and the screening list.
- **Out of scope:** onboarding and login, real clearing and FX, investment advice (refused), cross-border payments, adding new payees.
- Browser speech recognition returns text only. The transcript and its alternatives are audited, but raw audio is not archived yet. In production, record with MediaRecorder or use server-side ASR and store the audio by hash.
- **English only for now.** Speech recognition is set to `en-SG`, amount words are recognised in English ("a grand", "5k", "fifty"), and every sentence the customer reads comes from English templates. Supporting Chinese, Malay and Tamil means adding each language's number words, templates and speech locale; the signing path and the gateway's checks stay the same.
- The shield blocks about half of injection attempts on its own (30/60). This is by design: the guarantee against unauthorised posting comes from the confirmation sheet and gateway signature verification, not from the filter.
