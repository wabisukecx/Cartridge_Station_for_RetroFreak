# Communication design

[日本語](ja/PROTOCOL.md)

## Reader

- The USB VID:PID is f00d:1337. It is the RetroFreak cartridge adapter and works as USB CDC ACM (a virtual serial port).
- The product-information reply has two 16-bit values, build and product. The values used to identify the product are build=10 and product=4919.
- Communication is synchronous request and reply. One worker owns one transport, and never sends another request in the middle of a request.

## Transports

| Transport | Use | Settings |
|---|---|---|
| SerialTransport | Windows, and environments where the reader appears as a serial port | 115200 bps, 8N1, read timeout 3 s, write timeout 5 s, DTR and RTS enabled, no flow control. The input buffer is discarded on connection |
| UsbTransport | Direct USB on MiSTer (Linux) | Bulk transfer with PyUSB and libusb. Read timeout 3 s, write timeout 5 s |

### Choosing a transport

1. If a port is given explicitly, use serial.
2. If direct USB is given explicitly, use UsbTransport.
3. If neither is given, enumerate the serial ports whose VID:PID match. If two or more are found it is an error (a port must be specified); if one is found, use that port.
4. If none is found, use UsbTransport on Linux. On Windows, it is an error.
- Enumeration does not open unrelated serial devices. Only the identified reader is opened, and if that fails the tool reports the error rather than silently switching to another transport.

### UsbTransport initialization

- Requires exactly one device with the matching VID:PID.
- From the active configuration, chooses the single interface that has alternate setting 0 and exactly one bulk IN and one bulk OUT endpoint. It stops if this is ambiguous or absent. It does not change alternate settings or the configuration.
- If the data interface is of the CDC data class, requires exactly one control interface (CDC ACM). This reader has no CDC Union descriptor, so the two are associated by this condition.
- If necessary, detaches the kernel driver and claims the interface. On exit it releases the interface and reattaches the driver it detached.
- CDC control: sets 115200 bps, 8N1 with SET_LINE_CODING, and enables DTR and RTS with SET_CONTROL_LINE_STATE (the same settings as serial).
- Reads are done in multiples of the endpoint's maximum packet size, and surplus bytes are kept in an internal buffer. A timeout is returned as an empty byte string, matching the serial contract.
- For libusb, besides Python's standard lookup, the tool also looks for the existing shared library on Linux. No native library is bundled.

### USB input recovery

- Right after connecting, and before the next request after a send or receive failure, the input endpoint is read and discarded within limits. It is not discarded unconditionally before every normal read.
- Limits: 0.5 seconds, 256 KiB, and 64 reads. Each read has a 20 ms timeout, and two empty reads in a row mean the endpoint is quiet.
- If a limit is reached, it is an error and the user is asked to check again or to reconnect USB. A failed request is never resent automatically.
- The result (outcome, discarded_bytes, reads, duration_ms) is kept as the most recent recovery information and written to the log. The content of the discarded data is not recorded.
- Quiet is the observation that the endpoint was empty at that moment. It is not proof that the firmware or a USB stall has recovered.

## Packets

- A request is a sequence of little-endian 32-bit unsigned integers, built with `packet(w0, w1, ...)`.
- A reply header is 8 bytes: status (32 bits) and length (32 bits). Normal is status=0. length is the length of the payload that follows.

## Common commands

| Command | Request | Reply |
|---|---|---|
| 0x01 product information | (1, 0, 4) | Header with length=4, then build and product (16 bits each) |
| 0x06 cartridge state | (6, 0, 4) | Header with length=4 (a reply with length=0 is also accepted and treated as empty); the first of the following 4 bytes is the state code |
| 0x04 mode | (4, 4, 0, value) | 8 bytes. The content is recorded without being checked |
| 0x05 commit | (5, 0, 0) | 8 bytes. The content is recorded without being checked |
| Connection initialization | (4, 4, 0, 0) | 8 bytes. 0x05 is not sent |
| 0x2F SFC mode | Five words: (0x2F, 8, 0, 0, value) | 8 bytes. The value is the last word |

- For the replies to 04 and 05, only the amount received is checked, and no meaning is guessed from the content. The raw replies are kept, up to the most recent 32, for diagnosis.
- The meaning of the value 0/1 passed to 04 is not settled, so it is sent only in the start and end sequences decided for each system (only the GBA start uses value 0).

### Cartridge state codes

| Value | Meaning |
|---|---|
| 0 | Empty (a length=0 reply) |
| 1 | SFC |
| 2 | MD |
| 3 | FC/NES |
| 5 | GB/GBC |
| 6 | GBA |
| 7 | Treated as a supported state code (corresponds to SMS; no read function) |
| 8 | PCE |
| 254 | Several cartridges are inserted at the same time |
| 255 | Empty (the value the reader actually returns). Distinguished from a communication loss or an unknown state |

