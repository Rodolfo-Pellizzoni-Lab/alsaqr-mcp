#include "encoding.h"
#include <stdio.h>
#include <stdlib.h>
#include "utils.h"
#include "pmu_test_func.c"

// Four-core rendezvous: core 0 wakes cores 2 and 3 with APMU counter-overflow interrupts (core 1 is woken by the
// startup code), every core checks in, and core 0 exits with 0 once all four have checked in.

__attribute__ ((section(".heapl2ram"))) volatile int checked_in[4] = {0, 0, 0, 0};

#define read_32b(addr)         (*(volatile uint32_t *)(long)(addr))
#define write_32b(addr, val_)  (*(volatile uint32_t *)(long)(addr) = val_)

#define PLIC_BASE              0x0C000000
#define PLIC_PRIO(src)         (PLIC_BASE + 4*(src))
// interrupt-enable word of PLIC context `ctx` that holds source `src`
#define PLIC_EN(ctx, src)      (PLIC_BASE + 0x2000 + 0x80*(ctx) + 4*((src)/32))

// PLIC context of each hart's machine-mode external interrupt
static const int m_ctx[4] = {1, 3, 5, 6};

// APMU counter i raises PLIC source 156 + i on overflow
#define PMU_PLIC_SRC(cnt)      (156 + (cnt))

int thread_entry(int cid, int nc){
  return 0;
}

static void wake_with_counter(int hart, int cnt) {
  int src = PMU_PLIC_SRC(cnt);
  write_32b(PLIC_PRIO(src), 1);
  write_32b(PLIC_EN(m_ctx[hart], src), 1 << (src % 32));
  write_32b(EVENT_INFO_BASE_ADDR + cnt*COUNTER_BUNDLE_SIZE, OVERFLOW_EN);
  write_32b(COUNTER_BASE_ADDR + cnt*COUNTER_BUNDLE_SIZE, 0x40000000);
}

int main(int argc, char const *argv[]) {
  uint32_t mhartid;
  asm volatile ("csrr %0, 0xF14\n" : "=r" (mhartid));

  if (mhartid == 0) {
    #ifdef FPGA_EMULATION
    uint32_t baud_rate = 9600;
    uint32_t test_freq = 100000000;
    #else
    set_flls();
    uint32_t baud_rate = 115200;
    uint32_t test_freq = 50000000;
    #endif
    uart_set_cfg(0,(test_freq/baud_rate)>>4);
    write_32b(0x1C + 0x1A106000, 0xA0000000);   // LLC address fix, as in quad_boot

    checked_in[0] = 1;
    printf("Core0: waking cores 2 and 3\n");
    wake_with_counter(2, 0);
    wake_with_counter(3, 1);

    while (!(checked_in[1] && checked_in[2] && checked_in[3])) {
      asm volatile("nop");
    }
    printf("Core0: all 4 cores checked in\n");
    return 0;
  }

  // cores 1-3: check in, then park (returning would end the test early through exit())
  for (volatile int d = 0; d < 200 * (int)mhartid; d++);   // stagger the prints
  printf("Core%d checked in\n", mhartid);
  checked_in[mhartid] = 1;
  while (1) {
    asm volatile("nop");
  }
  return 0;
}
