# IAW 5AM diagnostic protocol — reference

Everything below is what a **Magneti Marelli IAW 5AM (HW610)** actually answers on the bus:
established by observation and verified against a live capture from a Moto Guzzi carrying that
ECU. Where a value is still a guess, it says so.

Русская версия: [PROTOCOL.ru.md](PROTOCOL.ru.md).

---

## 1. Physical layer

- **KWP2000 / ISO 14230** over the single-wire **K-Line**, **10400 baud, 8N1**, no flow control.
- Half-duplex: everything the tester transmits is echoed back on RX before the ECU replies —
  the echo must be read and discarded.
- Cable contains an L9637D-class transceiver; the FTDI TXD does not drive K-Line directly.

## 2. Frame format

Addressed frame (both directions):

```
[FMT] [TGT] [SRC] [DATA ...] [CS]
FMT = 0x80 | len      len = number of DATA bytes (1..63)
TGT = 0x10 (ECU)      SRC = 0xF1 (tester)          request:  [.. 10 F1 ..]
CS  = sum(all preceding bytes) & 0xFF              response: [.. F1 10 ..]
```

Short frame (no address information), `FMT` high bits = `00`:

```
[len] [DATA ...] [CS]
```

The IAW 5AM answers both framings for reads; the tool uses addressed frames by default.

## 3. Initialisation

Two wake-ups, selectable in Config → K-Line bus → Init.

**Fast init** (default): pull K-Line low **25 ms** (serial BREAK), release **25 ms**, then
send StartCommunication.

**Slow init (5-baud)**: bit-bang the address byte **0x33** at 5 baud (200 ms/bit — idle high,
start bit low, 8 data bits LSB-first, stop bit high) via BREAK. The ECU then replies at 10400
baud with `0x55` (sync), `KW1`, `KW2`; the tester sends the **inverted KW2**, the ECU sends the
**inverted address** (`~0x33 = 0xCC`). No StartCommunication follows — slow init establishes the
session by itself.

## 4. Session (live data)

```
(init)
-> 81                 StartCommunication          (fast init only)
<- C1 EA 8F ...        positive + keybytes
   ... poll loop ...
-> 3E                 TesterPresent (keep-alive)  when idle > 3 s
-> 82                 StopCommunication           on exit
<- C2 ...
```

The live-data path does **not** send `10 85` StartDiagnosticSession or `1A` ReadEcuId — the
IAW 5AM rejects `10 85` with NRC 0x22 (conditionsNotCorrect). (ReadEcuId `1A 80` is used once,
separately, for the identity block — see §8.)

### 4.1 Diagnostic session (required by §6 and §7)

A session is brought up right after `81`:

```
-> 83 03 00 FF 00 FF 00   AccessTimingParameter (subfn 03 = set given values:
                          P2min=00 P2max=FF P3min=00 P3max=FF P4min=00)
-> 10 81                  StartDiagnosticSession, session 0x81 (NOT 0x85!)
   ... loop ...
-> 20                     StopDiagnosticSession   on exit
-> 82                     StopCommunication
```

The answers to `83` and `10 81` are not checked — they are sent and ignored.

**There is no separate "enter test mode" command on the 5AM**: `31 21`, `30 7E 04` and
`30 <lid> 07/00` are ordinary services inside that session, and service `0x10` appears nowhere
but the bring-up. A "test mode" is a convention of the user interface, not a state of the ECU:
parameter polling keeps running while a test does.

The logger arms this session **lazily, before the first Testing command**
(`KWP2000Session.enter_test_mode`), since `21 <rli>` works in the default session anyway.

Negative response: `7F <SID> <NRC>` (0x10 generalReject, 0x11 serviceNotSupported,
0x12 subFunctionNotSupported, 0x22 conditionsNotCorrect, 0x31 requestOutOfRange, …).

## 5. Live parameters — service 0x21 (ReadDataByLocalIdentifier)

