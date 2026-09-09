# IAW 5AM diagnostic protocol — reference

Everything below is what a **Magneti Marelli IAW 5AM (HW610)** actually answers on the bus:
established by observation and verified against a live capture from a Moto Guzzi carrying that
ECU. Where a value is still a guess, it says so.

Russian version: [PROTOCOL.ru.md](PROTOCOL.ru.md).

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
| 0x35 | Advance (latched)| raw / 10     | °    | 20.0° at warm idle; freezes at its last value with the engine stopped |
| 0x37 | Advance (live) | raw / 10       | °    | the same value while running; a fixed 8.0° whenever the engine is not turning |
| 0x39 | Injection      | raw / 1000     | ms   | 0 stopped, 8.6 ms cranking, ~1.4 ms at warm idle |
| 0x3A | Idle target    | raw            | rpm  | target idle vs temp: 1571 cold → 1401 warm (matches the ECU map) |
| 0x3C | Battery        | raw / 10       | V    | 11.9–14.1 V |
| 0x45 | Lambda F (front)| raw           | mV   | oscillates (closed loop) |
| 0x46 | Lambda R (rear) | raw           | mV   | 2nd lambda; a constant zero on images that stub it |
| 0x47 | Lambda integrator F | raw / 10  | %    | signed 16-bit, −18…+25 % |
| 0x48 | Lambda integrator R | raw / 10  | %    | signed 16-bit; a constant zero on images that stub it |
| 0x4B | Lambda phase F | raw            | —    | 1 frozen, 2 cold sensor, 3/4 open loop, 5 closed loop leaning out, 7 closed loop enriching |
| 0x53 | Road speed     | raw            | km/h | 0 standing, 87 at the 8384 rpm limiter in second; rpm/speed gives the gear |
| 0x6B | Idle stepper base | raw         | steps| temperature-scheduled: 117 at 51 °C, 100 from 75 °C up |
| 0x6C | Idle stepper position | raw     | steps| 0x6B + 0x6D; the sum is exact in 83 % of samples and within ±2 in 99 % |
| 0x6D | Idle stepper trim | raw         | steps| signed; the closed-loop offset carried on top of 0x6B |

Cylinders are **F/R (front/rear)** per Moto Guzzi / Ducati / Moto Morini V-twin layout.
Further named channels carry a decoder from `config/status_maps.json`: 0x49 the lambda loop flag
(0 open, 2 closed), 0x58 the engine-state bitfield (0x01 closed throttle, 0x02 after-start phase over,
0x04 engine turning), 0x61 the neutral flag, 0x76 the side stand and 0x78 the clutch. 0x54 / 0x55 are a
coil charge time compensated for battery voltage; the count stays raw.

Five more identifiers carry a state rather than a measurement. **0x7A and 0x79 are one input reported
twice**, strictly inverse across every sample where both were read; 0x7A stands at 1 whenever the
engine runs and flips to 1 in the sample the starter engages, which reads as the kill switch, RUN on
0x7A and STOP on 0x79. **0x57 is a stop-state code**: 4 while the engine runs, 8 once it has stopped,
and 16 both at the instant of one stop and some 18 s after another. **0x62** is 0 stopped, 4 at idle
and 2 off idle, flickering between the two around 1300–1700 rpm on a closed throttle. **0x5C** carries
the same closed-throttle decision as 0x58's 0x01 bit, coded 1 shut and 4 open, and is read a request
earlier. **0x75** goes to 1 in the sample the starter engages and stays there while the engine runs.

**38 of the 79 answer a constant zero.** The image serves them from three shared slots — one for a
2-byte zero, one for a 1-byte zero, one for the two 3-byte identifiers — so there is no variable
behind them and no condition that makes them move: 0x31, 0x36, 0x38, 0x3B, 0x3F–0x44, 0x46, 0x48,
0x4A, 0x4C, 0x4D, 0x4E, 0x50–0x52, 0x56, 0x59–0x5B, 0x64–0x69, 0x6F, 0x71–0x73 and 0x7B–0x7F. The
four in that list that name the rear bank (0x46, 0x48, 0x4A, 0x4C) are stubbed on this image; other
builds of the same ECU wire a second sensor. Of the 41 identifiers that have a slot of their own,
24 are the named channels above, 11 more are named but not yet proven on a bike, and 6 carry no
established meaning at all: 0x3E (a config/ID byte, a constant 52 through a whole cold start), 0x5D,
0x5E, 0x5F, 0x63 (a flat zero through the rev limiter, full throttle and a warm-up to 97 °C) and
0x6A (0 at closed throttle and flat out alike, 5..21 on a moderate steady throttle).

`config/params.json` schema: `{key, name, rli, fmt(2/0), offset, length, endian, signed, scale,
bias, recip, digits, default, group, map, map_type, dead}`. `dead` marks an identifier served by one
of those shared zero slots: the record stays, but nothing may select it. `recip != 0` → `value = recip / raw` (period-style). `default`
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
