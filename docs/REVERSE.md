# What the PC diagnostic tools gave us

Catalogue of everything extracted from the decompiled/disassembled Windows tools, what
this logger already uses, and what is deliberately left alone. Companion to
`PROTOCOL.md` (which describes the wire protocol itself).

Sources on the author's machine (not in this repo):

| artefact | what it is |
|----------|------------|
| `guzzidiag_V0.42.exe.txt` | Hex-Rays decompile, 59 k lines, PureBasic. The most complete source. |
| `IAWDiag_V0.52.exe.txt` | Hex-Rays decompile, same author/architecture, **six** ECU families. |
| `GuzziDiag_V0.61.exe`, `GuzziDiag_V0.60.app` | newer builds, binaries only |
| `JPDiag28c.exe` + `JPDiag28c.ini` + `lang/*.ini` + `*Dtc.dll` | third-party VB6 tool; junk decompile, but rich plain-text resources |

## 1. Variable names — no

Both tools are stripped. Everything named in our notes (`sub_415D1B`, `unk_45D938`, …) is
an address, not a symbol. What *is* recoverable is the **role** of a few globals, because
they are written right before every transport call:

| global (GuzziDiag / IAWDiag) | role |
|------------------------------|------|
| `unk_45D938` / `unk_495598` | service byte (0x21, 0x30, 0x31, …) |
| `unk_45D939` / `unk_495599` | LocalID / sub-function |
| `unk_45D93A` / `unk_49559A` | second parameter |
| `dword_45D934` / `dword_495594` | frame format: 2 = addressed, 0 = short header |
| `dword_45D92C` / `dword_49558C` | payload length handed to the comm thread |
| `byte_45D949` / `byte_4955A8` | response bytes |

That is enough to enumerate every request either tool can send — which is how the rest of
this document was produced.

## 2. Parameter names — **not in any binary** (verified twice)

- The tools store UI text under keys (`View_42`, `Value_1`…`Value_69`, `Fault_3`…) and load
  the actual strings from an external `GuzziDiag.ini` / `IAWDiag.ini` written next to the
  exe on first run.
- Scanning both binaries for UTF‑16 strings returns the *keys* only, never the labels.
- The default INI embedded in GuzziDiag V0.42 (`byte_452348`, 4056 bytes, recoverable in
  full from the decompile) contains `[Settings] [Modells] [Deutsch]` — menus, buttons and
  the model table, but **no `Value_N` names**.
- Structurally there is nothing to name: each family's parameter dispatcher is simply one
  `21 <rli>` getter per rli over a contiguous block (see §4), so the tools have no semantic
  knowledge of the channels either.

Conclusion: naming the 5AM channels is only possible from captures (what
`config/params.json` already does for 12+ channels), or from a community `.ini`.

## 3. Mode initialisation — yes, fully

See `PROTOCOL.md` §4.1. One session, opened at link bring-up by both tools:
`81` → `83 03 00 FF 00 FF 00` → `10 81`, kept alive with `3E`, closed with `20` + `82`.
There is no separate "enter test mode" service; actuator tests and adaptation resets are
ordinary services inside that session. **Implemented** (armed lazily on the first Testing
command so the logging path is untouched).

## 4. Per-family command matrix (IAWDiag V0.52)

IAWDiag carries six ECU families, each with its own comm thread and transport function.
Grouping every non-poll service by transport gives the capability matrix:

| transport | rli block | services seen |
|-----------|-----------|---------------|
| `sub_41B3A7` | 0x30–0x75 + 0xA6 | `18 00`, `1A 00`, `30 02/03/04/05/08/09 07`, `30 15 05`, `30 20 07`, `30 7E 04`, `31 21` |
| `sub_4158D0` | 0x30–0x7C + 0xA6 | `14 FF 00`, `14 FF 24`, `18 00`, `30 02/03/04/06/07/08/0C/0F/11/20/79 07`, `30 7E 04`, `31 21`, `31 24`, `33 21`, `33 24` |
| `sub_4043F7` | 0x30–0x75 + 0xA6 | `14 FF 00`, `18 00`, `30 02/03/05/08/09/0A/0B/20 07`, `30 7E 04` |
| `sub_4115A6` | 0x00–0xA1 | `14 FF 00`, `18 00`, `30 08/09/23 07`, `30 7E 04`, `31 22/25/28 03/2E`, `33` |
| `sub_4545C6` | 0x00–0x8F + 0xB0 | `18 00`, `30 02/03/05/08/09/23 07`, `30 7E 04`, `31 22`, `31 23` |
| `sub_42C7DD` | 0x30–0x7E + 0xA6 | `18 00`, `30 0C/79 07`, `33 24` |

`sub_41B3A7` is the family whose actuator set matches the Guzzi 5AM (coils 2/3, injectors
8/9, pump 5, stepper 4) — so its extras are the interesting ones for us: **`30 20 07`** and
**`30 15 05 <value>`** (the latter is the CO trim, §6).

`31 24` + `33 24 <n>` on `sub_4158D0` reads a multi-record block and writes it to a file
named `MIU1 StoredData` — a MIU stored-data dump, not a 5AM feature.

## 5. Fault codes — read, clear, and the status byte

`18 00 FF 00` / `14 FF 00` were already implemented. New from this pass: **how both tools
decode the status byte** (GuzziDiag DTC screen, IAWDiag @26617/26655):

```
status & 0x0F   fault kind, one of 1 / 2 / 4 / 8   -> labels Fault_3..Fault_6 (external ini)
status & 0x20   set  -> stored fault list ("Gespeicherte Fehler")
                clear-> current/new fault list ("Neue Fehler")
status & 0x40   warning indicator
```