- An empty slot is returned as a header (status=0, length=4) followed by the payload FFFFFFFF.
- If cartridges are inserted in several slots, 254 is returned; when one is removed, the value of the remaining system returns.
- An undefined value such as 4 is shown as "a different system / unknown kind", and is treated neither as an insertion nor as a removal.
- A state is confirmed when the same value is obtained three times in a row. An unknown value is not regarded as a removal.

## Memory reads

1. Discard stale bytes in the input buffer.
2. Send the request (command, 8, size, address, size).
3. Check the reply header (status=0, length=size).
4. Send the state request (6, 0, 4) right after it (PCE alone does not send it here, as described below).
5. Receive the data for the size.
6. Read the trailing state reply and confirm it is the expected system's code. If not, stop as "the cartridge disappeared or changed".

- Addresses are in bytes, and one read is at most 32 KiB (SFC uses a 24-bit address and 1–32768 bytes).
- Reception repeats until the specified number of bytes has arrived. The overall deadline is 10 seconds, checked between receives. The lower layers block for their own timeouts, so a return within 10 seconds is not guaranteed.

### Per-system commands

| System | Read | Write | Notes |
|---|---|---|---|
| FC/NES | 0x11 CPU space, 0x13 PPU (CHR) space | 0x12 normal write, 0x15 lower-register write | After a normal write, a dummy read of 8 bytes from 0x8000 is done with 0x11 (not after a lower-register write) |
| GB/GBC | 0x1B | 0x1C | Only reading the ROM area and limited bank control |
| GBA | 0x1F | None | At most 32 KiB |
| MD | 0x0D | 0x0E | Writes only to 0xA130F1 and 0xA130FF |
| PCE | 0x2D | 0x2E | The state request is not inserted during the read; it is checked independently after all the data is received. Writes only to 0x1FF0–0x1FFF |
| SFC | 0x07 | 0x08 ROM mapping | 24-bit addresses. 0x2F is also used for mode switching. Writes only the registers and values for the ROM of SA-1, S-DD1, and the permitted CX4 ([SFC design](SYSTEM_SFC.md)) |

### Writes (bank control)

1. Send the request (command, 8, 1, address, 1).
2. Check the ready header (length=0).
3. Send the 1-byte value.
4. Check the completion header (length=0).

- The public write entry points strictly limit the addresses and values allowed for each system and board, and never reach save RAM, IDs, flash-programming keys, and so on. The generic entry points (FC's cpu_register_write, GB's gb_register, and so on) reject addresses and values outside the range of the ROM bank registers.
- For boards that need registers at low addresses (0x6000–0x7FFF and so on), a dedicated function for that board is provided, and it passes only the allowed addresses and values.

## Modes per system

| System | Start | End |
|---|---|---|
| FC/NES | 04(1), 05, wait 200 ms | 04(0), 05 |
| GB/GBC | 04(1), 05, wait 200 ms | 04(0), 05 |
| GBA | 04(0), 05, wait 200 ms | 04(0), 05 |
| MD | 04(1), 05, wait 200 ms | 04(0), 05 |
| PCE | 04(1), 05, wait 200 ms (the voltage is not switched by probing) | 04(0), 05 |
| SFC | 04(1), 2F(1), 05, 05, read 16 KiB from $00:C000 | 2F(0), 04(0), 05 |

## Error design

- A communication error includes the operation name, the command, the address, the stage (header, payload, or state reply), and the number of bytes received and required. For example:
  `Truncated response: no bytes returned (timeout or end of stream) [GBA read command 0x1F address 0x008000 payload; 2/8 bytes received]`
- A send failure shows the number of bytes requested and "transmitted count unknown". It does not claim that everything arrived or that nothing did.
- An empty reply only indicates a timeout or the end of the stream; it is not taken to mean the cable was unplugged.
- Exceptions from reading and sending include the original exception name and message, and the original exception stays in the exception chain.
- An invalid header shows status, length, the expected length, and the name of the operation.
- A transaction that hit a partial reply, an invalid header, or a send/receive exception is neither continued nor resent automatically. Because unread data may have lost the frame boundary, the service closes the session and the user reconnects.
- Details may not fit on the screen; on MiSTer they are recorded in runtime/manager.log.

## Diagnostics

- probe: checks only the product information and the cartridge state (does not read a ROM).
- Communication trace: records, as JSON Lines, the bytes (hex) the application passed to the transport, the requested length, the bytes actually received and their count, the start time relative to the connection, the duration of the call, and errors. The result of USB input recovery is included. An existing trace file is never overwritten.
- `diagnose-gb` does only header reads, with no writes or bank switching. `probe-pce` reads the samples needed to decide the method and does limited bank switching when needed, but never does a whole-ROM read or a save.
