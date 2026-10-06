#include <stdio.h>
#include <stdlib.h>
#include "utils.h"
#include "encoding.h"

// Small self-test: core 0 runs five checks and exits with the number that failed.

static int fib(int n) { int a = 0, b = 1; for (int i = 0; i < n; i++) { int t = a + b; a = b; b = t; } return a; }
static int popcount(unsigned x) { int c = 0; while (x) { c += x & 1; x >>= 1; } return c; }
static int sum_to(int n) { int s = 0; for (int i = 1; i <= n; i++) s += i; return s; }

int main(int argc, char const *argv[]) {
  if (read_csr(mhartid) != 0) {
    while (1) asm volatile("wfi");          // only core 0 runs the checks
  }
  struct { const char *name; long got, want; } c[] = {
    {"sum_to(100)",       sum_to(100),        5050},
    {"fib(20)",           fib(20),            6766},
    {"popcount(0xF0F0)",  popcount(0xF0F0),   8},
    {"(unsigned char)300", (unsigned char)300, 44},
    {"fib(10)*3",         fib(10) * 3,        156},
  };
  int fails = 0;
  for (int i = 0; i < 5; i++) {
    int ok = c[i].got == c[i].want;
    printf("check %d %s: %s\r\n", i + 1, c[i].name, ok ? "ok" : "mismatch");
    fails += !ok;
  }
  uart_wait_tx_done();
  return fails;
}
