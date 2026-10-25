# Boat ID ("guess, then reveal") — plan for review

_Written 2026-10-25 for Kevin's review. Planning only: no feature code, no
service restarts, nothing in the running system was changed. The numbers below
come from read-only analysis of the live AIS database and service logs on the
Pi._

> **Note on the brief:** the pasted brief was cut off mid-sentence in item 8
> ("…how often an AIS match would be possible from the"). I've assumed it ended
> "…from the existing AIS data", and did that experiment (section 3.1).

## Decisions so far (Kevin, 2026-10-25)

| # | Decision |
|---|---|
| **D2** | **Use the single existing dongle for now** (no second SDR yet): snap-triggered AIS (option A) + fixes before/after the photo (option B). Revisit after testing. |
| **D3/D4** | **Hosted AI with Claude Opus 5** (`claude-opus-5`, $5 / $25 per million input/output tokens → roughly **$0.04–0.06 per photo**). Local model postponed. Note: Opus 5.5 (newer, $4 / $20) was the recommendation; pending Kevin's confirmation of which he meant. |
| **D6** | **No lock-down yet** — Kevin is effectively the only user. Photos keep random, unguessable IDs and aren't listed anywhere public. Revisit before anyone else uses it. |
| **Scope** | **Sailboats only** for the first version, to prove the idea. |

**Next (2026-10-25): Kevin chose option B** — skip the separate Phase 0
experiments and build the Phase 1 sailboat pages directly, in usable pieces:
(1) Snap page + Claude Opus 5 guesses, (2) comparison screen, (3) AIS reveal
(snap-triggered AIS burst), (4) scoring + "My sightings" log. Experiments
happen naturally through use.

Still open: D1 (name — defaulting to `/boats`), D5 (library bootstrap), D7, D8, D9 — the defaults below apply unless Kevin says otherwise.

### What "sailboats only" means in practice
- **Why it fits:** of the 219 vessels the AIS database knows, **53 are type "sailing"**, 49 named, 52 with length (median ~13 m / 42 ft). Most AIS traffic here is cruising sailboats on moorings, so the reveal has real data from day one.
- **Your quick guess (tap chips):** hull — *monohull / catamaran / trimaran*; rig — *sloop / cutter / ketch or yawl / schooner / not sure*; size — *under 30 ft / 30–39 / 40–49 / 50+*; optional free-text make/model.
- **What the AI is asked for:** the same fields, plus visible features (cockpit centre/aft, deck saloon, bowsprit, transom/sugar-scoop, hull colour, dodger/bimini, davits) and up to 5 make/model candidates with reasons.
- **Scoring:** points per field (hull, rig, size, model), with an AIS-verified bonus when the size and "sailing" type check out against the identified boat.
- **Non-sailboats:** "That's not a sailboat" is a valid answer and is stored, but not scored, in v1.

**Decisions you need to make are marked like this:**
> **DECISION D# — question.** *Recommended default:* …

There's a summary of all decisions at the end (section 10).

---

## 1. My understanding of the idea (and where I'd push back)

**In my words:** you see a boat, snap a photo in the NavaOS web app, and make a
quick guess ("sailboat", or "I think that's a Catalina 30"). The app makes its
own ranked guesses and shows them side by side with your photo, swipeable, like
Merlin or iNaturalist. You pick "that's it" or "none of these". Then comes the
reveal: if the boat broadcasts AIS, the kit knows which boat was really there,
so the app can tell you "that was SEDNA, a 38 ft sailboat". Everything you
confirm becomes a labeled example, so the app gets better the more it's used.
Your guesses get scored against the AI's, there's a personal collection, and
boats you see more than once build up a history. The bigger picture: a kit
(Pi + SDR + antenna) people put on their boat or waterfront house, each one a
local node.

**What I agree with most:** treating the AI as "narrow it down, the human
confirms" rather than "the AI is right". That's the only honest framing:
make/model from a photo is genuinely hard.

**Where I'd challenge or clarify:**

