// Bidirectional DShot600 on Raspberry Pi 5 using the RP1 PIO block (via piolib).
//
// The PIO state machine does all the timing-critical work:
//   1. send one 16-bit DShot frame, inverted (idle high, bits are low pulses)
//   2. release the pin and wait for the ESC's telemetry reply
//   3. sample the 21-bit reply and push it to the RX FIFO (0 = no reply)
// Python (dshot.py) builds the frames and decodes the replies.

#include "hardware/pio.h"

#define DSHOT_BITRATE   600000  // DShot600
#define CYCLES_PER_BIT  40      // PIO cycles per DShot bit (reply bits are 32 = 5/4 rate)
#define REPLY_TIMEOUT   1200    // wait-loop iterations (2 cycles each) = 100 us

// Hand-assembled PIO program. No side-set, so [n] is a delay of n extra cycles.
static const uint16_t program_instructions[] = {
    0x80a0, //  0: pull   block           ; wait for next frame from C
    0xe001, //  1: set    pins, 1         ; idle level high...
    0xe081, //  2: set    pindirs, 1      ; ...then start driving the pin
    0xe02f, //  3: set    x, 15           ; 16 bits
    0xee00, //  4: set    pins, 0     [14] ; bit: 15 cycles low
    0x6e01, //  5: out    pins, 1     [14] ;      15 cycles inverted data bit
    0xe801, //  6: set    pins, 1     [8]  ;      10 cycles high (incl. jmp)
    0x0044, //  7: jmp    x--, 4
    0x6050, //  8: out    y, 16           ; low half of the word = reply timeout
    0xe080, //  9: set    pindirs, 0      ; release line, pull-up holds it high
    0x00d0, // 10: jmp    pin, 16         ; still high -> keep waiting
    0xee34, // 11: set    x, 20       [14] ; falling edge: skip to middle of bit
    0x5e01, // 12: in     pins, 1     [30] ; sample one reply bit every 32 cycles
    0x004c, // 13: jmp    x--, 12
    0x8020, // 14: push   block           ; reply (21 bits)
    0x0000, // 15: jmp    0
    0x008a, // 16: jmp    y--, 10         ; timeout counter
    0x8020, // 17: push   block           ; timeout: push 0 (ISR is empty)
};

static const struct pio_program program = {
    .instructions = program_instructions,
    .length = 18,
    .origin = -1,
};

static PIO pio;          // one PIO block shared by all ESCs
static int offset = -1;  // program is loaded once and shared

// Start DShot on a GPIO. Returns a handle (state machine number) or < 0 on error.
int dshot_open(unsigned gpio)
{
    if (!pio) {
        PIO p = pio_open(0);
        if (PIO_IS_ERR(p))
            return -1;  // /dev/pio0 missing or no permission
        pio = p;
    }
    pio_select(pio);  // piolib keeps a "current PIO" per thread
    if (offset < 0) {
        if (!pio_can_add_program(pio, &program))
            return -2;
        offset = pio_add_program(pio, &program);
    }
    int sm = pio_claim_unused_sm(pio, false);
    if (sm < 0)
        return -3;  // all 4 state machines in use

    gpio_pull_up(gpio);
    pio_gpio_init(pio, gpio);

    pio_sm_config c = pio_get_default_sm_config();
    sm_config_set_wrap(&c, offset, offset + program.length - 1);
    sm_config_set_set_pins(&c, gpio, 1);
    sm_config_set_out_pins(&c, gpio, 1);
    sm_config_set_in_pins(&c, gpio);
    sm_config_set_jmp_pin(&c, gpio);
    sm_config_set_out_shift(&c, false, false, 32);  // MSB first
    sm_config_set_in_shift(&c, false, false, 32);
    sm_config_set_clkdiv(&c, (float)clock_get_hz(clk_sys) / (DSHOT_BITRATE * CYCLES_PER_BIT));
    pio_sm_init(pio, sm, offset, &c);
    pio_sm_set_enabled(pio, sm, true);
    return sm;
}

// Send one frame and return the raw 21-bit reply (0 = no reply).
uint32_t dshot_send(int sm, uint16_t frame)
{
    uint16_t inverted = ~frame;  // the PIO outputs data bits as-is; inverted DShot needs ~bit
    pio_sm_put_blocking(pio, sm, ((uint32_t)inverted << 16) | REPLY_TIMEOUT);
    return pio_sm_get_blocking(pio, sm);
}

void dshot_close(int sm)
{
    pio_sm_set_enabled(pio, sm, false);
    pio_sm_unclaim(pio, sm);
}
