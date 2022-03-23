"""
Copyright (c) 2022 Spencer Chang

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import cocotb
from cocotb.triggers import FallingEdge, RisingEdge, First, Timer, Event
from collections import deque
from ... import SpiSlaveBase, SpiConfig, SpiFrameError, SpiFrameTimeout


class TMC4671(SpiSlaveBase):
    def __init__(self, signals):
        self._config = SpiConfig(
            word_width=40,
            # datasheet identifies CPOL=1, CPHA=1, but
            # timing diagrams indicate shift on falling, sample on rising
            # with a clock idle polarity
            cpol=True,
            cpha=False,
            msb_first=True,
            frame_spacing_ns=6
        )

        self._address_change_callbacks = {}

        # mockup of the test registers
        self._registers = {
            0x00: int.from_bytes(b"4671", byteorder='big'),
            0x01: 0x0000_0000,
        }

        self._register_address_changed_hook(0x01, [0x00],
            lambda: {
                0: int.from_bytes(b"4671", byteorder='big'),
                1: 0x0000_0100,
                2: 0x2022_0323,
                3: 0x0010_1029,
                4: int.from_bytes(b"var2", byteorder='big'),
                5: int.from_bytes(b"rev3", byteorder='big')
            }[self._registers[0x01]]
        )

        super().__init__(signals)

    async def get_register(self, reg_num):
        await self.idle.wait()
        return self._registers[reg_num]

    def create_spi_word(self, operation, address, content):
        command = 0
        if operation == "read":
            # it is already 0
            pass
        elif operation == "write":
            command |= 1 << 39
        else:
            raise ValueError("Expected operation to be in ['read', 'write']")

        try:
            self._registers[address]
        except KeyError:
            raise ValueError(f"Expected address to be in {list(self._registers.keys())}")
        command |= (address & 0b1111) << 32
        command |= (content & 0xFFFF_FFFF)

        return command


    def _register_address_changed_hook(self, watch_address, update_addresses, f):
        self._address_change_callbacks[watch_address] = (update_addresses, f)

    async def _transaction(self, frame_start, frame_end):
        await frame_start
        self.idle.clear()

        # SCLK pin should be low at the chip select edge
        if not bool(self._sclk.value):
            raise SpiFrameError("TMC4671: sclk should be high at chip select edge")

        s = await FallingEdge(self._sclk)
        t = await Timer(20, units='ns')
        self._miso.value = self._mosi.value
        do_write = bool(int(self._mosi.value))

        if frame_end in (s, t):
            raise SpiFrameError("TMC4671: chip select deasserted in middle of transaction")

        address = 0
        for k in range(7):
            s = await First(FallingEdge(self._sclk), frame_end)
            t = await First(Timer(20, units='ns'), frame_end)
            address |= int(self._mosi.value.integer) << (7 - 1 - k)
            self._miso.value = self._mosi.value

            if frame_end in (s, t):
                raise SpiFrameError("TMC4671: chip select deasserted in middle of transaction")

        if await First(RisingEdge(self._sclk), frame_end) == frame_end:
            raise SpiFrameError("TMC4671: chip select deasserted in middle of transaction")

        post_read_wait = Timer(250, units='ns')
        if not do_write and (await First(FallingEdge(self._sclk), post_read_wait) != post_read_wait):
            raise SpiFrameError("TMC4671: SPI Timing of Read Access requires a 500ns pause")

        content = 0
        for k in range(32):
            s = await First(FallingEdge(self._sclk), frame_end)
            t = await First(Timer(20, units='ns'), frame_end)
            content |= int(self._mosi.value.integer) << (32 - 1 - k)
            self._miso.value = bool(self._registers[address] & (1 << (32 - 1 - k)))

            if frame_end in (s, t):
                raise SpiFrameError("TMC4671: chip select deasserted in middle of transaction")

        # end of frame
        if await First(frame_end, FallingEdge(self._sclk)) != frame_end:
            raise SpiFrameError("TMC4671: sampled more than 16 bits")

        if not bool(self._sclk.value):
            raise SpiFrameError("TMC4671: sclk should be high at chip select edge")

        if do_write:
            self._registers[address] = content

            if address in self._address_change_callbacks:
                cb = self._address_change_callbacks[address]
                for addr in cb[0]:
                    self._registers[addr] = cb[1]()