1. **AIS confirms *which boat* and *how big*, not *which model*.** AIS sends the
   name, MMSI, a coarse type ("sailing", "pleasure craft", "tug"), and length/
   beam. It never sends make or model. So the AIS reveal can say "that was
   SEDNA, sailing, 11.6 m long" and can check whether a guess is *plausible*
   (a "Catalina 30" is about 9 m, so that guess doesn't fit an 11.6 m boat).
   But it can't confirm "Catalina 30" vs "Hunter 30". Model-level truth only
   ever comes from people: the first time someone confirms SEDNA is a "Valiant
   37", that's remembered against her MMSI for every later sighting. I think
   the game works with that. The reveal is "who she is", and model knowledge
   builds up over time.
2. **"Silently validates or corrects" is risky.** In a mooring field there can be
   ten AIS boats inside your camera's view. If the app quietly "corrects" you to
   the wrong one, it teaches the library something false. **Recommendation:**
   the reveal always *shows* the AIS match and its confidence, and only
   auto-applies it when there's one clear candidate. Otherwise it asks "which
   of these?". (D7)
3. **The AIS coverage problem is bigger than the brief assumes** (section 3).
   With today's timesharing, the AIS reveal would almost never work for a moving
   boat. That's fixable, but it needs a hardware or radio-time decision.
4. **Most small powerboats have no AIS at all.** Center consoles, runabouts and
   many trawlers. For those boats the "reveal" is just you vs the AI, with no
   ground truth. The fun has to survive that (section 3.5).
5. **The live-camera future and the sellable-kit future both depend on
   continuous AIS**, which points to a second SDR dongle (section 3.2). I'd
   treat that as near-certain for the product, not optional.
6. **Privacy is the hidden big issue.** A per-boat history of "seen here, at
   this time, with a photo" is effectively a tracking log of someone's boat and
   movements. Fine for your personal notebook; sensitive the moment it's shared
   or sold (section 6).

---

## 2. How it fits the existing system

**Short version:** a new, separate module beside the radio, sharing the AIS
database. Nothing in the voice/scan/recorder path changes, apart from the AIS
scheduling decision in section 3.2.

| Piece | Where | Notes |
|---|---|---|
| Routes | new file `api/boatid.py`, mounted under **`/boats/...`** in `main.py` | Kept out of `/radio` because it isn't radio. Pages: `/boats/snap` (camera), `/boats/reveal/{id}` (comparison + reveal), `/boats/log` (your collection), `/boats/vessel/{mmsi}` (one boat's history). |
| Pages | `api/templates/boats_*.html` | Same dark theme, header and palette as the player. |
| Logic | new package `navaos_boatid/` (capture, matching, candidates, library) | Kept apart from `navaos_audio/` so a crash or heavy job here can't touch the radio. |
| Photos | `~/navaos-data/boatid/photos/YYYY-MM/<random-id>.jpg` | Outside the repo, so never on GitHub (same rule as `static/hero.jpg`). Random IDs, so photo URLs can't be guessed. |
| Library + sightings | `~/navaos-data/boatid/boatid.db` (SQLite) | First database in the project. The JSON-file pattern used so far (vessels.json, .listened.json) gets awkward once you have photos ↔ models ↔ boats ↔ sightings, plus image "fingerprints" for similarity search. SQLite is built into Python: one file, no server, easy to back up. |
| Kit identity | `~/navaos-data/kit.json` {kit_id, name, lat, lon, antenna height} | Every sighting row stores `kit_id`, so data from several kits can be merged later. |
| AIS | reads `navaos_audio/ais.py` VesselDB (positions, tracks, names, sizes) | Needs one addition: keep the raw fixes (not just the thinned 24 h track) for a few minutes after each photo, so the match can use the best data. |
| Transcriber | no direct link in v1 | Later and fun: "you photographed SEDNA; she called the bridge at 15:04" (transcripts already carry vessel tags). Shares the CPU budget, see 4.4. |