GuzziDiag uses the coarser `status & 0x60 != 0`; IAWDiag computes `(status & 0x60) >> 5` and
files 1 and 3 as stored, i.e. bit 0x20 is the discriminator. **Implemented** — the Testing
tab now shows *stored / current*, the fault-kind number and a ⚠ flag, with the raw byte and
its bit pattern in the tooltip. The wording behind kinds 1/2/4/8 is in the external
localization file and is *not* recoverable, so the number is shown as-is.

The `*Dtc.dll` files that ship with JPDiag are plain SAE J2012 code tables
(2084 codes × 4 languages) — already imported into `config/dtc/`.

## 6. CO trim — recovered, deliberately **not** implemented

GuzziDiag's "CO Einstellung" screen for the 5AM (`sub_41EFFC`, bound as `dword_45D8E4`):

```
30 15 07 <value>     probe / apply for the session
30 15 05 <value>     store   (the +/- screen ORs 0x100 into the argument to pick 05)
value: signed, clamped -128..127
```

JPDiag's "Trimmer" form is the same feature with the gates spelled out: engine warm
(coolant ≥ 40 °C), read current value, *test*, then *confirm*; its note says the factory
value for a Ducati 749S sits between 22 and 29.

This writes to the ECU, the commit semantics are not sniff-verified, and the owner tunes
through the firmware path instead — so it is documented here and in `config/profiles.json`,
and nothing sends it.

## Firmware image identity (from a TunerPro XDF, verified against 5 images)

The XDF `5AM_Morini_23ECCLGPS_merged.xdf` names two string tables the ECU firmware carries about
itself: `Map Name 1` at `0x47FA4` (12 B, space padded) and `Map Name 2` at `0x48006` (16 B), which
concatenate into the calibration code — `23EC` + `CLGPSMD` = `23ECCLGPSMD`. A hardware string sits at
`0x48016` (`5AM X0000`, NUL padded) and a `55 AA 33 CC` marker immediately precedes Map Name 1,
occurring only three times in the whole 0x50000 image.

Verified by reading every image on the board: `23ECCLGPSMB/MC/MD.bin` match their names,
`my_original_fw.bin` is MC, and `granpasso_v4_draft.bin` is MC while its sidecar claims MD.

Not recovered: what the letters mean. `23EC…GPS` reads as a Granpasso mnemonic and `23ACMCORA` (from
the Corsaro XDF) as a Corsaro one, but no public source decodes the scheme — and it is not the same
scheme other makers use. Real `1A 80` dumps show Ducati answering `96520610B` and Guzzi `28640921A`,
both OEM part numbers rather than mnemonics, so image code and live Drawing must be treated as
separate namespaces. `config/fw_catalog.json` records what we know, with `verified` telling apart
what came off this bike from what came off a forum.

## 7. Bike presets — recovered in full

The embedded INI's `[Modells]` section maps **65 Moto Guzzi models** to an ECU family and a
transport variant, e.g. `Griso 1200 8V = 5AM,PF2C`, `Breva 750 = 15RC,PF1C`,
`V7 Stone = MIUG3`. Reproduced verbatim in `config/profiles.json` together with the per-ECU
command matrix, and shown read-only on the Testing tab. Protocol tags (PF09/PF10/PF1C/PF2C/
PF3C) select the K-line bring-up, not the service IDs.

Adaptation commands differ per family, which is exactly why the presets matter:

| ECU | TPS reset | self-adaptation | note |
|-----|-----------|-----------------|------|
| 5AM | `31 21` | `30 7E 04` | verified on this bike |
| 15RC | raw byte `0x89` | raw byte `0x8A` | proprietary echo handshake, not KWP |
| MIUG3 | `31 21` | — | different transport |
| 7SM | `31 23` + `33 23` polled 20 × 500 ms | — | full learn, not a reset |

## 8. Smaller findings folded into the code

- **`33 <lid>` RequestRoutineResults** — how IAWDiag confirms a routine finished on the
  families that support it. The 5AM branch of both tools does not use it, so we probe it
  best-effort after `31 21` and report whatever comes back (usually a negative response).
- **`1A 00`** — the IAW-5AM family in IAWDiag asks for identification option 0x00; we only
  asked 0x80. Both are now read and every block that answers is kept.
- **`30 20 07`** — added to `config/actuators.json` marked unverified (`*`). Likely the idle
  control behind GuzziDiag's *Leerlaufregelung* button, whose handler body is missing from
  the dump.
- **Frame format**: GuzziDiag reads 5AM parameters with the **short header** (`dword_45D934 = 0`,
  `[len][data][cs]`), we use the addressed frame — the ECU accepts both, and our rli scan
  records both framings anyway.
- **"Frage alles ab"** (`View_12`, writes `TestResultate.txt`) is GuzziDiag's own rli sweep;
  our Full parameter scan already covers 0x00–0xFF in both framings with per-sweep CSV.

## 9. Deliberately out of scope

- **Immobilizer / "Unlock ECU"** (JPDiag Form4: virginity test, 5-digit card code, ECU state
  list 0–8). The crypto is not recoverable from the VB6 decompile — only a primitive
  `byte ^0xE9 ^0x3E ^0x30` is visible. Needs a clean decompile or a seed/key sniff.
- **`31 22 / 24 / 25 / 28 03 / 2E`** — routines belonging to other IAWDiag families. Firing
  unknown routine IDs at a 5AM has unknown side effects; documented, never sent.
- **`14 FF 24`** — a clear-DTC variant scoped to another group, seen only on the MIU family.
- **Service reset / belt reset / "Gasgriff anlernen"** — present as button labels
  (`View_58`, `View_59`) and in JPDiag, but no command is recoverable for the 5AM.