Each measurement is its **own** request; there is no single "read all" block.

```
-> 21 <rli>
<- 61 <rli> <value...>        value bytes = response DATA at offset 2, big-endian, 1..3 bytes
```

The IAW 5AM answers every `rli` in **0x30..0x7F** (79 live identifiers on the test bike). The
value byte length varies per rli and is taken from the frame length. Named channels and their formulas were
cross-checked against the on-bike capture (RPM ~1500 idle, injection 4–29 ms, battery ~12–14 V,
lambdas oscillating):

| rli  | channel        | formula        | unit | notes |
|------|----------------|----------------|------|-------|
| 0x30 | RPM            | raw            | rpm  | idle ~1500, revs to ~4500 |
| 0x32 | Air temp       | raw − 40       | °C   | ~ambient |
| 0x33 | Coolant temp   | raw − 40       | °C   | rises on warm-up |
| 0x34 | Throttle       | raw / 10       | °    | idle ~1.8° |
| 0x35 | Advance        | raw            | °    | reads ~200 raw — scale to confirm |
| 0x37 | Injection      | raw / 20       | ms   | 4–29 ms |
| 0x39 | Idle governor  | raw            | rpm  | governor output; ~1281 at warm idle, lags actual rpm when riding |
| 0x3A | Idle target    | raw            | rpm  | target idle vs temp: 1571 cold → 1401 warm (matches the ECU map) |
| 0x3B | CO / duty      | raw            | %    | 8-bit |
| 0x3C | Battery        | raw / 10       | V    | 11.9–14.1 V |
| 0x45 | Lambda F (front)| raw           | mV   | oscillates (closed loop) |
| 0x46 | Lambda R (rear) | raw           | mV   | 2nd lambda |
| 0x47 | Fuel trim F    | raw / 100      | %    | signed 16-bit |
| 0x48 | Fuel trim R    | raw / 100      | %    | signed 16-bit |

Cylinders are **F/R (front/rear)** per Moto Guzzi / Ducati / Moto Morini V-twin layout.
The remaining `rli` (0x31, 0x36, 0x38, 0x3D–0x44, 0x49–0x7F) are readable but unnamed. From the
2026-08-21 capture a few have a working guess, carried in the param name in brackets: 0x3E config/ID byte (const 52), 0x49 status
flag (0/2, closed-loop?), 0x4B counter/status (0–7), 0x54 / 0x55 raw ADC channels (weak +0.2 correlation
with rpm and injection), 0x57 status (const 4, rarely 16), 0x58 status/gear? (0–7).

`config/params.json` schema: `{key, name, rli, fmt(2/0), offset, length, endian, signed, scale,
bias, recip, digits, default}`. `recip != 0` → `value = recip / raw` (period-style). `default`
= selected for the decoded log at startup (named channels on, unidentified off). The worker polls
**only selected** params, so fewer selected = higher poll rate.

## 6. Adaptation resets (verified for IAW 5AM)

Do these with the engine off (kill-switch) and ignition on.

| function            | request     | positive response | service |
|---------------------|-------------|-------------------|---------|
| **TPS reset** (Drosselklappe zurücksetzen) | `31 21` | `71 21` | StartRoutineByLocalId |
| **Self-adaptation reset** (Selbstadaption) | `30 7E 04` | `70 7E` | IOControlByLocalId |

> IAW 5AM PF1C and PF2C use the same commands; PF3C has no TPS-reset button. Other ECU families
> differ (IAW15RC = proprietary byte 0x89; IAW7SM = `31 23` + timer; MIUG3 = `31 21`, other
> transport). The wire command on the 5AM is `31 21`; only a K-Line sniff confirms it 100 %.

## 7. Actuator tests — service 0x30 (InputOutputControlByLocalIdentifier)

```
-> 30 <localid> 07        activate
-> 30 <localid> 00        deactivate
<- 70 <localid> ...        positive
```