**What gets stored per sighting:** kit, who took it (browser id for now), time,
phone position + accuracy, compass heading + accuracy, where in the frame you
tapped the boat, your guess, the AI's guesses, what you confirmed, the AIS match
(MMSI, confidence, or "none"), and the photo. Plus a **source** field
(`phone` / `fixed-camera`) and a camera id, so live cameras can use the same
table later without a redesign.

> **DECISION D1 — name for the feature/URL.** *Recommended default:* `/boats`,
> titled "Boat ID", until you pick a product name.

---

## 3. The AIS matching problem (the crux)

### 3.1 What the real data says

From the live system, read-only:

- **The database knows 219 vessels** (173 Class B, 23 Class A, 23 other). 47
  were heard in the last 24 h; about 40 of those were moored.
- **Only 5 boats moved more than 200 m in the last 24 h.** Most AIS traffic
  here is boats sitting on moorings, with a few movers a day.
- **AIS windows run about every 12 minutes, but only while nobody is
  listening.** In the last 24 h: ~5 per hour overnight, **zero from 09:00 to
  12:00 yesterday**, and **none from 07:31 today until now (~14:00)**, because
  you were listening to the scan the whole time. That's the shared-radio rule
  working as designed (voice always wins). But it means **AIS goes dark exactly
  when someone is using the app**, and the photographer *is* someone using
  the app.
- **Moving boats travel 1.5–3.9 km between windows** (fixes 10–12 min apart at
  4–10 kn). Guessing where they were at the exact photo moment from that is
  unreliable, especially around the river bends and the bridge.

**Simulation:** I picked 2,000 random moments in the last 24 h and checked
whether AIS had a position fix close to each one:

| Fix within… | Any vessel (24 h) | Moving vessel (24 h) | Any vessel (08–18 h) | Moving vessel (08–18 h) |
|---|---|---|---|---|
| ±1 min | 18% | 4% | 11% | **3%** |
| ±3 min | 41% | 12% | 23% | 10% |
| ±6 min | 66% | 23% | 38% | 18% |

