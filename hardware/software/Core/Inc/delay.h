#ifndef DELAY_H
#define DELAY_H

#define SYSTICK_CTRL (*(volatile unsigned int *)0xE000E010)
#define SYSTICK_LOAD (*(volatile unsigned int *)0xE000E014)
#define SYSTICK_VAL (*(volatile unsigned int *)0xE000E018)

void delay_init(void);
void delay(unsigned int time_ms);

#endif