LocalID → actuator per the IAW test map, command byte 07/00.
A 2-cylinder Guzzi only has some of these; the rest simply return an NRC.

| id | actuator | id | actuator |
|----|----------|----|----------|
| 1  | A/C compressor relay | 10 | Injector cyl 3 |
| 2  | Coil (front cyl)      | 11 | Injector cyl 4 |
| 3  | Coil (rear cyl)       | 12 | Electric fan 1 relay |
| 4  | Idle stepper          | 13 | Electric fan 2 relay |
| 5  | Fuel pump relay       | 14 | Oxygen heater 1 |
| 6  | Tachometer            | 15 | Oxygen heater 2 |
| 8  | Injector (front cyl)  | 16 | Canister purge valve |
| 9  | Injector (rear cyl)   | 17 | Warning lamp |
|    |                       | 18 | Water-temp lamp |

There is no ECU-side timer on the 5AM: an output stays energised until something switches it
off. The logger sends a **momentary pulse** instead: the `07` frame comes from the command, the `00` frame from the worker's poll loop at the
deadline. The length is set on the Testing tab (default 5 s, clamped to 0.5–30 s). The board owns
that timing, so live values keep updating during a test and the output is released even if the
browser is closed or drops off Wi-Fi.

Part of the map is separately confirmed for the 5AM: fuel pump=5, coil R=3, injector R=9,
tachometer=6.

### 7.1 Routine results — service 0x33

`33 <localid>` (RequestRoutineResultsByLocalIdentifier) confirms that a routine finished, on the
ECU families that answer it (`31 21` then `33 21`). It is not part of the 5AM flow, so the logger
probes it best-effort after a TPS reset and reports the answer — a negative response there is
expected, not a failure.

## 8. Fault codes & identity

Status byte: `status & 0x0F` = fault kind (1/2/4/8), `status & 0x20` = stored
(clear = current/new), `status & 0x40` = warning indicator. Bit 0x20 is the one that decides
stored vs. current — the coarser `status & 0x60 != 0` also files a warning as stored.

- **Read DTC** — `18 00 FF 00` (ReadDTCByStatus) → `58 <count> [hi lo status]×count`. Each DTC is a
  2-byte SAE J2012 code (`P/C/B/U` + digits) + status byte. Descriptions come from the standard SAE
  tables (en/es/fr/it).
- **Clear DTC** — `14 FF 00` (ClearDiagnosticInformation).
- **ECU identity** — `1A 80` (ReadEcuIdentification) → labelled fields sliced by `config/ecu_id.json`
  offsets: Drawing (0:11), Hardware (11:22), Omologation, Software, Tester, Date. Rendered as a
  labelled `.txt` (Date = read date) shown in Firmware → Read and saved next to a dump.

## 9. Firmware read/write

Programming path (handled by [`denandz/5am_util`](https://github.com/denandz/5am_util),
built for aarch64): `10 85` StartDiagnosticSession → `27` SecurityAccess (seed/key) → baud switch
→ block transfer (`34`/`36`) + checksum. Image size is exactly **327680** bytes (0x50000);
writing any other size is blocked. Read ≈ 20 min. ⚠ Writing risks bricking the ECU.

## 10. Status bit-fields

Decoders shipped in `config/status_maps.json` (attach to a status rli once identified):

- **motorstate** (bits): 01 Power-On/engine off, 02 Ignition on/engine off, 04 Engine running,
  08 Stalling, 10 Power-Latch active, 20 Power-Latch ended.
- **motor** (cut-off bits, 16-bit): 0001 Crank … 0040 Steady running … 0400 In cut-off …
  2000 Exit cut-off (accel).
- **tps_state** (bits): 01 min, 02 full open, 04 mid.
- **immobilizer** (enum): 0 stored, 1 virgin, 2 no-start, 3 universal code, 5 backdoor code,
  6 erroneous key, 8 no code.
- **sidestand / clutch / gear / onoff** (enums).