(The "any vessel" numbers understate it for moored boats: their positions
barely change, and the database deliberately stores few points for boats that
aren't moving. **For moored boats, position is effectively always known.**)

**Conclusion:** with today's settings, a reveal for a **moored** boat usually
works. A reveal for a **moving** boat, the more exciting case ("what's that
going under the bridge?"), would work maybe **3–10%** of the time in daylight.
Not good enough.

### 3.2 Fixing AIS coverage: three options

| Option | How it works | Moving-boat reveal | Cost |
|---|---|---|---|
| **A. Snap triggers AIS** | Opening the camera page starts AIS straight away and keeps it running (in 90 s bursts) while the page is open. The voice scan pauses. | Good: fixes every 10–30 s right around the photo | Voice scan is deaf while someone is photographing. Under the shared-radio rules, it would need the radio to be free or held by the photographer. |
| **B. Wait for the next window, then interpolate** | Save the photo; the reveal waits up to ~12 min for the next AIS window and estimates position from the fixes before and after. | Fair on straight runs, poor around bends; useless while someone listens all day | No extra cost, but a slow reveal ("check back in 10 min") |
| **C. Second RTL-SDR dongle for AIS** | One dongle for voice, one dedicated to AIS, both on the same antenna through a splitter (or a separate small AIS antenna). | **Excellent: continuous, instant** | ~$35–45 hardware plus ~$15 splitter. A splitter costs ~3 dB of signal on each side; signals here have margin, but the noise meter can check. Small extra CPU: AIS-catcher was light in testing. |

> **DECISION D2 — how to get AIS for photos.** *Recommended default:* **buy a
> second dongle (C) now**, and build **A** as the fallback for single-dongle
> setups. For the sellable kit, two receivers should be standard: every
> feature beyond voice (reveal, live cameras, alerts) needs continuous AIS.

### 3.3 What the phone can tell us

All of this needs the page on HTTPS (vhf.nnwx.com already is) and the user's
permission. **These figures are typical published behaviour, not measured on
your phones — that's experiment E2 in section 8.**

| Data | iPhone (Safari) | Android (Chrome) | Typical accuracy |
|---|---|---|---|
| **Time** | Device clock | Device clock | Within about 1 s (network time). The Pi's clock is network-synced too. |
| **Location** | Geolocation API; asks permission once per site | Same | Outdoors with GPS: ~5–15 m. **If "Precise Location" is turned off for Safari on iOS, it can be off by ~1–3 km**, which breaks matching. The app must detect this from the reported accuracy and say so. |
| **Compass heading** | Needs an explicit "Allow motion & orientation" prompt, triggered by a button tap (`DeviceOrientationEvent.requestPermission`); gives a compass heading plus an accuracy estimate | Absolute-orientation event; usually no prompt; heading worked out from the orientation | Often ±5–15° when calibrated. **Commonly off by 20–30°+ near steel, engines, boat electronics, or a metal roof.** Magnetic, so it needs correcting by the local variation (~7–8° W at St. Augustine). Which way the heading points when the phone is held up as a camera needs checking per platform. |
| **Photo metadata (EXIF)** | Photos chosen from the library may have location stripped | Varies | **Don't depend on it.** Capture time, location and heading ourselves at the moment the shutter is tapped, using the in-page camera. |
| **Camera field of view** | Not reported by the browser | Not reported | Main camera ≈ 65–75° wide in landscape, ~50° in portrait, narrower when zoomed. Assume a range and let the tap position refine it. |

**Practical consequence:** the snap page uses its own camera view (not "pick
from library"), and asks for location and motion access once, with a short
explanation. Library photos are still allowed, but they get a "no reveal
possible" label unless their time and place can be recovered.

### 3.4 Matching "what the camera saw" to AIS boats

1. Estimate each AIS vessel's position at the photo time: moored = last fix;
   moving = interpolated between the fixes before and after (or projected
   forward from the last fix if it's recent). Each estimate carries an
   uncertainty circle that grows with time since the fix × speed.
2. Work out the bearing and distance from the phone to each vessel.
3. **Ask the user to tap the boat in the photo.** The tap's position in the
   frame shifts the compass heading (tap on the right edge = ~+30° of a 65°
   view). This narrows the cone a lot and handles "several boats in view".
4. Keep vessels inside the cone (heading ± compass error ± size of the
   uncertainty circle) and within believable photo range (default ≤ 2 km; most
   identifiable photos are < 800 m).
5. Rank by how far off-angle they are, plus a **size check:** apparent length in
   pixels ≈ real length × focal factor ÷ distance. A 12 m boat 300 m away
   should look about the right size; a 40 m yacht wouldn't.
6. **Show, don't silently decide:**
   - one clear winner → "AIS says that's SEDNA" ✔
   - 2–5 plausible → "Which of these is it?" with names, sizes and distances
   - none → "No AIS boat there" (see 3.5)

The confidence shown should be honest ("probably", "one of these 3").

> **DECISION D7 — auto-apply AIS identity?** *Recommended default:* only when
> there's a single clear candidate; otherwise ask.

### 3.5 Boats with no AIS

Likely the majority of small powerboats. The reveal screen says, plainly:
**"No AIS match — this boat isn't broadcasting AIS (most small boats don't)."**
Then:
- your confirmation stands, marked **"unverified"** in your log, and scored only
  you vs the AI (no "ground truth" bonus);
- it still teaches the library, but **unverified examples weigh less** than
  AIS-verified ones when the app ranks future candidates;
- later: if the same boat is identified again (by you, or another kit user),
  matching sightings can "verify" each other.

---

## 4. The image side: producing ranked candidates

Two different jobs:
- **(a) Category:** sailboat / catamaran / trawler / center console / sportfish /
  pontoon / etc. This is fairly easy.
- **(b) Make/model:** "Catalina 30" vs "Hunter 31". This is hard, and depends
  heavily on having reference photos of that exact model.

### 4.1 Hosted AI (Claude vision)

Send the photo plus a short instruction; get back the category, a description
("sloop, ~35 ft, center cockpit, dark blue hull, bimini"), and up to 5 make/
model guesses with reasons. Strong on category and on describing the boat, and
it recognises distinctive popular models. **It will sometimes be confidently
wrong on model** — fine, because the user confirms.

- **Cost (current list prices, Oct 2026):** a phone photo resized to ~1 megapixel
  is roughly 1,300–1,600 input tokens, plus ~500 tokens of instructions and
  ~1,000–2,000 output tokens (including the model's reasoning). Per photo that's
  about **$0.03–0.05 on Claude Opus 5.5** (the default: $4 / $20 per million
  input/output tokens), **~$0.015–0.025 on Sonnet 5.5** ($2 / $10), and
  **under ~$0.01 on Haiku 4.5** ($1 / $5). At 20 photos a day: about $0.60–1.00
  a day on Opus, roughly $20–30 a month. These are estimates: measure with real
  photos in experiment E3.
- **Latency:** typically 5–20 s. Fine with a "thinking…" animation.
- **Needs:** an Anthropic API account and key, stored like the PIN (on the Pi
  only, never in the repo), and **user photos leave the Pi** (privacy, section 6).
  It also needs internet; on a boat with poor LTE this can fail, so fall back to
  local.

### 4.2 Local model on the Pi 5

A small image model like CLIP turns a photo into a "fingerprint" (embedding).
Similar boats get similar fingerprints, so "find the closest reference photos"
becomes a quick comparison.
- **Category without any references:** CLIP-type models do this decently out
  of the box.
- **Make/model:** only as good as the reference library. With a few photos per
  model, retrieval works surprisingly well for distinctive boats and poorly for
  look-alikes.
- **Speed on Pi 5 (my estimate, not measured — experiment E4):** roughly 1–3 s
  per photo with a small model, if given one CPU core at low priority. Free per
  photo. Works offline.
- **Install:** a few hundred MB (model + runtime), built or installed into
  `~/src` like whisper.cpp.

### 4.3 Recommendation

> **DECISION D3 — hosted, local, or both?** *Recommended default:* **both**,
> each doing what it's good at:
> - **Claude** (Opus 5.5 unless you choose a cheaper tier) for the
>   description, the category and the first make/model guesses, especially
>   early on while the library is nearly empty;
> - a **local model** for "show me similar photos" from your own growing
>   library, which gets better with every confirmation;
> - offline / no-key fallback: local only.

> **DECISION D4 — which Claude tier?** *Recommended default:* start with
> **Opus 5.5** for accuracy while we measure. Cheaper tiers are your call once
> experiment E3 shows whether they're good enough.

### 4.4 Never starving the radio

The radio currently uses little CPU (the demod ~5% of a core; load average
~1.4 of 4 while listening). The transcriber already runs at low priority on 2
threads. The image work will:

- run in a **separate process** (not inside the web server), at the **lowest
  priority** (`nice 19`), **pinned to one CPU core**, one photo at a time;
- **pause the transcriber's backlog** while an ID job runs (live transcripts
  continue);
- have a simple **guard:** if the scan/recorder ever falls behind (it already
  logs failures), new ID jobs wait;
- for the hosted path, it's mostly waiting on the network, so almost no CPU.

---

## 5. The reference photo library

**Hard rule from your brief, agreed: no scraping.** Listing sites (YachtWorld,
boats.com, BoatTrader) forbid it in their terms, and their photos belong to
brokers and sellers. Manufacturer photos are copyrighted too.

**Legitimate ways to build it, best first:**

1. **Your own photos.** You own them. Photographing boats on public water is
   generally legal in the US (not legal advice). St. Augustine's anchorages
   give hundreds of subjects, and AIS gives the boats' names for free.
2. **User contributions.** Every confirmed sighting, *if the user agrees* (an
   explicit "add to the shared library" switch, with simple terms granting the
   right to use the photo).
3. **Wikimedia Commons** through its official API (not scraping). Many
   CC-licensed or public-domain boat photos, some filed by model. Each photo's
   license and author must be stored and shown (attribution). Share-alike
   licenses need care if photos are redistributed. Coverage of specific
   recreational models is patchy.
4. **Ask manufacturers and dealers.** Many have press/media kits; some would
   likely say yes to "show your boat as the reference photo" (it's free
   exposure). Needs written permission per source.
5. **Model facts** (make, model, years, length, rig, hull type) from public spec
   sheets. Facts aren't copyrightable, but don't copy another site's database
   wholesale: build the list by hand for the ~50–100 models seen locally.

**Not suitable:** academic ship-photo datasets are mostly research-only licenses
and commercial ships. US Coast Guard vessel documentation records are public
data and could link names to builders, but bulk access has its own terms:
check before using, don't scrape.

**AI-generated "reference" images:** no. They'd be misleading.

**Storage:** all photos live under `~/navaos-data/boatid/`, outside the repo. I'd
also add a `.gitignore` guard for `*.jpg`/`*.png` anywhere except deliberate
UI assets, as a safety net.

> **DECISION D5 — library bootstrap.** *Recommended default:* your own photos
> + Commons (with attribution) for v1. Ask 3–5 manufacturers later, once
> there's something to show them.

**Legal risk flags (not legal advice):** copyright on reference photos (avoided
above); terms of service of any data source; and for a sold product,
user-content terms and a privacy policy. Worth an hour with a lawyer before
selling kits.

---

## 6. Privacy

**Today:** vhf.nnwx.com has no login. Anyone with the address can open every
page. Fine for radio audio (it's public broadcast). **Not fine for photos**:
they can show people, children, faces, boat names and registration numbers,
and with time and place attached they become a log of where a particular boat
was.

| Question | Recommendation |
|---|---|
| Who can see photos and sightings? | **Only you (PIN-gated) in v1.** No public pages. Photo URLs use random IDs and also need the PIN. |
| Who can upload? | PIN holders only, so strangers can't fill the Pi with photos. |
| Third parties | If Claude is used (D3), photos go to Anthropic for processing. Say so on the snap page. |
| What's stored | The photo with **metadata stripped** (we keep our own time/place/heading separately); your guess; the AI's guesses; the confirmation; the AIS match. |
| Your phone's location | Needed for matching. Keep it with the sighting for your own log. For any future shared or multi-user version, store it **rounded** (~100 m) or drop it once matched. Exact locations of where someone stands repeatedly can reveal their home. |
| How long | Raw photos: **keep until you delete them** in v1 (it's your notebook). Shared library entries: cropped to the boat where possible. Later: optional face blurring. |
| Per-boat history | Private to you in v1. **Before any sharing or product launch: boat owners need a way to opt out**, and histories shouldn't be public by default. This is the part most likely to cause trouble. |

> **DECISION D6 — access control.** *Recommended default:* **PIN-gate the
> Boat ID pages** (reusing the existing override PIN, or a separate one) for
> v1. Before anyone else uses it: Cloudflare Access (free for small teams) or
> real accounts.

---

## 7. Phased plan

Effort is in "sessions like ours" (a few hours each), assuming no surprises.

**Phase 0 — experiments before building (1–2 sessions)**
See section 8. The outcome decides D2, D3 and D4.

**Phase 1 — MVP: "is it fun?" (4–6 sessions)**
- Snap page: in-page camera, captures time/location/heading at the shutter,
  tap-the-boat, quick category chips or free-text guess.
- AI guesses (Claude): category + up to 5 make/model candidates with a short
  description.
- Comparison screen: your photo on top, candidates below, swipe left/right.
  Reference photos come from your own confirmed photos and Commons; where
  there's none yet: "No reference photo yet — yours could be the first".
- "That's it" / "None of these" (v1: "none" lets you type the answer or pick a
  category).
- AIS reveal for moored boats from existing data; moving boats via the
  snap-triggered AIS burst (option A), or the second dongle if D2 = C.
- Scoring: you vs AI (category right, model right, AIS-verified bonus).
- My Sightings log, newest first.
- PIN-gated; SQLite; photos in ~/navaos-data.

**Phase 2 — "it gets smarter" (3–4 sessions)**
- Local model + similarity search over your library ("photos like this").
- Smarter "none of these" (broaden or narrow by category/size).
- Per-boat pages (MMSI): every sighting, photos, confirmed model, plus her VHF
  transcripts mentioning her name (ties to the transcriber).
- Collection features: "boats seen", "models seen", streaks, a sightings map.

**Phase 3 — kit/product groundwork (5–8 sessions, plus hardware)**
- Second-dongle continuous AIS as standard; setup wizard.
- Accounts (or Cloudflare Access), privacy policy, owner opt-out.
- Multi-kit: kits share library entries (with consent) via a small central
  service. That's a hosting decision for later.
- Packaging: install script, config, updates.

**Phase 4 — fixed cameras (later, not designed in detail)**
- A camera at the bridge, fixed bearing (no compass needed), the same AIS
  matching auto-labels boats passing. Same `sightings` table with
  source = fixed-camera.

---

## 8. Experiments to run first (Phase 0)

| # | Experiment | What it tells us | Effort |
|---|---|---|---|
| E1 | **AIS coverage** — done above (section 3.1) | Today's timesharing can't support moving-boat reveals | done |
| E2 | **Phone sensor test page** (PIN-gated, nothing stored): point at a known landmark, e.g. a Bridge of Lions tower, from a known spot. Log GPS accuracy and compass heading vs the true bearing, iPhone and Android, indoors/outdoors, near/away from metal. | Real compass error → how wide the match cone must be, and whether the tap-in-photo trick is enough | 1 session + 20 min of your time |
| E3 | **Claude vision test** on ~30 of your photos of AIS-identified local boats (you'd take them; AIS gives the names/sizes) | Real category and model accuracy, real cost per photo, Opus vs Sonnet vs Haiku | ~1 session, a few dollars of API use, needs an API key (D4) |
| E4 | **Local CLIP benchmark** on the Pi, at low priority while the radio runs | Real speed and CPU impact; category accuracy without any library | ~½ session (installs a model in ~/src) |
| E5 | **Second dongle + splitter** (if you buy them) | Whether the splitter loss hurts voice reception (noise meter + recordings before/after) | ½ session |

---

## 9. Risks and unknowns

- **Model-level accuracy may disappoint** until the library has many photos per
  model. Mitigation: lead with category + "who she is" (AIS), and make model
  guesses explicitly "suggestions".
- **Compass error near boats and metal** could make cones too wide in crowded
  anchorages. Mitigation: tap-the-boat + size check + "which of these?".
- **AIS here is mostly moored sailboats.** Great for the anchorage, less so for
  powerboat traffic, which mostly has no AIS.
- **Single dongle:** without D2 = C, Boat ID and listening compete for the radio.
- **Privacy/legal** for the product (section 6), plus FCC: the kit is
  receive-only, which is fine.
- **Library cold start:** the first weeks are mostly the AI's guesses with few
  reference photos. That's why your own photos matter.
- **Pi load:** manageable with the rules in 4.4; to be measured in E4.

---

## 10. Decisions summary

| # | Decision | Recommended default |
|---|---|---|
| D1 | Feature name / URL | `/boats`, "Boat ID" |
| D2 | AIS coverage for photos | Buy a 2nd RTL-SDR + splitter now; snap-triggered AIS as the single-dongle fallback |
| D3 | Hosted vs local image AI | Both: Claude for guesses/descriptions, local model for similarity search |
| D4 | Claude tier / API account | Opus 5.5 while measuring; revisit after E3. Needs an API key stored on the Pi only |
| D5 | Library bootstrap | Your own photos + Wikimedia Commons with attribution; manufacturers later |
| D6 | Access control | PIN-gate Boat ID for v1; Cloudflare Access or accounts before others use it |
| D7 | Auto-apply AIS identity | Only with one clear candidate; otherwise ask |
| D8 | Photo retention | Keep until you delete (personal notebook); cropped, consented copies for the shared library |
| D9 | Scoring | Simple points: category right, model right, AIS-verified bonus; no leaderboards in v1 |

**Suggested next step after your review:** decide D2 (dongle) and D4 (API key),
then run experiments E2–E4 before writing any Phase 1 code.
